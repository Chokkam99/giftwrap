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
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prava.client import PravaClient, PravaError, mask_secret  # noqa: E402


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
    args = parser.parse_args()

    if not args.real:
        return run_dry_run()

    if os.environ.get("PRAVA_ALLOW_REAL") != "1":
        print(
            "Refusing to run in real mode: --real was passed but env PRAVA_ALLOW_REAL=1 "
            "is not set. Both guards are required to prevent accidental sandbox usage "
            "(30 txn/day cap shared with the orchestrator)."
        )
        return 1

    return run_real()


if __name__ == "__main__":
    raise SystemExit(main())
