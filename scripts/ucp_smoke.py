#!/usr/bin/env python3
"""Live smoke test for the UCP catalog client (ucp/client.py).

Makes real, read-only catalog calls against a live Shopify UCP endpoint.
Never touches checkout/cart/payment tools.

Usage:
    python scripts/ucp_smoke.py --store giva-jewelry.myshopify.com --query "necklace" --max-price 3000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from ucp.client import UCPClient, UCPError  # noqa: E402


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Smoke-test a live Shopify UCP catalog endpoint.")
    parser.add_argument("--store", required=True, help="Storefront or myshopify domain, e.g. giva-jewelry.myshopify.com")
    parser.add_argument("--query", required=True, help="Search query, e.g. 'necklace'")
    parser.add_argument("--max-price", type=float, default=None, help="Maximum price filter")
    parser.add_argument("--limit", type=int, default=10, help="Max results to request")
    parser.add_argument("--get-product", default=None, help="Optionally fetch a single product by id after searching")
    args = parser.parse_args()

    client = UCPClient()

    try:
        endpoint = client.discover(args.store)
    except UCPError as exc:
        print(f"Discovery failed: {exc}", file=sys.stderr)
        return 1
    print(f"Discovered MCP endpoint: {endpoint}")
    print(f"Agent profile used:      {client.agent_profile_url}\n")

    try:
        products = client.search_products(args.store, args.query, max_price=args.max_price, limit=args.limit)
    except UCPError as exc:
        print(f"Search failed: {exc}", file=sys.stderr)
        return 1

    if not products:
        print("No products returned.")
        return 0

    print(f"{len(products)} product(s) for query={args.query!r} (max_price={args.max_price}):\n")
    for p in products:
        print(f"- {p.title}")
        print(f"    id:         {p.id}")
        print(f"    price:      {p.price} {p.currency}")
        print(f"    variant_id: {p.variant_id}")
        print(f"    url:        {p.product_url}")
        print(f"    image:      {p.image_url}")
        print()

    if args.get_product:
        try:
            product = client.get_product(args.store, args.get_product)
        except UCPError as exc:
            print(f"get_product failed: {exc}", file=sys.stderr)
            return 1
        print(f"get_product({args.get_product}):")
        print(f"  {product.title} - {product.price} {product.currency}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
