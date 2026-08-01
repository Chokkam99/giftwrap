"""Mocked tests for ucp/client.py — no live network calls here.

Live, read-only smoke testing against giva-jewelry.myshopify.com lives in
scripts/ucp_smoke.py instead.
"""

from __future__ import annotations

import json

import httpx
import pytest

from ucp.client import DEFAULT_AGENT_PROFILE, Product, UCPClient, UCPError

WELL_KNOWN_URL = "https://shop.example.myshopify.com/.well-known/ucp"
MCP_ENDPOINT = "https://shop.example.myshopify.com/api/ucp/mcp"

WELL_KNOWN_BODY = {
    "ucp": {
        "services": {
            "dev.ucp.shopping": [
                {"version": "2026-04-08", "transport": "embedded"},
                {"version": "2026-04-08", "transport": "mcp", "endpoint": MCP_ENDPOINT},
            ]
        }
    }
}


def _search_result(is_error: bool = False, messages: list | None = None) -> dict:
    """A search_catalog JSON-RPC response with one product that has a
    cheaper OUT-OF-STOCK variant and a pricier IN-STOCK one, to exercise
    the "cheapest in-stock variant" normalization rule.
    """
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "isError": is_error,
            "structuredContent": {
                "products": [
                    {
                        "id": "gid://shopify/Product/1",
                        "title": "Test Necklace",
                        "handle": "test-necklace",
                        "price_range": {
                            "min": {"amount": 179900, "currency": "INR"},
                            "max": {"amount": 249900, "currency": "INR"},
                        },
                        "variants": [
                            {
                                "id": "gid://shopify/ProductVariant/10",
                                "price": {"amount": 249900, "currency": "INR"},
                                "availability": {"available": False},
                            },
                            {
                                "id": "gid://shopify/ProductVariant/11",
                                "price": {"amount": 199900, "currency": "INR"},
                                "availability": {"available": True},
                            },
                            {
                                "id": "gid://shopify/ProductVariant/12",
                                "price": {"amount": 179900, "currency": "INR"},
                                "availability": {"available": False},
                            },
                        ],
                        "media": [{"type": "image", "url": "https://cdn.example.com/img.jpg"}],
                    }
                ],
                "messages": messages or [],
            },
        },
    }


def _make_client(post_handler) -> UCPClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and str(request.url) == WELL_KNOWN_URL:
            return httpx.Response(200, json=WELL_KNOWN_BODY)
        if request.method == "POST" and str(request.url) == MCP_ENDPOINT:
            return post_handler(request)
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(handler)
    return UCPClient(client=httpx.Client(transport=transport))


# ----------------------------------------------------------------------
# Plain-JSON response parsing
# ----------------------------------------------------------------------


def test_discover_finds_mcp_endpoint():
    client = _make_client(lambda request: httpx.Response(200, json={}))
    endpoint = client.discover("shop.example.myshopify.com")
    assert endpoint == MCP_ENDPOINT
    # cached on second call — still correct, and no crash if called again.
    assert client.discover("shop.example.myshopify.com") == MCP_ENDPOINT


def test_plain_json_response_parse():
    def post_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_search_result(), headers={"content-type": "application/json"}
        )

    client = _make_client(post_handler)
    products = client.search_products("shop.example.myshopify.com", "necklace")

    assert len(products) == 1
    assert isinstance(products[0], Product)
    assert products[0].title == "Test Necklace"
    assert products[0].merchant == "shop.example.myshopify.com"
    assert products[0].product_url == "https://shop.example.myshopify.com/products/test-necklace"


# ----------------------------------------------------------------------
# SSE response parsing
# ----------------------------------------------------------------------


