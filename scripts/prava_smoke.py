#!/usr/bin/env python3
"""Prava smoke script.

Default mode is DRY-RUN: builds the exact request payload for
POST /v1/sessions and prints it, without making any network call.

Real mode (actually hits the Prava sandbox) requires BOTH:
  1. the --real CLI flag, AND
  2. the environment variable PRAVA_ALLOW_REAL=1

If either guard is missing when --real is requested, this script prints
why and exits 1 rather than silently falling back to dry-run.

The sandbox has a 30-transaction/day cap shared across the team and is
reserved for orchestrator-run verification -- do not run --real mode
casually.

Real mode creates exactly one session, prints the iframe_url for a human
to approve, polls for a result, prints the masked one-time credential and
txn_ref_id, and then exits WITHOUT calling report-status -- the
orchestrator owns the full approve -> checkout -> report loop.

Handshake mode (--handshake, on top of --real + PRAVA_ALLOW_REAL=1) is a
one-shot, self-cleaning contract check: create a session, print its
response STRUCTURE (field names/types, values masked), poll
payment-result once, print its structure, then immediately revoke the
session and exit. No card data is ever entered, no mandate/charge
endpoint is called, and nothing but the created session's own IDs is
ever printed in the clear.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prava.client import (  # noqa: E402
    PravaClient,
    PravaError,
    _parse_payment_result,
    _parse_session,
    mask_secret,
)


def build_payload() -> dict:
    """Build a representative request payload for POST /v1/sessions."""
    return {
        "user_id": "smoke-test-user",
        "user_email": "smoke-test@example.com",
        "total_amount": "2999.00",
        "currency": "INR",
        "integration_type": "full_checkout",
        "description": "Prava client smoke test",
        "purchase_context": [
            {
                "merchant_details": {
                    "name": "Giva Jewelry",
                    "url": "https://giva.co",
                    "country_code_iso2": "IN",
                },
                "product_details": [
                    {
                        "description": "Sample gift item",
                        "unit_price": "2999.00",
                        "quantity": 1,
                    }
                ],
                "effective_until_minutes": 15,
            }
        ],
    }


def build_handshake_payload() -> dict:
    """Build the exact request payload for the one-shot handshake run."""
    return {
        "user_id": "giftwrap_demo",
        "user_email": "gifting-demo@example.com",
        "total_amount": "699.95",
        "currency": "USD",
        "integration_type": "full_checkout",
        "description": "GiftWrap contract validation",
        "purchase_context": [
            {
                "merchant_details": {
                    "name": "Agentic Gifting Demo",
                    "url": "https://agentic-gifting-demo.myshopify.com",
                    "country_code_iso2": "US",
                },
                "product_details": [
                    {
                        "description": "The Complete Snowboard",
                        "unit_price": "699.95",
                        "quantity": 1,
                    }
                ],
                "effective_until_minutes": 15,
            }
        ],
    }


# Fields whose real value is safe/useful to print in full (not sensitive).
_FULL_VALUE_KEYS = {"status", "currency", "order_id", "description"}
# Fields shown only as a short, non-reversible prefix.
_PREFIX_ONLY_KEYS = {"session_id"}


def _display_value(key: str, value: Any) -> str:
    """Render a scalar field's value for structure printing, masked by default."""
    if key in _FULL_VALUE_KEYS or key == "expires_at":
        return repr(value)
    if key in _PREFIX_ONLY_KEYS and isinstance(value, str):
        return f"{value[:8]!r}+'...' (len={len(value)})"
    if key == "iframe_url" and isinstance(value, str):
        parsed = urlparse(value)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path} [query+fragment omitted]"
    if value is None:
        return "None"
    if isinstance(value, bool):
        return repr(value)
    if isinstance(value, str):
        return f"<masked str, len={len(value)}>"
    if isinstance(value, (int, float)):
        return "<masked number>"
    return f"<masked {type(value).__name__}>"


def print_structure(data: Any, indent: int = 0) -> None:
    """Print field names + types recursively, masking sensitive values."""
    pad = "  " * indent
    if isinstance(data, dict):
        if not data:
            print(f"{pad}{{}} (empty dict)")
            return
        for key, value in data.items():
            if isinstance(value, dict):
                print(f"{pad}{key}: dict")
                print_structure(value, indent + 1)
            elif isinstance(value, list):
                print(f"{pad}{key}: list[{len(value)}]")
                print_structure(value, indent + 1)
            else:
                print(f"{pad}{key}: {type(value).__name__} = {_display_value(key, value)}")
    elif isinstance(data, list):
        if not data:
            print(f"{pad}(empty list)")
            return
        for i, item in enumerate(data):
            print(f"{pad}[{i}]:")
            print_structure(item, indent + 1)
    else:
        print(f"{pad}{data!r}")


