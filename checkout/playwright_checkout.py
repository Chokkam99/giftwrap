"""Playwright-driven Shopify checkout completion (WP3, Agentic Gifting).

Prava mints a one-time virtual card (16-digit token + dynamic CVV + expiry).
There is no pure-API way to spend that card at a Shopify checkout, so this
module drives the real checkout page in a browser and fills the card in.

Target is ALWAYS a Shopify **development store** we control, running a test
payment gateway. Purchases against real merchant storefronts are refused by
``assert_purchase_allowed`` -- catalog/browse against real merchants happens
elsewhere (UCP), never here.

Public surface:
    run_shopify_checkout(...) -> CheckoutResult    # full purchase
    verify_store(...)         -> dict              # read-only store probe
    CheckoutResult, CheckoutError

Everything above the "browser automation" banner is pure logic with no
Playwright dependency, so it can be unit-tested without a browser.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Optional, Sequence
from urllib.parse import urlparse

try:  # optional: keeps .env working for CLI use, never required
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass


__all__ = [
    "CheckoutError",
    "CheckoutResult",
    "run_shopify_checkout",
    "verify_store",
    "assert_purchase_allowed",
    "classify_outcome",
    "extract_order_id",
    "detect_decline",
    "mask_pan",
]


# ---------------------------------------------------------------------------
# Errors / result types
# ---------------------------------------------------------------------------


class CheckoutError(RuntimeError):
    """Infrastructure/setup problem -- NOT a payment decline.

    Raised for missing configuration, a missing browser, a password-gated
    storefront with no password, or a disallowed (non dev-store) target.
    Callers use this to tell "our setup is broken" apart from "the card was
    declined", which comes back as a ``CheckoutResult`` instead.
    """


STATUS_APPROVED = "APPROVED"
STATUS_DECLINED = "DECLINED"
STATUS_FAILED = "FAILED"


@dataclass
class CheckoutResult:
    """Outcome of a checkout attempt."""

    success: bool
    order_id: Optional[str]
    status: str  # "APPROVED" | "DECLINED" | "FAILED"
    message: str
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "order_id": self.order_id,
            "status": self.status,
            "message": self.message,
            "details": self.details,
        }


def build_result(
    status: str,
    message: str,
    order_id: Optional[str] = None,
    details: Optional[dict] = None,
) -> CheckoutResult:
    """Map a status string to a fully-formed CheckoutResult."""
    if status not in (STATUS_APPROVED, STATUS_DECLINED, STATUS_FAILED):
        raise ValueError(f"unknown checkout status: {status!r}")
    return CheckoutResult(
        success=status == STATUS_APPROVED,
        order_id=order_id if status == STATUS_APPROVED else None,
        status=status,
        message=message,
        details=details or {},
    )


# ---------------------------------------------------------------------------
# Pure logic: env, masking, guards
# ---------------------------------------------------------------------------

ENV_PRODUCT_URL = "SHOPIFY_DEV_STORE_PRODUCT_URL"
ENV_STOREFRONT_PASSWORD = "SHOPIFY_STOREFRONT_PASSWORD"
ENV_HEADLESS = "CHECKOUT_HEADLESS"
ENV_BOGUS_GATEWAY = "BOGUS_GATEWAY"
ENV_ALLOW_ANY_HOST = "CHECKOUT_ALLOW_ANY_HOST"
ENV_ARTIFACT_DIR = "CHECKOUT_ARTIFACT_DIR"

# Hosts we must never attempt to buy from. These are live merchants used for
# read-only catalog browsing elsewhere in the product.
REAL_MERCHANT_DENYLIST = (
    "giva.co",
    "mamaearth.com",
    "nykaa.com",
    "myntra.com",
    "flipkart.com",
    "ajio.com",
    "amazon.",
    "tanishq.co.in",
    "caratlane.com",
    "bluestone.com",
)

TRUTHY = {"1", "true", "yes", "y", "on"}
FALSY = {"0", "false", "no", "n", "off"}


def env_bool(name: str, default: bool, env: Optional[dict] = None) -> bool:
    """Parse a boolean env var; unknown/blank values fall back to *default*."""
    raw = (env if env is not None else os.environ).get(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val in TRUTHY:
        return True
    if val in FALSY:
        return False
    return default


def resolve_headless(headless: Optional[bool] = None, env: Optional[dict] = None) -> bool:
    """Explicit argument wins; otherwise CHECKOUT_HEADLESS; otherwise True."""
    if headless is not None:
        return bool(headless)
    return env_bool(ENV_HEADLESS, True, env=env)


def resolve_product_url(product_url: Optional[str] = None, env: Optional[dict] = None) -> str:
    """Resolve the product URL from the argument or the environment."""
    source = env if env is not None else os.environ
    url = (product_url or source.get(ENV_PRODUCT_URL) or "").strip()
    if not url:
        raise CheckoutError(
            "No product URL. Pass product_url= or set "
            f"{ENV_PRODUCT_URL} to a dev-store product page."
        )
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise CheckoutError(f"Invalid product URL: {url!r}")
    if "your-store" in parsed.netloc or "your-product" in parsed.path:
        raise CheckoutError(
            f"{ENV_PRODUCT_URL} still holds the .env.example placeholder "
            f"({url!r}). Point it at the real dev store."
        )
    return url


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_dev_store_host(host: str) -> bool:
    """Shopify development stores always answer on *.myshopify.com."""
    return host.endswith(".myshopify.com")


def assert_purchase_allowed(url: str, env: Optional[dict] = None) -> None:
    """Refuse to spend money anywhere but our own dev store.

    Allowed: ``*.myshopify.com``. A custom dev-store domain can be opted in
    with ``CHECKOUT_ALLOW_ANY_HOST=1``, but the real-merchant denylist is
    absolute and cannot be overridden.
    """
    host = host_of(url)
    if not host:
        raise CheckoutError(f"Cannot determine host for checkout URL: {url!r}")
    for banned in REAL_MERCHANT_DENYLIST:
        if banned in host:
            raise CheckoutError(
                f"Refusing to run a checkout against real merchant host {host!r}. "
                "Live merchants are for catalog browsing only; buy on the "
                "Shopify development store."
            )
    if is_dev_store_host(host):
        return
    if env_bool(ENV_ALLOW_ANY_HOST, False, env=env):
        return
    raise CheckoutError(
        f"Host {host!r} is not a *.myshopify.com development store. "
        f"Set {ENV_ALLOW_ANY_HOST}=1 only if this is your own dev store on a "
        "custom domain."
    )


def mask_pan(pan: Optional[str]) -> str:
    """Mask a card number down to its last 4 digits. Never log the raw PAN."""
    digits = re.sub(r"\D", "", pan or "")
    if not digits:
        return "****"
    if len(digits) <= 4:
        return "*" * len(digits)
    return f"**** **** **** {digits[-4:]}"


def format_expiry(month: Any, year: Any) -> str:
    """Normalise expiry to the MMYY string Shopify's card field expects."""
    try:
        m = int(str(month).strip())
    except (TypeError, ValueError):
        raise CheckoutError(f"Invalid expiry month: {month!r}")
    if not 1 <= m <= 12:
        raise CheckoutError(f"Invalid expiry month: {month!r}")
    y_raw = str(year).strip()
    if not y_raw.isdigit() or len(y_raw) not in (2, 4):
        raise CheckoutError(f"Invalid expiry year: {year!r}")
    yy = y_raw[-2:]
    return f"{m:02d}{yy}"