def test_sse_response_parse():
    def post_handler(request: httpx.Request) -> httpx.Response:
        body = (
            "event: message\n"
            f"data: {json.dumps(_search_result())}\n\n"
        )
        return httpx.Response(
            200,
            content=body.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    client = _make_client(post_handler)
    products = client.search_products("shop.example.myshopify.com", "necklace")

    assert len(products) == 1
    assert products[0].title == "Test Necklace"
    assert products[0].price == "1999.00"
    assert products[0].currency == "INR"


def test_sse_with_multiple_data_lines_uses_last_valid_payload():
    def post_handler(request: httpx.Request) -> httpx.Response:
        body = (
            "event: ping\n"
            "data: {}\n\n"
            "event: message\n"
            f"data: {json.dumps(_search_result())}\n\n"
        )
        return httpx.Response(
            200,
            content=body.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    client = _make_client(post_handler)
    products = client.search_products("shop.example.myshopify.com", "necklace")
    assert len(products) == 1


# ----------------------------------------------------------------------
# Product normalization: price_range -> cheapest IN-STOCK variant
# ----------------------------------------------------------------------


def test_cheapest_in_stock_variant_is_selected_over_cheaper_oos_variant():
    def post_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_search_result())

    client = _make_client(post_handler)
    products = client.search_products("shop.example.myshopify.com", "necklace")

    product = products[0]
    # Variant 12 (179900) is cheaper but out of stock; variant 11 (199900,
    # in stock) must win over both it and variant 10 (249900, also OOS).
    assert product.variant_id == "gid://shopify/ProductVariant/11"
    assert product.price == "1999.00"
    assert product.image_url == "https://cdn.example.com/img.jpg"


def test_falls_back_to_price_range_when_no_variants():
    def post_handler(request: httpx.Request) -> httpx.Response:
        result = _search_result()
        result["result"]["structuredContent"]["products"][0]["variants"] = []
        return httpx.Response(200, json=result)

    client = _make_client(post_handler)
    products = client.search_products("shop.example.myshopify.com", "necklace")

    product = products[0]
    assert product.variant_id is None
    # price_range.min.amount == 179900 -> "1799.00"
    assert product.price == "1799.00"
    assert product.currency == "INR"


def test_get_product_normalization_via_structured_content():
    def post_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["params"]["name"] == "get_product"
        assert payload["params"]["arguments"]["catalog"]["id"] == "gid://shopify/Product/1"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": False,
                    "structuredContent": {
                        "product": _search_result()["result"]["structuredContent"]["products"][0],
                        "messages": [],
                    },
                },
            },
        )

    client = _make_client(post_handler)
    product = client.get_product("shop.example.myshopify.com", "gid://shopify/Product/1")
    assert product.title == "Test Necklace"
    assert product.price == "1999.00"


def test_content_text_fallback_when_structured_content_absent():
    def post_handler(request: httpx.Request) -> httpx.Response:
        inner = _search_result()["result"]["structuredContent"]
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": False,
                    "content": [{"type": "text", "text": json.dumps(inner)}],
                },
            },
        )

    client = _make_client(post_handler)
    products = client.search_products("shop.example.myshopify.com", "necklace")
    assert len(products) == 1
    assert products[0].title == "Test Necklace"


# ----------------------------------------------------------------------
# Error message surfacing
# ----------------------------------------------------------------------


def test_business_error_in_messages_array_raises_ucp_error():
    def post_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": True,
                    "structuredContent": {
                        "product": None,
                        "messages": [
                            {
                                "type": "error",
                                "content_type": "plain",
                                "code": "product_not_found",
                                "content": "Product not found",
                                "severity": "unrecoverable",
                            }
                        ],
                    },
                },
            },
        )

    client = _make_client(post_handler)
    with pytest.raises(UCPError, match="Product not found"):
        client.get_product("shop.example.myshopify.com", "gid://shopify/Product/999")


def test_jsonrpc_level_error_raises_ucp_error_with_server_message():
    def post_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "error": {
                    "code": -32001,
                    "message": "UCP discovery failed: profile_unreachable",
                },
            },
        )

    client = _make_client(post_handler)
    with pytest.raises(UCPError, match="profile_unreachable"):
        client.search_products("shop.example.myshopify.com", "necklace")


def test_discover_raises_when_no_mcp_transport_advertised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ucp": {"services": {"dev.ucp.shopping": []}}})

    transport = httpx.MockTransport(handler)
    client = UCPClient(client=httpx.Client(transport=transport))
    with pytest.raises(UCPError):
        client.discover("shop.example.myshopify.com")


def test_default_agent_profile_is_sent_in_arguments():
    seen = {}

    def post_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen["profile"] = payload["params"]["arguments"]["meta"]["ucp-agent"]["profile"]
        return httpx.Response(200, json=_search_result())

    client = _make_client(post_handler)
    client.search_products("shop.example.myshopify.com", "necklace", max_price=3000, limit=5)

    assert seen["profile"] == DEFAULT_AGENT_PROFILE
