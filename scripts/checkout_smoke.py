#!/usr/bin/env python3
"""Smoke CLI for the Playwright checkout (WP3).

Two modes:

  1. Read-only store probe -- loads a product page, checks add-to-cart exists,
     buys nothing. Safe to point anywhere:

       python scripts/checkout_smoke.py --verify-store \
           --product-url https://my-dev-store.myshopify.com/products/silver-pendant

  2. Full checkout with a one-time virtual card (dev store only):

       python scripts/checkout_smoke.py \
           --product-url https://my-dev-store.myshopify.com/products/silver-pendant \
           --token 4111111111111111 --cvv 123 --month 12 --year 2027 --headed

Add --dry-run to fill the entire checkout but stop before clicking Pay.

Use --country US for a US-region dev store (the default shipping address is
Indian and a US store rejects it); it seeds $CHECKOUT_ADDRESS_COUNTRY.

Safety: a purchase run refuses to start unless a product URL is supplied
(argument or SHOPIFY_DEV_STORE_PRODUCT_URL), and the module itself refuses
non-dev-store hosts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from checkout.playwright_checkout import (  # noqa: E402
    ENV_ADDRESS_COUNTRY,
    ENV_PRODUCT_URL,
    SHIPPING_PROFILES,
    CheckoutError,
    bogus_gateway_pan,
    mask_pan,
    run_shopify_checkout,
    shipping_address,
    verify_store,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="checkout_smoke.py",
        description="Smoke-test the Shopify dev-store checkout automation.",
    )
    parser.add_argument(
        "--product-url",
        default=None,
        help=f"Dev-store product page URL (defaults to ${ENV_PRODUCT_URL}).",
    )
    parser.add_argument("--token", default=None, help="16-digit virtual card number.")
    parser.add_argument("--cvv", default=None, help="Dynamic CVV for the card.")
    parser.add_argument("--month", default=None, help="Expiry month, e.g. 12.")
    parser.add_argument("--year", default=None, help="Expiry year, e.g. 2027.")
    parser.add_argument(
        "--email",
        default="gifting-demo@example.com",
        help="Contact email used on the order.",
    )
    parser.add_argument(
        "--country",
        default=None,
        choices=sorted(SHIPPING_PROFILES),
        help="Shipping-address region (defaults to $CHECKOUT_ADDRESS_COUNTRY, "
        "then IN). Use US for a US-region dev store.",
    )
    parser.add_argument(
        "--headed", action="store_true", help="Show the browser window."
    )
    parser.add_argument(
        "--timeout", type=int, default=60000, help="Per-operation timeout in ms."
    )
    parser.add_argument(
        "--verify-store",
        action="store_true",
        help="Read-only: load the product page and check add-to-cart exists. "
        "Buys nothing; no card arguments required.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fill the whole checkout but never click Pay.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON only.")
    return parser


def _emit(payload: dict, as_json: bool, lines: list) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        for line in lines:
            print(line)


def _run_verify(args) -> int:
    headless = False if args.headed else None
    report = verify_store(
        product_url=args.product_url, headless=headless, timeout_ms=args.timeout
    )
    lines = [
        f"URL              : {report['url']}",
        f"Host             : {report['host']} (dev store: {report['is_dev_store']})",
        f"Title            : {report.get('title')}",
        f"Price            : {report.get('price_text')}",
        f"Add-to-cart      : found={report['add_to_cart_found']} "
        f"enabled={report['add_to_cart_enabled']}",
        f"Result           : {'OK' if report['ok'] else 'NOT READY'} - {report['message']}",
    ]
    _emit(report, args.json, lines)
    return 0 if report["ok"] else 2


def _run_checkout(args) -> int:
    missing = [
        name
        for name, value in (
            ("--token", args.token),
            ("--cvv", args.cvv),
            ("--month", args.month),
            ("--year", args.year),
        )
        if not value
    ]
    if missing:
        print(
            "Refusing to run: missing card arguments " + ", ".join(missing),
            file=sys.stderr,
        )
        return 2
    if not args.product_url and not os.environ.get(ENV_PRODUCT_URL):
        print(
            "Refusing to run a purchase: no --product-url and "
            f"${ENV_PRODUCT_URL} is unset. Checkouts run against our Shopify "
            "development store only.",
            file=sys.stderr,
        )
        return 2

    bogus = bogus_gateway_pan()
    if bogus is not None:
        print(
            f"[warn] BOGUS_GATEWAY mode: card number {bogus!r} will be sent "
            "instead of the real token (gateway simulation).",
            file=sys.stderr,
        )
    # --country just seeds CHECKOUT_ADDRESS_COUNTRY so per-field CHECKOUT_SHIP_*
    # overrides keep working on top of the chosen region profile.
    if args.country:
        os.environ[ENV_ADDRESS_COUNTRY] = args.country
    address = shipping_address()
    if not args.json:
        print(f"Card: {mask_pan(args.token)}  exp {args.month}/{args.year}")
        print(f"Ship: {address['city']}, {address['province_code']} {address['country_code']}")
        if args.dry_run:
            print("Dry run: the Pay button will NOT be clicked.")

    result = run_shopify_checkout(
        token=args.token,
        dynamic_cvv=args.cvv,
        expiry_month=args.month,
        expiry_year=args.year,
        product_url=args.product_url,
        contact_email=args.email,
        headless=False if args.headed else None,
        timeout_ms=args.timeout,
        dry_run=args.dry_run,
    )
    lines = [
        f"Status  : {result.status}",
        f"Success : {result.success}",
        f"Order id: {result.order_id}",
        f"Message : {result.message}",
        f"Steps   : {', '.join(result.details.get('steps', []))}",
    ]
    frames = result.details.get("card_frames")
    if frames is not None:
        lines.append(f"Card DOM: {'iframes: ' + ', '.join(frames) if frames else 'direct inputs'}")
    warnings = result.details.get("field_warnings")
    if warnings:
        lines.append(f"Warnings: {warnings}")
    screenshots = result.details.get("screenshots")
    if screenshots:
        lines.append(f"Shots   : {', '.join(screenshots)}")
    _emit(result.to_dict(), args.json, lines)
    if result.status == "APPROVED":
        return 0
    return 1 if result.status == "DECLINED" else 3


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.verify_store:
            return _run_verify(args)
        return _run_checkout(args)
    except CheckoutError as exc:
        print(f"[setup error] {exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