def bogus_gateway_pan(env: Optional[dict] = None) -> Optional[str]:
    """Return the Bogus Gateway simulation PAN, if that mode is enabled.

    Shopify's "(for testing) Bogus Gateway" ignores real PANs: card number
    ``1`` approves, ``2`` fails, ``3`` errors. Setting ``BOGUS_GATEWAY`` to one
    of those digits (or to a truthy value, which means ``1``) substitutes it in
    place of the Prava token. This is gateway *simulation* -- the real token is
    not exercised -- so it is a fallback, not the demo path.
    """
    raw = (env if env is not None else os.environ).get(ENV_BOGUS_GATEWAY)
    if raw is None:
        return None
    val = raw.strip().lower()
    if val in ("1", "2", "3"):
        return val
    if val in TRUTHY:
        return "1"
    return None


def effective_card_number(token: str, env: Optional[dict] = None) -> str:
    """The PAN actually typed into the card field."""
    bogus = bogus_gateway_pan(env=env)
    if bogus is not None:
        return bogus
    pan = re.sub(r"\s+", "", token or "")
    if not pan:
        raise CheckoutError("Empty card token supplied to checkout.")
    return pan


# ---------------------------------------------------------------------------
# Pure logic: outcome classification
# ---------------------------------------------------------------------------

DECLINE_MARKERS = (
    "declined",
    "card was declined",
    "your card was declined",
    "payment not processed",
    "payment was not processed",
    "payment could not be processed",
    "unable to process your payment",
    "we couldn't process your payment",
    "transaction was not authorized",
    "transaction not authorized",
    "insufficient funds",
    "do not honor",
    "do not honour",
    "card is not supported",
    "invalid card number",
    "incorrect security code",
    "authorization failed",
    "payment failed",
    "issuer declined",
    "exceeds the limit",
    "over the limit",
)

CONFIRMATION_URL_MARKERS = (
    "/thank_you",
    "/thank-you",
    "checkout_success",
    "/orders/",
    "order_status",
    "/order-confirmation",
)

