"""Prava payments REST client.

Wraps the Prava sandbox API (docs.prava.space) for minting one-time,
merchant-scoped, amount-capped virtual cards.

Endpoints used:
  POST /v1/sessions                              -- create_session
  GET  /v1/sessions/{session_id}/payment-result   -- get_payment_result
  POST /v1/sessions/{session_id}/report-status    -- report_status
  POST /v1/sessions/{session_id}/revoke           -- revoke_session

NEVER log or repr the full `token` / `dynamic_cvv` values -- always mask
to the last 4 characters when displaying credentials.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

DEFAULT_BACKEND_URL = "https://sandbox.api.prava.space"

VALID_REPORT_STATUSES = {"APPROVED", "DECLINED"}

TERMINAL_SESSION_STATUSES = {"completed", "failed"}

# Reserved / special-use / non-delegated TLDs. Prava forwards both the
# customer email and merchant_details.url to the card network; anything on a
# TLD that does not resolve in the public DNS root passes every earlier step
# and then dies at passkey registration (PASSKEY_REG_FAILED). Reject early.
RESERVED_TLDS = frozenset(
    {
        "local", "test", "example", "demo", "invalid", "localhost", "internal",
        "devices", "lan", "home", "corp", "domain", "host", "intranet", "private",
        "onion", "alt", "nep", "dev-local", "box", "site-local",
    }
)


def _tld_of(hostname: str) -> str:
    return hostname.rsplit(".", 1)[-1].lower() if "." in hostname else hostname.lower()


def _tld_is_plausible(tld: str) -> bool:
    """True when `tld` looks like a real delegated public-DNS TLD.

    We cannot ship the full IANA root zone, so this is a shape check plus the
    explicit reserved-TLD denylist: alphabetic (or an IDN a-label), >= 2 chars,
    and not special-use.
    """
    if tld in RESERVED_TLDS:
        return False
    if tld.startswith("xn--"):
        return True
    return len(tld) >= 2 and tld.isalpha()


def validate_user_email(email: str) -> str:
    """Return `email` if it is safe to forward to the card network.

    Raises PravaError when the address is malformed or sits on a reserved /
    made-up TLD. `foo@example.com` is fine (.com is a real TLD);
    `foo@something.example` is not.
    """
    value = (email or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+", value):
        raise PravaError(
            f"Invalid user_email {email!r}: must be a single address of the form name@domain.",
            code="INVALID_USER_EMAIL",
        )
    domain = value.rsplit("@", 1)[1].rstrip(".").lower()
    if "." not in domain:
        raise PravaError(
            f"Invalid user_email {email!r}: domain {domain!r} has no TLD. Prava forwards this "
            f"address to the card network during passkey registration; use a real domain "
            f"such as gifting-demo@example.com.",
            code="INVALID_USER_EMAIL",
        )
    tld = _tld_of(domain)
    if not _tld_is_plausible(tld):
        raise PravaError(
            f"Invalid user_email {email!r}: '.{tld}' is a reserved or non-delegated TLD. "
            f"Passkey registration will fail at the very last step (PASSKEY_REG_FAILED). "
            f"Use a real TLD, e.g. gifting-demo@example.com.",
            code="INVALID_USER_EMAIL",
        )
    return value


def normalize_merchant_url(url: str) -> str:
    """Return a bare `https://host` origin for `merchant_details.url`.

    Prava requires a bare https origin on a real TLD -- no path, no query, no
    fragment. A path is STRIPPED (and logged) rather than rejected, because the
    model routinely supplies a full product URL and losing the whole checkout
    over it is worse than normalizing. Everything else -- missing/`http`/other
    scheme, no host, reserved or made-up TLD -- raises PravaError.
    """
    raw = (url or "").strip()
    if not raw:
        raise PravaError(
            "merchant_url is required and must be a bare https origin, e.g. https://example.com",
            code="INVALID_MERCHANT_URL",
        )

    parsed = urlparse(raw)
    if not parsed.scheme:
        raise PravaError(
            f"Invalid merchant_url {raw!r}: missing scheme. Supply a bare https origin, "
            f"e.g. https://example.com (not 'example.com').",
            code="INVALID_MERCHANT_URL",
        )
    if parsed.scheme.lower() != "https":
        raise PravaError(
            f"Invalid merchant_url {raw!r}: scheme must be https, got {parsed.scheme!r}.",
            code="INVALID_MERCHANT_URL",
        )

    hostname = (parsed.hostname or "").rstrip(".").lower()
    if not hostname:
        raise PravaError(
            f"Invalid merchant_url {raw!r}: no host. Supply a bare https origin, "
            f"e.g. https://example.com",
            code="INVALID_MERCHANT_URL",
        )
    if "." not in hostname:
        raise PravaError(
            f"Invalid merchant_url {raw!r}: host {hostname!r} has no TLD. Prava rejects "
            f"non-public hosts; supply a real storefront origin.",
            code="INVALID_MERCHANT_URL",
        )
    tld = _tld_of(hostname)
    if not _tld_is_plausible(tld):
        raise PravaError(
            f"Invalid merchant_url {raw!r}: '.{tld}' is a reserved or non-delegated TLD. "
            f"Prava requires a merchant origin on a real public TLD.",
            code="INVALID_MERCHANT_URL",
        )

    origin = f"https://{hostname}"
    if parsed.port:
        origin = f"{origin}:{parsed.port}"
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        log.warning(
            "merchant_url %r carried a path/query/fragment; normalized to bare origin %r "
            "(Prava rejects non-origin merchant URLs)",
            raw,
            origin,
        )
    return origin


def mask_secret(value: Optional[str]) -> Optional[str]:
    """Mask a sensitive credential value, showing only the last 4 chars.

    Public helper -- use this (never the raw `token`/`dynamic_cvv`) in any
    logging, printing, or repr of a one-time card credential.
    """
    if value is None:
        return None
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


class PravaError(Exception):
    """Raised when the Prava API returns a non-2xx response."""

    def __init__(
        self,
        message: str,
        code: Optional[str] = None,
        http_status: Optional[int] = None,
        details: Any = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status
        self.details = details

    def __repr__(self) -> str:
        return f"PravaError(message={self.message!r}, code={self.code!r}, http_status={self.http_status!r})"


@dataclass
class MerchantDetails:
    name: str
    url: str
    country_code_iso2: str


@dataclass
class ProductDetail:
    description: str
    unit_price: str
    quantity: int


@dataclass
class PurchaseContext:
    merchant_details: MerchantDetails
    product_details: list[ProductDetail]
    effective_until_minutes: int = 15


@dataclass
class Session:
    """Response from POST /v1/sessions."""

    session_id: str
    session_token: str
    iframe_url: str
    order_id: str
    expires_at: str


@dataclass
class LineItem:
    """A single line item within a transaction's payment result."""

    txn_ref_id: str
    merchant_name: str
    merchant_url: str
    total_amount: str
    status: str
    token: Optional[str] = None
    dynamic_cvv: Optional[str] = None
    expiry_month: Optional[str] = None
    expiry_year: Optional[str] = None
    products: list[Any] = field(default_factory=list)

    def has_credential(self) -> bool:
        """True when this line item currently carries a one-time card."""
        return self.status == "awaiting_result" and self.token is not None

    def __repr__(self) -> str:
        return (
            f"LineItem(txn_ref_id={self.txn_ref_id!r}, status={self.status!r}, "
            f"token={mask_secret(self.token)!r}, dynamic_cvv={mask_secret(self.dynamic_cvv)!r}, "
            f"expiry_month={self.expiry_month!r}, expiry_year={self.expiry_year!r})"
        )


