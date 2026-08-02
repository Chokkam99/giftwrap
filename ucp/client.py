"""UCP (Universal Commerce Protocol) catalog client.

Speaks JSON-RPC 2.0 over the "streamable HTTP" MCP transport that Shopify
(and other UCP-compliant merchants) expose at a discovered endpoint. Only
read-only catalog tools (`search_catalog`, `get_product`) are used here —
this module never calls checkout/cart/payment tools.

Verified live against giva-jewelry.myshopify.com:
    - Discovery: GET https://{domain}/.well-known/ucp -> JSON profile with
      services["dev.ucp.shopping"][*] entries; pick the one with
      transport == "mcp" and read its "endpoint".
    - tools/call requires meta.ucp-agent.profile to be a URL that the
      merchant server can itself fetch and parse as a UCP agent profile.
      A placeholder/unreachable URL (e.g. https://example.com/...) is
      rejected with a JSON-RPC error (code -32001, "UCP discovery failed").
      Shopify publishes reachable test fixtures for exactly this purpose;
      DEFAULT_AGENT_PROFILE below is one of them and works today.
    - No MCP `initialize` handshake was needed for giva-jewelry — direct
      tools/call succeeded first try. The handshake fallback below exists
      for merchants that DO require it.
    - Successful results land in result.structuredContent (preferred) or
      must be parsed out of result.content[0].text (JSON string) as a
      fallback. Business/catalog errors (e.g. "product not found") show up
      as result.isError == True with details in structuredContent.messages
      — NOT as a top-level JSON-RPC "error" (that's reserved for
      protocol/negotiation failures like a bad agent profile).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

# A Shopify-hosted UCP agent-profile fixture that is publicly fetchable and
# declares catalog + checkout capabilities. Verified live: passes UCP
# negotiation against giva-jewelry.myshopify.com's /api/ucp/mcp endpoint.
DEFAULT_AGENT_PROFILE = "https://shopify.dev/ucp/agent-profiles/2026-04-08/valid-with-capabilities.json"

DEFAULT_TIMEOUT = 15.0

_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


class UCPError(Exception):
    """Raised for JSON-RPC errors, business/catalog errors, or transport failures."""


@dataclass
class Product:
    """Shared contract — WP4/WP5 depend on these exact fields."""

    id: str
    title: str
    price: str
    currency: str
    image_url: str | None
    product_url: str | None
    merchant: str
    variant_id: str | None


def _strip_scheme(domain: str) -> str:
    return _SCHEME_RE.sub("", domain.strip()).rstrip("/")


def _format_amount(amount: Any) -> str:
    """UCP catalog prices are integer minor units (e.g. 299900 == INR 2999.00)."""
    if amount is None:
        return "0.00"
    try:
        return f"{float(amount) / 100:.2f}"
    except (TypeError, ValueError):
        return "0.00"


class UCPClient:
    """Read-only UCP catalog client speaking JSON-RPC 2.0 over streamable HTTP."""

    def __init__(
        self,
        agent_profile_url: str = DEFAULT_AGENT_PROFILE,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        self.agent_profile_url = agent_profile_url
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._endpoint_cache: dict[str, str] = {}
        self._session_ids: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(self, domain: str) -> str:
        """Resolve a storefront or myshopify domain to its UCP MCP endpoint.

        Accepts either a storefront domain (e.g. "giva.co") or a myshopify
        domain (e.g. "giva-jewelry.myshopify.com"). Result is cached
        in-instance keyed by the input domain.
        """
        key = _strip_scheme(domain)
        if key in self._endpoint_cache:
            return self._endpoint_cache[key]

        url = f"https://{key}/.well-known/ucp"
        try:
            resp = self._client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise UCPError(f"UCP discovery request to {url} failed: {exc}") from exc

        try:
            profile = resp.json()
        except ValueError as exc:
            raise UCPError(f"UCP discovery at {url} returned non-JSON body") from exc

        services = (
            profile.get("ucp", {}).get("services", {}).get("dev.ucp.shopping", [])
        )
        endpoint = None
        for svc in services:
            if svc.get("transport") == "mcp" and svc.get("endpoint"):
                endpoint = svc["endpoint"]
                break

        if not endpoint:
            raise UCPError(f"No MCP shopping transport advertised at {url}")

        self._endpoint_cache[key] = endpoint
        return endpoint

    # ------------------------------------------------------------------
    # Public catalog API
    # ------------------------------------------------------------------

    def search_products(
        self,
        store: str,
        query: str,
        max_price: float | None = None,
        limit: int = 10,
    ) -> list[Product]:
        """Unchanged contract (WP4/WP5 depend on it): first page, no cursor.

        Delegates to `search_products_page` and discards the cursor. Use
        `search_products_page` directly to thread `pagination.cursor` through
        for "show more" / "load more" (WP-BROWSE).
        """
        products, _cursor = self.search_products_page(store, query, max_price=max_price, limit=limit)
        return products

    def search_products_page(
        self,
        store: str,
        query: str,
        max_price: float | None = None,
        limit: int = 10,
        cursor: str | None = None,
    ) -> tuple[list[Product], str | None]:
        """Cursor-aware search. Returns (products, next_cursor).

        `next_cursor` is None when the merchant reports no further page
        (`pagination.has_next_page` is false/absent) — callers should treat
        that as "nothing more to load", not retry with a stale cursor.
        """
        endpoint = self.discover(store)
        catalog: dict[str, Any] = {"query": query, "pagination": {"limit": limit}}
        if cursor:
            catalog["pagination"]["cursor"] = cursor
        if max_price is not None:
            catalog["filters"] = {"price_range": {"max": str(max_price)}}

        arguments = {
            "meta": {"ucp-agent": {"profile": self.agent_profile_url}},
            "catalog": catalog,
        }
        result = self._call_tool(endpoint, "search_catalog", arguments)
        data = self._extract_content(result)
        self._raise_if_error(result, data)
        products = data.get("products") or []
        pagination = data.get("pagination") or {}
        next_cursor = pagination.get("cursor") if pagination.get("has_next_page") else None
        return [self._normalize_product(p, store) for p in products], next_cursor

    def get_product(self, store: str, product_id: str) -> Product:
        product, _raw = self.get_product_full(store, product_id)
        return product

    def get_product_full(self, store: str, product_id: str) -> tuple[Product, dict]:
        """Like `get_product`, but also returns the raw UCP product payload —
        full media list, variants (with options/availability), and
        description.html — everything the normalized `Product` contract
        deliberately drops. Used by the product detail view (WP-BROWSE);
        `get_product` keeps its exact existing return type for WP4/WP5.
        """
        endpoint = self.discover(store)
        arguments = {
            "meta": {"ucp-agent": {"profile": self.agent_profile_url}},
            "catalog": {"id": product_id},
        }
        result = self._call_tool(endpoint, "get_product", arguments)
        data = self._extract_content(result)
        self._raise_if_error(result, data)
        product = data.get("product")
        if not product:
            raise UCPError(f"Product {product_id!r} not found")
        return self._normalize_product(product, store), product

    # ------------------------------------------------------------------
    # JSON-RPC / MCP transport
    # ------------------------------------------------------------------

    def _call_tool(self, endpoint: str, name: str, arguments: dict) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        response = self._post(endpoint, payload)

        if "error" in response:
            err = response["error"]
            if endpoint not in self._session_ids and self._looks_like_init_required(err):
                self._initialize(endpoint)
                response = self._post(endpoint, payload)
            if "error" in response:
                err = response["error"]
                raise UCPError(err.get("message", "unknown UCP JSON-RPC error"))

        return response.get("result", {})

    def _post(self, endpoint: str, payload: dict) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        session_id = self._session_ids.get(endpoint)
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        try:
            resp = self._client.post(endpoint, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise UCPError(f"UCP request to {endpoint} failed: {exc}") from exc

        new_session_id = resp.headers.get("Mcp-Session-Id")
        if new_session_id:
            self._session_ids[endpoint] = new_session_id

        return self._parse_response(resp)

    @staticmethod
    def _parse_response(resp: httpx.Response) -> dict:
        text = resp.text
        content_type = resp.headers.get("content-type", "")
        stripped = text.lstrip()
        looks_like_sse = "text/event-stream" in content_type or stripped.startswith(
            ("event:", "data:")
        )

        if looks_like_sse:
            parsed: dict | None = None
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[len("data:"):].strip()
                if not chunk:
                    continue
                try:
                    parsed = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
            if parsed is None:
                raise UCPError("Could not parse SSE response from UCP server")
            return parsed

        try:
            return resp.json()
        except ValueError as exc:
            raise UCPError(
                f"UCP server returned a non-JSON, non-SSE response "
                f"(status {resp.status_code}): {text[:200]!r}"
            ) from exc

    @staticmethod
    def _looks_like_init_required(err: dict) -> bool:
        message = str(err.get("message", "")).lower()
        code = err.get("code")
        return code == -32002 or any(
            token in message for token in ("not initialized", "initialize", "session")
        )

    def _initialize(self, endpoint: str) -> None:
        """Perform the MCP initialize handshake for servers that require it."""
        init_payload = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "agentic-gifting-ucp-client", "version": "0.1.0"},
            },
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        try:
            resp = self._client.post(endpoint, json=init_payload, headers=headers)
        except httpx.HTTPError as exc:
            raise UCPError(f"MCP initialize request to {endpoint} failed: {exc}") from exc

        session_id = resp.headers.get("Mcp-Session-Id")
        if session_id:
            self._session_ids[endpoint] = session_id

        result = self._parse_response(resp)
        if "error" in result:
            raise UCPError(f"MCP initialize failed: {result['error'].get('message')}")

        notify_headers = dict(headers)
        if endpoint in self._session_ids:
            notify_headers["Mcp-Session-Id"] = self._session_ids[endpoint]
        try:
            self._client.post(
                endpoint,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=notify_headers,
            )
        except httpx.HTTPError:
            # Notification is fire-and-forget; ignore transport errors here.
            pass

    # ------------------------------------------------------------------
    # Response normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_content(result: dict) -> dict:
        structured = result.get("structuredContent")
        if structured is not None:
            return structured

        for item in result.get("content") or []:
            if item.get("type") == "text" and item.get("text"):
                try:
                    return json.loads(item["text"])
                except json.JSONDecodeError:
                    continue
        return {}

    @staticmethod
    def _raise_if_error(result: dict, data: dict) -> None:
        if not result.get("isError"):
            return
        messages = data.get("messages") or []
        if messages:
            text = "; ".join(m.get("content", str(m)) for m in messages)
        else:
            text = "UCP tool call reported an error with no message detail"
        raise UCPError(text)

    def _normalize_product(self, raw: dict, store: str) -> Product:
        variants = raw.get("variants") or []
        in_stock = [v for v in variants if (v.get("availability") or {}).get("available")]
        pool = in_stock or variants

        chosen_variant: dict | None = None
        if pool:
            chosen_variant = min(
                pool,
                key=lambda v: (v.get("price") or {}).get("amount", float("inf")),
            )

        if chosen_variant and chosen_variant.get("price"):
            amount = chosen_variant["price"].get("amount")
            currency = chosen_variant["price"].get("currency", "")
            variant_id = chosen_variant.get("id")
        else:
            price_range = raw.get("price_range") or {}
            min_price = price_range.get("min") or {}
            amount = min_price.get("amount")
            currency = min_price.get("currency", "")
            variant_id = None

        media = raw.get("media") or (chosen_variant or {}).get("media") or []
        image_url = media[0].get("url") if media else None

        handle = raw.get("handle")
        product_url = f"https://{_strip_scheme(store)}/products/{handle}" if handle else None

        return Product(
            id=raw.get("id"),
            title=raw.get("title", ""),
            price=_format_amount(amount),
            currency=currency,
            image_url=image_url,
            product_url=product_url,
            merchant=_strip_scheme(store),
            variant_id=variant_id,
        )