_ORDER_ID_PATTERNS: Sequence[re.Pattern] = (
    re.compile(r"order\s*#\s*([A-Za-z0-9][A-Za-z0-9\-_]*)", re.I),
    re.compile(r"order\s+number\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9\-_]*)", re.I),
    re.compile(r"confirmation\s*(?:number)?\s*#\s*([A-Za-z0-9][A-Za-z0-9\-_]*)", re.I),
    re.compile(r"order\s+([A-Z]{0,4}#?\d{3,}[A-Za-z0-9\-]*)", re.I),
)

_ORDER_URL_PATTERN = re.compile(r"/orders/([A-Za-z0-9][A-Za-z0-9\-_]{5,})", re.I)

# Words that follow "Order " in ordinary checkout copy ("Order summary",
# "Order updates") and must never be mistaken for an order id.
_ORDER_ID_STOPWORDS = {
    "summary",
    "updates",
    "update",
    "details",
    "detail",
    "confirmation",
    "confirmed",
    "status",
    "history",
    "number",
    "placed",
    "again",
    "total",
    "items",
    "info",
    "information",
    "tracking",
    "notes",
    "note",
    "date",
    "id",
    "is",
    "was",
    "has",
    "and",
    "for",
}


def _clean_order_id(candidate: str) -> Optional[str]:
    value = (candidate or "").strip().strip("#.,:;)")
    if not value:
        return None
    if value.lower() in _ORDER_ID_STOPWORDS:
        return None
    # An id made only of letters is almost certainly prose, not an order id.
    if not any(ch.isdigit() for ch in value):
        return None
    return value


def extract_order_id(body_text: str = "", url: str = "") -> Optional[str]:
    """Pull an order id out of thank-you page text (or, failing that, the URL)."""
    text = body_text or ""
    for pattern in _ORDER_ID_PATTERNS:
        for match in pattern.finditer(text):
            cleaned = _clean_order_id(match.group(1))
            if cleaned:
                return cleaned
    match = _ORDER_URL_PATTERN.search(url or "")
    if match:
        cleaned = (match.group(1) or "").strip()
        if cleaned:
            return cleaned
    return None


def detect_decline(body_text: str) -> Optional[str]:
    """Return the decline phrase found in the page text, if any."""
    text = (body_text or "").lower()
    for marker in DECLINE_MARKERS:
        if marker in text:
            return marker
    return None


def is_confirmation_url(url: str) -> bool:
    lowered = (url or "").lower()
    return any(marker in lowered for marker in CONFIRMATION_URL_MARKERS)


def looks_confirmed(url: str, body_text: str) -> bool:
    """True when the page is convincingly an order-confirmation page."""
    if is_confirmation_url(url):
        return True
    text = (body_text or "").lower()
    if extract_order_id(body_text, url) and (
        "thank you" in text
        or "order confirmed" in text
        or "your order is confirmed" in text
    ):
        return True
    return False


def classify_outcome(url: str, body_text: str) -> CheckoutResult:
    """Turn a post-payment page into APPROVED / DECLINED / FAILED.

    Confirmation evidence wins over decline wording, because a thank-you page
    can legitimately mention declines in unrelated copy; a decline banner on a
    non-confirmation page means the card was refused; anything else means we
    never reached a decision and is an infrastructure/flow failure.
    """
    order_id = extract_order_id(body_text, url)
    details = {"final_url": url}
    if looks_confirmed(url, body_text):
        return build_result(
            STATUS_APPROVED,
            f"Order placed{f' ({order_id})' if order_id else ''}.",
            order_id=order_id,
            details=details,
        )
    marker = detect_decline(body_text)
    if marker:
        return build_result(
            STATUS_DECLINED,
            f"Payment declined by the gateway (matched: {marker!r}).",
            details={**details, "decline_marker": marker},
        )
    return build_result(
        STATUS_FAILED,
        "Checkout did not reach an order confirmation and showed no decline "
        "message; treating as failed.",
        details=details,
    )


# ---------------------------------------------------------------------------
# Pure logic: shipping address
# ---------------------------------------------------------------------------

DEFAULT_SHIPPING = {
    "first_name": "Aarav",
    "last_name": "Sharma",
    "address1": "12 MG Road, Shanthala Nagar",
    "address2": "",
    "city": "Bengaluru",
    "province": "Karnataka",
    "province_code": "KA",
    "zip": "560001",
    "country": "India",
    "country_code": "IN",
    "phone": "+919876543210",
}


def shipping_address(overrides: Optional[dict] = None, env: Optional[dict] = None) -> dict:
    """Default Indian shipping address, overridable via CHECKOUT_SHIP_* env."""
    source = env if env is not None else os.environ
    address = dict(DEFAULT_SHIPPING)
    for key in address:
        value = source.get(f"CHECKOUT_SHIP_{key.upper()}")
        if value:
            address[key] = value
    if overrides:
        address.update({k: v for k, v in overrides.items() if v is not None})
    return address


# ---------------------------------------------------------------------------
# Selector tables (Shopify one-page checkout first, PSP iframes as fallback)
# ---------------------------------------------------------------------------