@dataclass
class Transaction:
    """A single transaction within a payment result."""

    txn_id: str
    status: str
    line_items: list[LineItem] = field(default_factory=list)
    error: Optional[Any] = None


@dataclass
class PaymentResult:
    """Response from GET /v1/sessions/{session_id}/payment-result."""

    session_id: str
    order_id: str
    status: str
    transactions: list[Transaction] = field(default_factory=list)

    def first_credential(self) -> Optional[LineItem]:
        """Return the first line item currently carrying a one-time card, if any."""
        for txn in self.transactions:
            for item in txn.line_items:
                if item.has_credential():
                    return item
        return None

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_SESSION_STATUSES


def _parse_line_item(data: dict[str, Any]) -> LineItem:
    return LineItem(
        txn_ref_id=data["txn_ref_id"],
        merchant_name=data["merchant_name"],
        merchant_url=data["merchant_url"],
        total_amount=data["total_amount"],
        status=data["status"],
        token=data.get("token"),
        dynamic_cvv=data.get("dynamic_cvv"),
        expiry_month=data.get("expiry_month"),
        expiry_year=data.get("expiry_year"),
        products=data.get("products", []),
    )


def _parse_transaction(data: dict[str, Any]) -> Transaction:
    return Transaction(
        txn_id=data["txn_id"],
        status=data["status"],
        line_items=[_parse_line_item(li) for li in data.get("line_items", [])],
        error=data.get("error"),
    )