def run_handshake() -> int:
    """One-shot real-sandbox contract check: create -> print structure ->
    poll once -> print structure -> revoke -> exit.

    Never enters card data, never calls mandate/charge endpoints, never
    prints the secret key, session_token, or full iframe_url.
    """
    payload = build_handshake_payload()
    client = PravaClient()
    session_id: str | None = None
    try:
        print("=" * 70)
        print(f"STEP 1: POST {client.backend_url}/v1/sessions (create_session)")
        print("=" * 70)
        raw_session = client._request("POST", "/v1/sessions", json=payload)
        print_structure(raw_session)

        session_id = raw_session.get("session_id")
        if not session_id:
            print("\nNo session_id in response -- cannot poll or revoke. Aborting.")
            return 1

        try:
            _parse_session(raw_session)
            print("\n[dataclass check] Session parsing: OK")
        except KeyError as exc:
            print(f"\n[dataclass check] Session parsing FAILED -- missing field {exc}")

        print("\n" + "=" * 70)
        print("STEP 2: GET /v1/sessions/{session_id}/payment-result (single poll)")
        print("=" * 70)
        raw_result = client._request("GET", f"/v1/sessions/{session_id}/payment-result")
        print(f"status = {raw_result.get('status')!r}")
        print_structure(raw_result)

        try:
            _parse_payment_result(raw_result)
            print("\n[dataclass check] PaymentResult parsing: OK")
        except KeyError as exc:
            print(f"\n[dataclass check] PaymentResult parsing FAILED -- missing field {exc}")

        print("\n" + "=" * 70)
        print("STEP 3: POST /v1/sessions/{session_id}/revoke")
        print("=" * 70)
        raw_revoke = client.revoke_session(session_id)
        print_structure(raw_revoke)
        print(f"\nSession {session_id[:8]}... revoked.")
        session_id = None
        return 0
    except PravaError as exc:
        print(f"\nPrava API error: {exc.message} (code={exc.code}, http_status={exc.http_status})")
        return 1
    finally:
        if session_id:
            # Something failed after create but before/instead of a clean
            # revoke above -- make sure we don't leave a live session behind.
            try:
                client.revoke_session(session_id)
                print(f"[cleanup] revoked session {session_id[:8]}... on the way out")
            except PravaError as exc:
                print(f"[cleanup] FAILED to revoke session {session_id[:8]}...: {exc.message}")
        client.close()


def run_dry_run() -> int:
    payload = build_payload()
    print("DRY RUN (no network call). Request that would be sent:")
    print(f"  POST {os.environ.get('PRAVA_BACKEND_URL', 'https://sandbox.api.prava.space')}/v1/sessions")
    print(json.dumps(payload, indent=2))
    return 0


def run_real() -> int:
    payload = build_payload()
    client = PravaClient()
    try:
        purchase_context = payload["purchase_context"][0]
        session = client.create_session(
            user_id=payload["user_id"],
            user_email=payload["user_email"],
            total_amount=payload["total_amount"],
            currency=payload["currency"],
            merchant_name=purchase_context["merchant_details"]["name"],
            merchant_url=purchase_context["merchant_details"]["url"],
            country_code_iso2=purchase_context["merchant_details"]["country_code_iso2"],
            product_details=purchase_context["product_details"],
            description=payload.get("description"),
            effective_until_minutes=purchase_context["effective_until_minutes"],
        )
        print(f"Session created: session_id={session.session_id} order_id={session.order_id}")
        print(f"Approve in browser: {session.iframe_url}")
        print("Polling for payment result (up to 120s)...")

        result = client.wait_for_result(session.session_id)
        credential = result.first_credential()
        if credential is not None:
            print(
                f"Credential available: txn_ref_id={credential.txn_ref_id} "
                f"token={mask_secret(credential.token)} "
                f"dynamic_cvv={mask_secret(credential.dynamic_cvv)} "
                f"expiry={credential.expiry_month}/{credential.expiry_year}"
            )
        else:
            print(f"No credential yet. Session status={result.status!r}")

        print("Exiting WITHOUT calling report-status -- the orchestrator owns that step.")
        return 0
    except PravaError as exc:
        print(f"Prava API error: {exc.message} (code={exc.code}, http_status={exc.http_status})")
        return 1
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--real",
        action="store_true",
        help="Actually call the Prava sandbox API (requires PRAVA_ALLOW_REAL=1 env var too).",
    )
    parser.add_argument(
        "--handshake",
        action="store_true",
        help=(
            "Run the one-shot create -> print structure -> poll once -> print "
            "structure -> revoke contract check (requires --real and "
            "PRAVA_ALLOW_REAL=1 as well)."
        ),
    )
    args = parser.parse_args()

    if not args.real:
        if args.handshake:
            print("Refusing: --handshake requires --real as well.")
            return 1
        return run_dry_run()

    if os.environ.get("PRAVA_ALLOW_REAL") != "1":
        print(
            "Refusing to run in real mode: --real was passed but env PRAVA_ALLOW_REAL=1 "
            "is not set. Both guards are required to prevent accidental sandbox usage "
            "(30 txn/day cap shared with the orchestrator)."
        )
        return 1

    if args.handshake:
        return run_handshake()

    return run_real()


if __name__ == "__main__":
    raise SystemExit(main())