PASSWORD_GATE_LINK = (
    'a:has-text("Enter using password")',
    'button:has-text("Enter using password")',
    '[href*="/password"]:has-text("password")',
)
PASSWORD_INPUTS = (
    'form[action*="/password"] input[type="password"]',
    'input[name="password"]',
    'input[type="password"]',
)
PASSWORD_SUBMITS = (
    'form[action*="/password"] button[type="submit"]',
    'button:has-text("Enter")',
    'button[type="submit"]',
    'input[type="submit"]',
)

ADD_TO_CART_SELECTORS = (
    'button[name="add"]',
    'form[action*="/cart/add"] button[type="submit"]',
    'form[action*="/cart/add"] input[type="submit"]',
    ".product-form__submit",
    '[data-testid="add-to-cart"]',
    'button:has-text("Add to cart")',
    'button:has-text("Add to bag")',
    'button:has-text("ADD TO CART")',
)

CHECKOUT_BUTTON_SELECTORS = (
    'button[name="checkout"]',
    'input[name="checkout"]',
    'a[href="/checkout"]',
    'a[href*="/checkout"]',
    '[data-testid="Checkout-button"]',
    '#checkout',
    'button:has-text("Check out")',
    'button:has-text("Checkout")',
    'a:has-text("Check out")',
)

EMAIL_SELECTORS = (
    'input[name="email"]',
    "input#email",
    'input[autocomplete="email"]',
    'input[type="email"]',
    'input[placeholder*="email" i]',
)

SHIPPING_SELECTORS = {
    "first_name": (
        'input[autocomplete="given-name"]',
        'input[name="firstName"]',
        'input[name*="first_name" i]',
        "input#TextField0",
    ),
    "last_name": (
        'input[autocomplete="family-name"]',
        'input[name="lastName"]',
        'input[name*="last_name" i]',
    ),
    "address1": (
        'input[autocomplete="address-line1"]',
        'input[name="address1"]',
        'input[name*="address1" i]',
        'input[placeholder*="address" i]',
    ),
    "address2": (
        'input[autocomplete="address-line2"]',
        'input[name="address2"]',
        'input[name*="address2" i]',
    ),
    "city": (
        'input[autocomplete="address-level2"]',
        'input[name="city"]',
        'input[name*="city" i]',
    ),
    "zip": (
        'input[autocomplete="postal-code"]',
        'input[name="postalCode"]',
        'input[name*="zip" i]',
        'input[name*="postal" i]',
    ),
    "phone": (
        'input[autocomplete="tel"]',
        'input[name="phone"]',
        'input[type="tel"]',
    ),
}

COUNTRY_SELECT_SELECTORS = (
    'select[autocomplete="country"]',
    'select[name="countryCode"]',
    'select[name*="country" i]',
)
PROVINCE_SELECT_SELECTORS = (
    'select[autocomplete="address-level1"]',
    'select[name="zone"]',
    'select[name*="province" i]',
    'select[name*="state" i]',
)

CARD_NUMBER_SELECTORS = (
    'input[autocomplete="cc-number"]',
    'input[name="number"]',
    "input#number",
    'input[name="cardnumber"]',
    'input[name="cardNumber"]',
    "input#credit-card-number",
    'input[id*="card-number" i]',
    'input[data-elo="cc-number"]',
    'input[placeholder*="card number" i]',
    'input[aria-label*="card number" i]',
)
CARD_EXPIRY_SELECTORS = (
    'input[autocomplete="cc-exp"]',
    'input[name="expiry"]',
    "input#expiry",
    'input[name="exp-date"]',
    'input[name="expirationDate"]',
    "input#expiration",
    'input[id*="expir" i]',
    'input[placeholder*="MM / YY" i]',
    'input[aria-label*="expiration" i]',
)
CARD_CVV_SELECTORS = (
    'input[autocomplete="cc-csc"]',
    'input[name="verification_value"]',
    "input#verification_value",
    'input[name="cvc"]',
    'input[name="cvv"]',
    "input#cvv",
    'input[id*="cvv" i]',
    'input[id*="security-code" i]',
    'input[placeholder*="security code" i]',
    'input[aria-label*="security code" i]',
)
CARD_NAME_SELECTORS = (
    'input[autocomplete="cc-name"]',
    'input[name="name"]',
    "input#name",
    'input[name="cardholder"]',
    'input[id*="name-on-card" i]',
    'input[placeholder*="name on card" i]',
)

# Frame name/url hints for embedded card fields (Shopify's own card-fields-*,
# plus Stripe/Braintree style hosted fields).
CARD_FRAME_HINTS = (
    "card-fields",
    "card_fields",
    "cardnumber",
    "card-number",
    "checkout.shopifycs.com",
    "stripe",
    "braintree",
    "hosted-field",
    "securefields",
)