def _parse_session(data: dict[str, Any]) -> Session:
    return Session(
        session_id=data["session_id"],
        session_token=data["session_token"],
        iframe_url=data["iframe_url"],
        order_id=data["order_id"],
        expires_at=data["expires_at"],
    )


def _parse_payment_result(data: dict[str, Any]) -> PaymentResult:
    return PaymentResult(
        session_id=data["session_id"],
        order_id=data["order_id"],
        status=data["status"],
        transactions=[_parse_transaction(t) for t in data.get("transactions", [])],
    )


class PravaClient:
    """Sync REST client for the Prava payments API."""

    def __init__(
        self,
        secret_key: Optional[str] = None,
        backend_url: Optional[str] = None,
        timeout: float = 30.0,
        client: Optional[httpx.Client] = None,
    ):
        self.secret_key = secret_key or os.environ.get("PRAVA_SECRET_KEY")
        self.backend_url = (backend_url or os.environ.get("PRAVA_BACKEND_URL") or DEFAULT_BACKEND_URL).rstrip("/")
        if not self.secret_key:
            raise PravaError("PRAVA_SECRET_KEY is not set (env or constructor arg required)")

        self._owns_client = client is None
        # NOTE: Content-Type is deliberately NOT a client-wide default header.
        # httpx sets it per-request whenever `json=` is passed, so bodyless
        # requests (the payment-result GET) never advertise a JSON body they
        # do not have -- the sandbox rejects that pairing
        # (FST_ERR_CTP_EMPTY_JSON_BODY).
        self._client = client or httpx.Client(
            base_url=self.backend_url,
            headers={"Authorization": f"Bearer {self.secret_key}"},
            timeout=timeout,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "PravaClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise PravaError(f"Network error calling Prava API: {exc}") from exc

        if response.status_code >= 300:
            code = None
            message = f"Prava API returned HTTP {response.status_code}"
            details = None
            try:
                body = response.json()
                # The documented shape is {"error": {code, message, details}},
                # but preserve useful diagnostics if a sandbox route returns
                # the error object at the top level instead.
                err = body.get("error", body) if isinstance(body, dict) else {}
                if isinstance(err, dict):
                    code = err.get("code") or body.get("code")
                    message = err.get("message") or body.get("message") or message
                    details = err.get("details") or body.get("details")
            except ValueError:
                pass
            raise PravaError(message, code=code, http_status=response.status_code, details=details)

        if not response.content:
            return {}
        return response.json()

    def create_session(
        self,
        user_id: str,
        user_email: str,
        total_amount: str,
        currency: str,
        merchant_name: str,
        merchant_url: str,
        country_code_iso2: str,
        product_details: list[dict[str, Any]],
        description: Optional[str] = None,
        effective_until_minutes: int = 15,
        integration_type: str = "full_checkout",
    ) -> Session:
        """Create a payment session (POST /v1/sessions).

        `product_details` is a list of dicts each shaped like
        `{"description": str, "unit_price": str, "quantity": int}`.

        `user_email` and `merchant_url` are validated before any network call:
        both are forwarded to the card network, and a reserved/fake TLD or a
        non-origin merchant URL fails only at the very last step of checkout.
        `merchant_url` is normalized down to its bare https origin.
        """
        user_email = validate_user_email(user_email)
        merchant_url = normalize_merchant_url(merchant_url)
        country_code_iso2 = (country_code_iso2 or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", country_code_iso2):
            raise PravaError(
                "country_code_iso2 must be a two-letter ISO country code.",
                code="INVALID_MERCHANT_COUNTRY",
            )
        payload: dict[str, Any] = {
            "user_id": user_id,
            "user_email": user_email,
            "total_amount": total_amount,
            "currency": currency,
            "integration_type": integration_type,
            "purchase_context": [
                {
                    "merchant_details": {
                        "name": merchant_name,
                        "url": merchant_url,
                        "country_code_iso2": country_code_iso2,
                    },
                    "product_details": product_details,
                    "effective_until_minutes": effective_until_minutes,
                }
            ],
        }
        if description is not None:
            payload["description"] = description

        data = self._request("POST", "/v1/sessions", json=payload)
        return _parse_session(data)

    def get_payment_result(self, session_id: str) -> PaymentResult:
        """Poll GET /v1/sessions/{session_id}/payment-result."""
        data = self._request("GET", f"/v1/sessions/{session_id}/payment-result")
        return _parse_payment_result(data)

    def wait_for_result(
        self,
        session_id: str,
        timeout_seconds: float = 120.0,
        poll_interval: float = 3.0,
    ) -> PaymentResult:
        """Poll get_payment_result until a credential appears, the session
        reaches a terminal status, or timeout_seconds elapses.

        Returns the last PaymentResult observed (which may still be
        pending/awaiting_result with no credential if the timeout hit).
        """
        import time

        deadline = time.monotonic() + timeout_seconds
        result = self.get_payment_result(session_id)
        while True:
            if result.first_credential() is not None or result.is_terminal():
                return result
            if time.monotonic() >= deadline:
                return result
            time.sleep(poll_interval)
            result = self.get_payment_result(session_id)

    def report_status(self, session_id: str, txn_ref_id: str, status: str) -> None:
        """POST /v1/sessions/{session_id}/report-status.

        `status` must be "APPROVED" or "DECLINED". This MUST always be
        called after a checkout attempt or the transaction sticks.
        """
        if status not in VALID_REPORT_STATUSES:
            raise PravaError(
                f"Invalid txn_status {status!r}; must be one of {sorted(VALID_REPORT_STATUSES)}"
            )
        self._request(
            "POST",
            f"/v1/sessions/{session_id}/report-status",
            json={"txn_ref_id": txn_ref_id, "txn_status": status},
        )

    def revoke_session(self, session_id: str) -> dict[str, Any]:
        """POST /v1/sessions/{session_id}/revoke -- immediately invalidate a session.

        Sends an explicit empty JSON object body -- the live sandbox rejects
        a request with `Content-Type: application/json` and a literally
        empty body (FST_ERR_CTP_EMPTY_JSON_BODY).

        Returns the raw response body (shape not yet formalized into a
        dataclass since this endpoint is newly validated against the live
        sandbox).
        """
        return self._request("POST", f"/v1/sessions/{session_id}/revoke", json={})