CONTINUE_SELECTORS = (
    "#continue_button",
    'button:has-text("Continue to shipping")',
    'button:has-text("Continue to payment")',
    'button:has-text("Continue to payment method")',
    'button:has-text("Continue")',
    'a:has-text("Continue to shipping")',
    'a:has-text("Continue to payment")',
)

PAY_SELECTORS = (
    "#checkout-pay-button",
    'button:has-text("Pay now")',
    'button:has-text("Complete order")',
    'button:has-text("Complete purchase")',
    'button:has-text("Pay ")',
    '[data-testid="pay-button"]',
    'button[type="submit"]:has-text("Pay")',
)


# ---------------------------------------------------------------------------
# Browser automation
# ---------------------------------------------------------------------------

SHORT_TIMEOUT_MS = 2500


def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover - depends on install state
        raise CheckoutError(
            "Playwright is not installed. Run: pip install playwright && "
            "playwright install chromium"
        ) from exc
    return sync_playwright


def _launch(sync_playwright, headless: bool, timeout_ms: int):
    """Start Chromium; surface a CheckoutError if browsers were never installed."""
    manager = sync_playwright().start()
    try:
        browser = manager.chromium.launch(headless=headless)
    except Exception as exc:  # pragma: no cover - environment dependent
        manager.stop()
        raise CheckoutError(
            f"Could not launch Chromium ({exc}). Run: playwright install chromium"
        ) from exc
    context = browser.new_context(
        locale="en-IN",
        viewport={"width": 1440, "height": 1000},
    )
    context.set_default_timeout(timeout_ms)
    context.set_default_navigation_timeout(timeout_ms)
    return manager, browser, context


def _scopes(page) -> Iterator[Any]:
    """The page itself, then every iframe, card iframes first."""
    yield page
    frames = [f for f in page.frames if f is not page.main_frame]
    hinted = [f for f in frames if _frame_is_card_like(f)]
    rest = [f for f in frames if f not in hinted]
    for frame in hinted + rest:
        yield frame


def _frame_is_card_like(frame) -> bool:
    blob = f"{getattr(frame, 'name', '') or ''} {getattr(frame, 'url', '') or ''}".lower()
    return any(hint in blob for hint in CARD_FRAME_HINTS)


def _find(scope, selectors: Iterable[str], timeout: int = SHORT_TIMEOUT_MS):
    """First visible element matching any selector in *scope*, else None.

    One combined wait decides whether anything matches at all (keeps the
    absent-field case fast), then a quick pass honours selector priority
    instead of DOM order.
    """
    selectors = tuple(selectors)
    if not selectors:
        return None
    combined = ", ".join(selectors)
    try:
        scope.locator(combined).first.wait_for(state="visible", timeout=timeout)
    except Exception:
        return None
    for selector in selectors:
        locator = scope.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=250)
            return locator
        except Exception:
            continue
    return scope.locator(combined).first


def _find_anywhere(page, selectors: Iterable[str], timeout: int = SHORT_TIMEOUT_MS):
    """Search the page and every iframe. Returns (locator, scope) or (None, None).

    The main page gets the full timeout; iframes are polled briefly, since by
    the time we look at them they are already loaded.
    """
    selectors = tuple(selectors)
    for index, scope in enumerate(_scopes(page)):
        locator = _find(scope, selectors, timeout=timeout if index == 0 else 700)
        if locator is not None:
            return locator, scope
    return None, None


def _is_enabled(locator) -> bool:
    try:
        return locator.is_enabled(timeout=SHORT_TIMEOUT_MS)
    except Exception:
        return True


def _type_into(locator, value: str) -> bool:
    """Fill a field, falling back to keystroke entry for masked PSP inputs."""
    try:
        locator.click(timeout=SHORT_TIMEOUT_MS)
    except Exception:
        pass
    try:
        locator.fill(value, timeout=SHORT_TIMEOUT_MS)
        return True
    except Exception:
        pass
    try:
        press = getattr(locator, "press_sequentially", None) or locator.type
        press(value, delay=40)
        return True
    except Exception:
        return False


def _fill_anywhere(page, selectors: Iterable[str], value: str, timeout: int = SHORT_TIMEOUT_MS) -> bool:
    locator, _scope = _find_anywhere(page, selectors, timeout=timeout)
    if locator is None:
        return False
    return _type_into(locator, value)


def _select_option(locator, values: Sequence[str]) -> bool:
    for value in values:
        for kwargs in ({"value": value}, {"label": value}):
            try:
                locator.select_option(timeout=SHORT_TIMEOUT_MS, **kwargs)
                return True
            except Exception:
                continue
    return False


def _click(locator, page=None, timeout: int = SHORT_TIMEOUT_MS * 2) -> bool:
    try:
        locator.click(timeout=timeout)
        return True
    except Exception:
        try:
            locator.click(timeout=timeout, force=True)
            return True
        except Exception:
            return False


def _body_text(page) -> str:
    try:
        return page.inner_text("body", timeout=SHORT_TIMEOUT_MS * 2)
    except Exception:
        try:
            return page.content()
        except Exception:
            return ""


def _settle(page, ms: int = 1200) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=ms * 5)
    except Exception:
        pass
    try:
        page.wait_for_timeout(ms)
    except Exception:
        pass


def _screenshot(page, label: str, details: dict) -> None:
    directory = os.environ.get(ENV_ARTIFACT_DIR)
    if not directory:
        import tempfile

        directory = tempfile.gettempdir()
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"checkout-{label}-{int(time.time())}.png")
        page.screenshot(path=path, full_page=True)
        details.setdefault("screenshots", []).append(path)
    except Exception:
        pass


# --- flow steps -------------------------------------------------------------


def _handle_password_gate(page, url: str, password: Optional[str], steps: list) -> None:
    """Dawn-theme dev stores hide the storefront behind a password form."""
    on_gate = "/password" in (page.url or "").lower()
    gate_input = None
    if not on_gate:
        gate_input = _find(page, PASSWORD_INPUTS, timeout=1200)
        # A visible password field on a product URL means we were bounced to
        # the gate even if the URL was rewritten.
        on_gate = gate_input is not None
    if not on_gate:
        return

    if not password:
        raise CheckoutError(
            "Storefront is password-protected but "
            f"{ENV_STOREFRONT_PASSWORD} is not set. Either disable the "
            "password in Online Store > Preferences or put it in .env."
        )

    link = _find(page, PASSWORD_GATE_LINK, timeout=1500)
    if link is not None:
        _click(link, page)
        page.wait_for_timeout(500)

    field = gate_input or _find(page, PASSWORD_INPUTS, timeout=SHORT_TIMEOUT_MS)
    if field is None:
        raise CheckoutError("Password gate detected but no password input was found.")
    _type_into(field, password)
    submit = _find(page, PASSWORD_SUBMITS, timeout=1500)
    if submit is not None:
        _click(submit, page)
    else:
        try:
            field.press("Enter")
        except Exception:
            pass
    _settle(page)
    steps.append("password_gate_unlocked")

    if "/password" in (page.url or "").lower():
        raise CheckoutError(
            "Storefront password was rejected (still on the password page). "
            f"Check {ENV_STOREFRONT_PASSWORD}."
        )
    page.goto(url, wait_until="domcontentloaded")
    _settle(page)


def _open_product_page(page, url: str, password: Optional[str], steps: list) -> None:
    page.goto(url, wait_until="domcontentloaded")
    _settle(page)
    _handle_password_gate(page, url, password, steps)
    steps.append("product_page_loaded")


def _add_to_cart(page, steps: list) -> None:
    button = _find(page, ADD_TO_CART_SELECTORS, timeout=SHORT_TIMEOUT_MS * 2)
    if button is None:
        raise CheckoutError(
            "No add-to-cart button found on the product page. Is the URL a "
            "product page, and is the variant in stock?"
        )
    if not _is_enabled(button):
        raise CheckoutError(
            "The add-to-cart button is disabled -- the variant is sold out or "
            "unavailable. Untick 'Track quantity' on the dev-store product."
        )
    if not _click(button, page):
        raise CheckoutError("Add-to-cart button could not be clicked.")
    _settle(page)
    steps.append("added_to_cart")


def _go_to_checkout(page, product_url: str, steps: list) -> None:
    """Click through the cart drawer/page, falling back to /checkout directly."""
    button = _find(page, CHECKOUT_BUTTON_SELECTORS, timeout=SHORT_TIMEOUT_MS * 2)
    if button is not None and _click(button, page):
        _settle(page)
    if "/checkout" not in (page.url or "").lower():
        parsed = urlparse(product_url)
        page.goto(f"{parsed.scheme}://{parsed.netloc}/checkout", wait_until="domcontentloaded")
        _settle(page)
    url = (page.url or "").lower()
    text = _body_text(page).lower()
    if "your cart is empty" in text or "cart is empty" in text:
        raise CheckoutError(
            "Checkout reports an empty cart -- add-to-cart did not stick. "
            "Check that the variant is in stock and purchasable."
        )
    if "/checkout" not in url:
        raise CheckoutError(
            f"Could not reach the checkout (stuck at {page.url}). The cart may "
            "be empty or the store may require an account."
        )
    steps.append("checkout_opened")


def _fill_contact(page, email: str, steps: list) -> None:
    if _fill_anywhere(page, EMAIL_SELECTORS, email, timeout=SHORT_TIMEOUT_MS * 2):
        steps.append("contact_email_filled")
    else:
        steps.append("contact_email_skipped")


def _fill_shipping(page, address: dict, steps: list) -> None:
    filled = []
    # Country first: it repopulates the province list.
    locator, _scope = _find_anywhere(page, COUNTRY_SELECT_SELECTORS, timeout=SHORT_TIMEOUT_MS)
    if locator is not None and _select_option(
        locator, [address["country_code"], address["country"]]
    ):
        filled.append("country")
        try:
            page.wait_for_timeout(700)
        except Exception:
            pass

    for key, selectors in SHIPPING_SELECTORS.items():
        value = address.get(key, "")
        if not value:
            continue
        if _fill_anywhere(page, selectors, value):
            filled.append(key)

    locator, _scope = _find_anywhere(page, PROVINCE_SELECT_SELECTORS, timeout=SHORT_TIMEOUT_MS)
    if locator is not None and _select_option(
        locator, [address["province_code"], address["province"]]
    ):
        filled.append("province")

    if not filled:
        raise CheckoutError(
            "No shipping fields were found on the checkout page -- the page "
            "layout is unrecognised or checkout requires an account."
        )
    steps.append(f"shipping_filled:{','.join(filled)}")


def _card_field_present(page) -> bool:
    locator, _scope = _find_anywhere(page, CARD_NUMBER_SELECTORS, timeout=1200)
    return locator is not None


def _advance_to_payment(page, steps: list, max_steps: int = 3) -> None:
    """One-page checkout needs nothing; legacy checkouts need Continue clicks."""
    for _ in range(max_steps):
        if _card_field_present(page):
            return
        button = _find(page, CONTINUE_SELECTORS, timeout=1500)
        if button is None:
            return
        if not _click(button, page):
            return
        _settle(page)
        steps.append("continue_clicked")


def _fill_payment(page, pan: str, expiry: str, cvv: str, holder: str, steps: list) -> None:
    if not _fill_anywhere(page, CARD_NUMBER_SELECTORS, pan, timeout=SHORT_TIMEOUT_MS * 2):
        raise CheckoutError(
            "Card number field not found (checked the page and every iframe). "
            "Make sure a test payment gateway is enabled on the dev store."
        )
    filled = ["number"]
    if _fill_anywhere(page, CARD_EXPIRY_SELECTORS, expiry):
        filled.append("expiry")
    if _fill_anywhere(page, CARD_CVV_SELECTORS, cvv):
        filled.append("cvv")
    if holder and _fill_anywhere(page, CARD_NAME_SELECTORS, holder):
        filled.append("name")
    if "expiry" not in filled or "cvv" not in filled:
        raise CheckoutError(
            f"Incomplete card form (filled: {', '.join(filled)}). Expiry and "
            "security code fields were not found."
        )
    steps.append(f"card_filled:{','.join(filled)}")


def _submit_payment(page, steps: list) -> None:
    button = _find(page, PAY_SELECTORS, timeout=SHORT_TIMEOUT_MS * 2)
    if button is None:
        raise CheckoutError("Pay/Complete-order button not found on the payment step.")
    if not _click(button, page):
        raise CheckoutError("Pay button could not be clicked.")
    steps.append("payment_submitted")


def _await_outcome(page, deadline: float, details: dict) -> CheckoutResult:
    """Poll the page until it confirms, declines, or we run out of time."""
    last = build_result(STATUS_FAILED, "Checkout outcome was never determined.", details=details)
    while time.time() < deadline:
        _settle(page, ms=800)
        url = page.url or ""
        text = _body_text(page)
        result = classify_outcome(url, text)
        if result.status in (STATUS_APPROVED, STATUS_DECLINED):
            result.details.update(details)
            result.details["final_url"] = url
            return result
        last = result
        try:
            page.wait_for_timeout(1500)
        except Exception:
            break
    last.details.update(details)
    last.details["final_url"] = page.url or ""
    return last


# --- public entry points ----------------------------------------------------


def run_shopify_checkout(
    token: str,
    dynamic_cvv: str,
    expiry_month: Any,
    expiry_year: Any,
    product_url: Optional[str] = None,
    contact_email: str = "gifting-demo@example.com",
    headless: Optional[bool] = None,
    timeout_ms: int = 60000,
    address_overrides: Optional[dict] = None,
    dry_run: bool = False,
) -> CheckoutResult:
    """Buy the product at *product_url* with a one-time virtual card.

    Args:
        token: 16-digit virtual card number minted by Prava (never logged raw).
        dynamic_cvv: the card's dynamic CVV.
        expiry_month / expiry_year: card expiry (year 2- or 4-digit).
        product_url: dev-store product page; defaults to
            ``SHOPIFY_DEV_STORE_PRODUCT_URL``.
        contact_email: email used for the order.
        headless: overrides ``CHECKOUT_HEADLESS`` (default headless).
        timeout_ms: per-operation timeout and overall outcome budget.
        address_overrides: partial shipping-address overrides.
        dry_run: fill everything but never click Pay (safe rehearsal).

    Returns:
        CheckoutResult with status APPROVED / DECLINED / FAILED.

    Raises:
        CheckoutError: for setup/infrastructure problems (missing config,
            missing browser, password gate without password, disallowed host).
            Payment declines are returned, not raised.
    """
    url = resolve_product_url(product_url)
    assert_purchase_allowed(url)
    is_headless = resolve_headless(headless)
    pan = effective_card_number(token)
    expiry = format_expiry(expiry_month, expiry_year)
    cvv = str(dynamic_cvv or "").strip()
    if not cvv:
        raise CheckoutError("Empty CVV supplied to checkout.")
    address = shipping_address(address_overrides)
    holder = f"{address['first_name']} {address['last_name']}".strip()
    password = os.environ.get(ENV_STOREFRONT_PASSWORD) or None

    steps: list = []
    details: dict = {
        "product_url": url,
        "card_last4": mask_pan(pan),
        "expiry": f"{expiry[:2]}/**",
        "headless": is_headless,
        "bogus_gateway": bogus_gateway_pan() is not None,
        "dry_run": dry_run,
        "steps": steps,
    }

    sync_playwright = _import_playwright()
    manager, browser, context = _launch(sync_playwright, is_headless, timeout_ms)
    page = context.new_page()
    deadline = time.time() + (timeout_ms / 1000.0)
    try:
        _open_product_page(page, url, password, steps)
        _add_to_cart(page, steps)
        _go_to_checkout(page, url, steps)
        _fill_contact(page, contact_email, steps)
        _fill_shipping(page, address, steps)
        _advance_to_payment(page, steps)
        _fill_payment(page, pan, expiry, cvv, holder, steps)

        if dry_run:
            _screenshot(page, "dry-run", details)
            steps.append("dry_run_stopped_before_pay")
            return build_result(
                STATUS_FAILED,
                "Dry run: checkout was filled but payment was not submitted.",
                details=details,
            )

        _submit_payment(page, steps)
        result = _await_outcome(page, max(deadline, time.time() + 45), details)
        _screenshot(page, result.status.lower(), details)
        return result
    except CheckoutError:
        _screenshot(page, "error", details)
        raise
    except Exception as exc:  # unexpected page behaviour -> FAILED, not a crash
        _screenshot(page, "exception", details)
        return build_result(
            STATUS_FAILED,
            f"Checkout automation error: {type(exc).__name__}: {exc}",
            details={**details, "final_url": page.url or ""},
        )
    finally:
        for closer in (context.close, browser.close, manager.stop):
            try:
                closer()
            except Exception:
                pass


def verify_store(
    product_url: Optional[str] = None,
    headless: Optional[bool] = None,
    timeout_ms: int = 45000,
) -> dict:
    """Read-only probe: does the product page load and expose add-to-cart?

    Buys nothing and submits nothing, so it is safe to point at any storefront.
    """
    url = resolve_product_url(product_url)
    is_headless = resolve_headless(headless)
    password = os.environ.get(ENV_STOREFRONT_PASSWORD) or None
    steps: list = []
    report: dict = {
        "ok": False,
        "url": url,
        "host": host_of(url),
        "is_dev_store": is_dev_store_host(host_of(url)),
        "title": None,
        "add_to_cart_found": False,
        "add_to_cart_enabled": False,
        "price_text": None,
        "steps": steps,
        "message": "",
    }

    sync_playwright = _import_playwright()
    manager, browser, context = _launch(sync_playwright, is_headless, timeout_ms)
    page = context.new_page()
    try:
        _open_product_page(page, url, password, steps)
        try:
            report["title"] = page.title()
        except Exception:
            pass
        report["final_url"] = page.url
        button = _find(page, ADD_TO_CART_SELECTORS, timeout=SHORT_TIMEOUT_MS * 2)
        report["add_to_cart_found"] = button is not None
        report["add_to_cart_enabled"] = button is not None and _is_enabled(button)
        price = _find(
            page,
            (
                ".price__regular .price-item",
                ".price-item--regular",
                "[data-price]",
                ".product__price",
                ".price",
            ),
            timeout=1500,
        )
        if price is not None:
            try:
                report["price_text"] = " ".join(price.inner_text().split())[:120]
            except Exception:
                pass
        report["ok"] = bool(report["add_to_cart_found"] and report["add_to_cart_enabled"])
        if report["ok"]:
            report["message"] = "Product page loaded and add-to-cart is available."
        elif report["add_to_cart_found"]:
            report["message"] = (
                "Add-to-cart button is present but DISABLED -- the variant is "
                "sold out or unavailable, so checkout would fail."
            )
        else:
            report["message"] = "Page loaded but no add-to-cart button was found."
        if not report["is_dev_store"]:
            report["message"] += (
                " NOTE: host is not *.myshopify.com, so purchases would be "
                "refused by assert_purchase_allowed."
            )
        return report
    finally:
        for closer in (context.close, browser.close, manager.stop):
            try:
                closer()
            except Exception:
                pass
