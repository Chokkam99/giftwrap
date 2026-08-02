"""Agentic Gifting — FastAPI backend + Claude tool-use loop (WP4 + WP-INT).

`prava/`, `ucp/` and `checkout/` are imported lazily inside the tool handlers
and called through their REAL signatures (see WP-INT). Stubs still exist so the
UI can be demoed without credentials, but falling back to one is now LOUD:
every stubbed or failed call is logged with its traceback, marked `degraded`
in the tool result the model sees, and reported by `GET /health`.

Safety invariants enforced here (not just in the prompt):
  * the one-time card `token` / `dynamic_cvv` never reach the model or the logs
    - they live in server-side conversation state, addressed only by session_id;
  * `mint_scoped_card` refuses to mint above the buyer's stated budget, and
    `complete_checkout` re-checks price and merchant scope before paying;
  * `search_products` hard-filters the catalog by budget in code — UCP's
    `catalog.filters.price_range.max` is only a soft relevance hint on live
    stores (WP2 handoff), so over-budget products must never reach the model.
"""

from __future__ import annotations

import html
import importlib
import json
import logging
import os
import re
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

log = logging.getLogger("agentic_gifting")

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
MAX_TOKENS = 8192
MAX_TOOL_ITERATIONS = 10
DEFAULT_STORE = os.getenv("UCP_DEFAULT_STORE", "giva-jewelry.myshopify.com")
# Multi-store catalog: when a tool call omits `store`, every one of these is searched
# concurrently and the results are merged (see _multi_store_search). salty.co.in,
# plumgoodness.com and xyxxcrew.com were verified live (WP-BROWSE) — discovery +
# one search each succeeded against their Shopify UCP endpoints. xyxx.com itself is
# a parked/unrelated domain; the brand's real storefront is xyxxcrew.com.
UCP_STORES = [
    s.strip() for s in
    os.getenv(
        "UCP_STORES",
        "giva-jewelry.myshopify.com,mamaearth.in,salty.co.in,plumgoodness.com,xyxxcrew.com",
    ).split(",")
    if s.strip()
] or [DEFAULT_STORE]
# Cap on total products shown across "show more" / "load more" pagination, per
# browsing session (buyer chat conversation, or one recipient gift-link query).
MAX_SHOWN_PRODUCTS = 36
CURRENCY = os.getenv("GIFT_CURRENCY", "INR")
CARD_POLL_TIMEOUT = 10  # seconds — short so the model can poll conversationally
BUYER_ID = os.getenv("PRAVA_USER_ID", "agentic-gifting-buyer")
BUYER_EMAIL = os.getenv("PRAVA_USER_EMAIL", "buyer@example.com")
# Prava's create_session requires a merchant country; our demo merchants are Indian.
MERCHANT_COUNTRY = os.getenv("PRAVA_MERCHANT_COUNTRY", "IN")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_HTML = STATIC_DIR / "index.html"
GIFT_HTML = STATIC_DIR / "gift.html"

PRODUCT_FIELDS = (
    "id", "title", "price", "currency", "image_url",
    "product_url", "merchant", "variant_id",
)

SYSTEM_PROMPT = f"""You are the Agentic Gifting concierge. You help a buyer pick and pay for a
gift, and you mint a one-time Prava card that is scoped to a single merchant and capped at the
buyer's budget, so the spend physically cannot exceed what they approved.

Follow this flow:
1. Gather the recipient, the budget, and the vibe/occasion. As soon as you know the budget and
   recipient, call `set_gift_context` — no other tool works until you have.
2. Find out who is picking the gift:
   * BUYER PICKS (the default — assume this unless told otherwise): search with
     `search_products`. Omit `store` to search every configured store at once
     ({', '.join(UCP_STORES)}) concurrently, and tell the buyer which stores you searched. Present
     2-3 options that are within budget, with prices and which store each is from, and ask the
     buyer to pick one.
   * RECIPIENT PICKS ("let them pick", "let her choose", "surprise them", etc.): call
     `create_gift_link` with a short warm note capturing the occasion/vibe, then give the buyer the
     returned link to share. Later, when the buyer asks whether the recipient has picked yet, call
     `get_gift_status` with that token. If nothing is picked, say so warmly and suggest checking
     back later. Once `get_gift_status` shows a picked product, treat it exactly like the buyer's
     own explicit approval in step 3 — proceed straight to minting for that exact product, no
     further confirmation needed.
3. NEVER call `mint_scoped_card` until a specific product has been explicitly approved — either by
   the buyer directly, or by the recipient through a gift link (confirmed via `get_gift_status`).
   Mint for the exact price of that product, scoped to that merchant.
4. After minting, tell the buyer to approve the payment in the Prava window that just opened.
5. Poll `get_card_status`. While it is pending, tell the buyer to finish the approval. Once it is
   ready, call `complete_checkout` for the approved product and present the receipt (order id,
   amount, merchant) in plain language.

The buyer's UI sends two LITERAL messages that are button presses, not free-form chat. Recognise
them exactly:
  * "I approve: {{title}} (id {{id}}) at {{price}}" — the buyer pressed "Gift this" on that exact
    product card. This IS the explicit approval required by step 3. Do not search again and do not
    ask them to confirm a second time: call `mint_scoped_card` ONCE for that product, with
    `amount` set to that exact price and `merchant_url` set to that product's store URL, then tell
    them to approve in the Prava window that just opened.
  * "I completed the Prava approval" — the buyer says they finished the Prava window. Call
    `get_card_status` with the `session_id` you got back from `mint_scoped_card`. If it comes back
    pending, say so warmly and ask them to finish the Prava window (they can tell you again). Once
    `ready` is true, call `complete_checkout` with that `session_id` and the approved product's
    `product_url`, then present the receipt.

Rules: be warm and brief. Quote prices with the currency. Never invent products, order ids, or
prices — only report what the tools returned. If a tool refuses (e.g. the budget guard), explain
the refusal honestly to the buyer instead of retrying with different numbers. If a tool result
carries "degraded": true or "stub": true, the live service was unreachable and the data is fake —
say so plainly rather than presenting it as real.
You never see the card number; that is by design."""

def _field(description: str = "", kind: str = "string") -> dict:
    return {"type": kind, "description": description} if description else {"type": kind}


def _tool(name: str, description: str, required: list[str], /, **properties: dict) -> dict:
    # positional-only, so a tool may itself have a "description" input field
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": properties, "required": required}}


TOOLS: list[dict[str, Any]] = [
    _tool("set_gift_context",
          "Record the gift context. MUST be called before any other tool. The budget recorded "
          "here is enforced in code: minting above it is rejected.",
          ["budget", "recipient"],
          budget=_field("Max total spend, e.g. '3000' or '₹3,000'."),
          recipient=_field("Who the gift is for."),
          note=_field("Occasion / vibe / preferences.")),
    _tool("search_products",
          "Search live catalogs for gift options. If `store` is omitted, ALL configured stores "
          f"({', '.join(UCP_STORES)}) are searched concurrently and the results are merged.",
          ["query"],
          store=_field("Store domain. Omit to search every configured store at once."),
          query=_field("What to search for."),
          max_price=_field("Upper price bound.", "number")),
    _tool("get_product",
          "Fetch one product's details by id.",
          ["product_id"],
          store=_field(), product_id=_field()),
    _tool("mint_scoped_card",
          "Mint a one-time Prava card scoped to this merchant and capped at this amount, and open "
          "the buyer's approval window. Only after the buyer approves a specific product.",
          ["merchant_name", "merchant_url", "amount", "description"],
          merchant_name=_field(),
          merchant_url=_field("Merchant site URL."),
          amount=_field("Exact product price as a decimal string."),
          description=_field("What is being bought.")),
    _tool("get_card_status",
          "Check whether the buyer finished approving the payment. Returns status and a ready flag "
          "only — the card credential stays server-side.",
          ["session_id"],
          session_id=_field()),
    _tool("complete_checkout",
          "Pay for the approved product with the approved scoped card and return the receipt.",
          ["session_id", "product_url"],
          session_id=_field(), product_url=_field()),
    _tool("create_gift_link",
          "Generate a shareable link so the RECIPIENT can pick their own gift, within the "
          "buyer's budget. Requires set_gift_context to have been called first. Use this when "
          "the buyer wants the recipient to choose (e.g. 'let them pick').",
          ["note"],
          note=_field("A short warm note from the buyer to the recipient (occasion / vibe).")),
    _tool("get_gift_status",
          "Check whether the recipient has picked a gift yet on a link made by "
          "create_gift_link. Returns the picked product if there is one.",
          ["token"],
          token=_field("The gift token returned by create_gift_link.")),
]


# --------------------------------------------------------------------------- state


@dataclass
class Conversation:
    id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    budget: float | None = None
    recipient: str | None = None
    note: str | None = None
    # session_id -> {merchant_name, merchant_url, amount, iframe_url, expires_at,
    #                credential (server-only), txn_ref_id, polls, stub, completed}
    minted: dict[str, dict[str, Any]] = field(default_factory=dict)
    prices: dict[str, float] = field(default_factory=dict)  # product_url -> price
    # "Show more like this" pagination state (WP-BROWSE). Set by the search tool,
    # consumed by POST /chat/{id}/more. None until the first real (non-stub) search.
    #   {query, max_price, stores, cursors: {store: cursor}, shown_ids: set[str]}
    last_search: dict[str, Any] | None = None


CONVERSATIONS: dict[str, Conversation] = {}

# Mode B — "let them pick" gift links. token -> {budget, note, recipient, stores,
# buyer_conversation_id, status: "awaiting_pick" | "picked", picked_product}.
# The recipient-facing endpoints (GET/POST /gift/{token}...) only ever read/write
# `note`, `budget`, `status`, and `picked_product` on this dict — never
# `buyer_conversation_id`, which stays server-side just like the Prava credential does.
GIFTS: dict[str, dict[str, Any]] = {}


def get_conversation(conversation_id: str) -> Conversation:
    conv = CONVERSATIONS.get(conversation_id)
    if conv is None:
        conv = Conversation(id=conversation_id)
        CONVERSATIONS[conversation_id] = conv
    return conv


# ------------------------------------------------------------------ lazy WP modules


MODULE_NAMES = ("prava", "ucp", "checkout")

_MODULES: dict[str, Any] = {}

# Import-level and runtime health of the three real work packages.
#   mode      "real"  — the module imported and its entry point is usable
#             "stub"  — every call is served by the fakes below
#             "unknown" — not probed yet
#   degraded  flips to True the first time a REAL module raises at CALL time.
#             That is the failure this spine used to swallow silently.
MODULE_STATUS: dict[str, dict[str, Any]] = {
    name: {"mode": "unknown", "detail": None, "degraded": False, "last_error": None}
    for name in MODULE_NAMES
}


def _set_mode(name: str, mode: str, detail: str | None = None) -> None:
    MODULE_STATUS[name]["mode"] = mode
    MODULE_STATUS[name]["detail"] = detail


def _mark_degraded(name: str, call: str, exc: BaseException) -> str:
    """Record — LOUDLY — that a real module failed at call time."""
    reason = f"{call}: {type(exc).__name__}: {exc}"
    MODULE_STATUS[name]["degraded"] = True
    MODULE_STATUS[name]["last_error"] = reason
    log.exception(
        "DEGRADED: real %s module failed in %s — falling back to stub data", name, call
    )
    return reason


def _import(name: str) -> Any | None:
    if name not in _MODULES:
        try:
            _MODULES[name] = importlib.import_module(name)
        except Exception:  # not built yet, or broken mid-edit — use the stub
            log.exception("could not import %s — that work package will be stubbed", name)
            _MODULES[name] = None
    return _MODULES[name]


def _load_ucp() -> Any | None:
    """WP2's ucp.client.UCPClient, or None (→ stub).

    Constructed no-arg on purpose: the real constructor is
    `UCPClient(agent_profile_url=DEFAULT_AGENT_PROFILE, timeout=15.0, client=None)`,
    i.e. every argument already has a working default.
    """
    if "ucp_instance" not in _MODULES:
        instance = None
        mod = _import("ucp.client")
        if mod is None:
            _set_mode("ucp", "stub", "ucp.client is not importable")
        else:
            try:
                instance = mod.UCPClient()
                _set_mode("ucp", "real")
            except Exception as exc:
                log.warning("UCPClient() could not be constructed: %s", exc)
                _set_mode("ucp", "stub", f"{type(exc).__name__}: {exc}")
        _MODULES["ucp_instance"] = instance
    return _MODULES["ucp_instance"]


def _load_prava() -> Any | None:
    """WP1's prava.client.PravaClient, or None (→ stub).

    Env-driven: the real constructor reads PRAVA_SECRET_KEY / PRAVA_BACKEND_URL
    and raises PravaError when the key is absent, which is exactly the "not
    configured → stub, and say so" case. Constructing it performs NO network I/O.
    """
    if "prava_instance" not in _MODULES:
        instance = None
        mod = _import("prava.client")
        if mod is None:
            _set_mode("prava", "stub", "prava.client is not importable")
        else:
            try:
                instance = mod.PravaClient()
                _set_mode("prava", "real")
            except Exception as exc:
                log.warning("PravaClient() could not be constructed: %s", exc)
                _set_mode("prava", "stub", f"{type(exc).__name__}: {exc}")
        _MODULES["prava_instance"] = instance
    return _MODULES["prava_instance"]


def _load_checkout() -> Any | None:
    """WP3's checkout.playwright_checkout.run_shopify_checkout, or None (→ stub)."""
    if "checkout_fn" not in _MODULES:
        fn = None
        mod = _import("checkout.playwright_checkout")
        if mod is None:
            _set_mode("checkout", "stub", "checkout.playwright_checkout is not importable")
        else:
            fn = getattr(mod, "run_shopify_checkout", None)
            if fn is None:
                _set_mode("checkout", "stub", "run_shopify_checkout is missing from the module")
            else:
                _set_mode("checkout", "real")
        _MODULES["checkout_fn"] = fn
    return _MODULES["checkout_fn"]


def probe_modules() -> dict[str, str]:
    """Import-level probe: real modules or stubs? Performs no network I/O."""
    loaders = (("prava", _load_prava), ("ucp", _load_ucp), ("checkout", _load_checkout))
    for name, loader in loaders:
        loaded = loader() is not None
        if not loaded and MODULE_STATUS[name]["mode"] != "stub":
            _set_mode(name, "stub", MODULE_STATUS[name]["detail"])
        elif loaded and MODULE_STATUS[name]["mode"] != "real":
            _set_mode(name, "real")
    return {name: MODULE_STATUS[name]["mode"] for name in MODULE_NAMES}


def _fields(obj: Any, names: tuple[str, ...]) -> dict[str, Any]:
    if isinstance(obj, dict):
        return {n: obj.get(n) for n in names}
    return {n: getattr(obj, n, None) for n in names}


def _parse_amount(value: Any) -> float | None:
    """'₹3,000' / '2999.00' / 2999 -> float."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    match = re.search(r"\d[\d,]*(?:\.\d+)?", value)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _same_merchant(a: str | None, b: str | None) -> bool:
    ha = (urlparse(a or "").hostname or "").lower().removeprefix("www.")
    hb = (urlparse(b or "").hostname or "").lower().removeprefix("www.")
    return bool(ha) and ha == hb


# ------------------------------------------------------------------------- stubs


def _stub_products(store: str, query: str, max_price: float | None, limit: int = 3) -> list[dict]:
    ceiling = max_price if max_price and max_price > 0 else 3000.0
    out = []
    for i in range(1, limit + 1):
        price = round(ceiling * (0.55 + 0.15 * i), 2)
        slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-") or "gift"
        out.append({
            "id": f"stub-{slug}-{i}",
            "title": f"{query.title()} — option {i}",
            "price": f"{price:.2f}",
            "currency": CURRENCY,
            "image_url": f"https://{store}/cdn/stub-{i}.jpg",
            "product_url": f"https://{store}/products/{slug}-{i}",
            "merchant": store,
            "variant_id": f"stub-variant-{i}",
            "stub": True,
        })
    return out


# ---------------------------------------------------------------- tool handlers


def _tool_set_gift_context(conv: Conversation, args: dict) -> dict:
    budget = _parse_amount(args.get("budget"))
    if budget is None or budget <= 0:
        return {"error": "Could not read a numeric budget. Ask the buyer for an amount."}
    conv.budget = budget
    conv.recipient = args.get("recipient")
    conv.note = args.get("note")
    return {
        "ok": True, "budget": budget, "currency": CURRENCY,
        "recipient": conv.recipient, "note": conv.note,
        "guard": "Minting above this budget will be rejected by the server.",
    }


def _within_budget(products: list[dict], max_price: float | None) -> tuple[list[dict], int]:
    """HARD client-side budget filter.

    UCP's `catalog.filters.price_range.max` is a soft relevance hint — WP2 saw
    both GIVA and Mamaearth return products above the requested max — so the cap
    is enforced here, before anything reaches the model. Products whose price we
    cannot parse are dropped too: an unknown price cannot be proven in budget.
    """
    if max_price is None:
        return products, 0
    kept: list[dict] = []
    dropped = 0
    for product in products:
        price = _parse_amount(product.get("price"))
        if price is None or price <= 0 or price > max_price + 1e-9:
            dropped += 1
            continue
        kept.append(product)
    if dropped:
        log.info(
            "budget filter dropped %d of %d catalog results above %.2f %s",
            dropped, dropped + len(kept), max_price, CURRENCY,
        )
    return kept, dropped


def _multi_store_search(
    stores: list[str],
    query: str,
    max_price: float | None,
    cursors: dict[str, str] | None = None,
    limit: int = 12,
) -> tuple[list[dict], dict[str, str], bool, dict[str, str]]:
    """Search `stores` for `query`, concurrently when there is more than one.

    Returns (merged_products, degraded_stores, client_missing, next_cursors).
    `degraded_stores` maps store -> failure reason for stores whose search
    raised; stores that returned (even an empty list) successfully are not
    included in it. `cursors` optionally supplies a per-store continuation
    cursor (from a prior call's `next_cursors`) for "show more" / "load more";
    omit it for a fresh first-page search. `next_cursors` only contains
    entries for stores that reported another page.
    """
    client = _load_ucp()
    if client is None:
        return [], {}, True, {}

    cursors = cursors or {}

    def _search_one(store: str) -> tuple[str, list[dict], str | None, BaseException | None]:
        try:
            # Real signature: search_products_page(store, query, max_price=None,
            # limit=10, cursor=None) -> (products, next_cursor). Over-fetch, because
            # the server-side price filter is only a hint and our own hard filter
            # below will discard the strays.
            products, next_cursor = client.search_products_page(
                store=store, query=query, max_price=max_price,
                limit=limit, cursor=cursors.get(store),
            )
            return store, [_fields(p, PRODUCT_FIELDS) for p in (products or [])], next_cursor, None
        except Exception as exc:  # noqa: BLE001 — captured per-store, not re-raised
            return store, [], None, exc

    if len(stores) > 1:
        with ThreadPoolExecutor(max_workers=min(len(stores), 8)) as pool:
            outcomes = list(pool.map(_search_one, stores))
    else:
        outcomes = [_search_one(s) for s in stores]

    products: list[dict] = []
    degraded_stores: dict[str, str] = {}
    next_cursors: dict[str, str] = {}
    for store, prods, next_cursor, exc in outcomes:
        if exc is not None:
            degraded_stores[store] = _mark_degraded("ucp", f"search_products[{store}]", exc)
        else:
            products.extend(prods)
            if next_cursor:
                next_cursors[store] = next_cursor
    return products, degraded_stores, False, next_cursors


def _more_products(
    stores: list[str],
    query: str,
    max_price: float | None,
    cursors: dict[str, str],
    shown_ids: set[str],
    cap: int = MAX_SHOWN_PRODUCTS,
) -> tuple[list[dict], dict[str, str]]:
    """Fetch the next page(s) for an existing "show more" / "load more" session.

    Dedupes against `shown_ids` (a product can reappear across store pages) and
    trims to whatever room is left under `cap`. Returns (fresh_products,
    next_cursors) — call sites should merge next_cursors into their stored
    cursor map and add the fresh ids to shown_ids.
    """
    if not cursors:
        return [], {}
    products, _degraded_stores, _client_missing, next_cursors = _multi_store_search(
        stores, query, max_price, cursors=cursors
    )
    products, _dropped = _within_budget(products, max_price)
    fresh = [p for p in products if p.get("id") not in shown_ids]
    room = max(0, cap - len(shown_ids))
    return fresh[:room], next_cursors


def _tool_search_products(conv: Conversation, args: dict) -> dict:
    store_arg = (args.get("store") or "").strip() or None
    query = args.get("query") or ""
    max_price = _parse_amount(args.get("max_price"))
    if conv.budget is not None:
        max_price = min(max_price, conv.budget) if max_price else conv.budget

    # Omitting `store` fans the search out across every configured store at once.
    stores = [store_arg] if store_arg else list(UCP_STORES)
    products, degraded_stores, client_missing, next_cursors = _multi_store_search(
        stores, query, max_price
    )

    products, dropped = _within_budget(products, max_price)
    products = products[:12]

    result: dict[str, Any] = {"stores_searched": stores}
    all_failed = client_missing or (bool(degraded_stores) and len(degraded_stores) == len(stores))
    if all_failed:
        # Only fake results when every store was unusable — an empty result from a
        # WORKING catalog is real information ("nothing in budget"), not a failure.
        fallback_store = stores[0] if stores else DEFAULT_STORE
        products = _within_budget(_stub_products(fallback_store, query, max_price), max_price)[0]
        result["stub"] = True
        result["degraded"] = True
        result["degraded_reason"] = "; ".join(degraded_stores.values()) if degraded_stores else (
            MODULE_STATUS["ucp"]["detail"] or "UCP catalog client is unavailable"
        )
        result["warning"] = (
            "These products are PLACEHOLDERS, not real catalog data — tell the buyer."
        )
    elif degraded_stores:
        # Some stores worked, some didn't — the results are still real, just partial.
        result["degraded_stores"] = degraded_stores

    for p in products:
        price = _parse_amount(p.get("price"))
        if price is not None and p.get("product_url"):
            conv.prices[p["product_url"]] = price

    result["count"] = len(products)
    result["products"] = products
    if max_price is not None:
        result["max_price_enforced"] = max_price
    if dropped:
        result["filtered_out_over_budget"] = dropped
    if not products and not result.get("stub"):
        where = f" across {', '.join(stores)}" if len(stores) > 1 else ""
        result["message"] = (
            f"The catalog{where} returned nothing at or below {max_price:.2f} {CURRENCY} for "
            f"{query!r}. Try a different search, or ask the buyer to raise the budget."
            if max_price is not None
            else f"The catalog{where} returned no products for {query!r}."
        )

    # "Show more like this" pagination state — only for real (non-stub) results;
    # a stub search has no cursor and nothing more to page through.
    if not result.get("stub"):
        shown_ids = {p["id"] for p in products if p.get("id")}
        conv.last_search = {
            "query": query, "max_price": max_price, "stores": stores,
            "cursors": next_cursors, "shown_ids": shown_ids,
        }
        result["has_more"] = bool(next_cursors) and len(shown_ids) < MAX_SHOWN_PRODUCTS
    return result


def _tool_get_product(conv: Conversation, args: dict) -> dict:
    store = args.get("store") or DEFAULT_STORE
    product_id = args.get("product_id") or ""
    client = _load_ucp()
    product: dict | None = None
    degraded: str | None = None
    if client is not None:
        try:
            # Real signature: get_product(store, product_id).
            raw = client.get_product(store=store, product_id=product_id)
            product = _fields(raw, PRODUCT_FIELDS) if raw else None
        except Exception as exc:
            degraded = _mark_degraded("ucp", "get_product", exc)
    stubbed = product is None
    if not product:
        product = _stub_products(store, product_id or "gift", conv.budget, limit=1)[0]
        product["id"] = product_id or product["id"]
    price = _parse_amount(product.get("price"))
    if price is not None and product.get("product_url"):
        conv.prices[product["product_url"]] = price
    result: dict[str, Any] = {"product": product}
    if stubbed:
        result["stub"] = True
        result["degraded"] = True
        result["degraded_reason"] = degraded or (
            MODULE_STATUS["ucp"]["detail"] or "UCP catalog client is unavailable"
        )
    if conv.budget is not None and price is not None and price > conv.budget + 1e-9:
        result["over_budget"] = True
        result["message"] = (
            f"This product costs {price:.2f} {CURRENCY}, above the approved budget of "
            f"{conv.budget:.2f} {CURRENCY}. Minting for it will be refused."
        )
    return result


# ------------------------------------------------------------- product detail
# (GET /product — the browsing modal/bottom-sheet on both buyer chat and the
# recipient page. Wraps ucp.client.get_product_full: a real UCP call, no LLM.)


_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"(?i)<br\s*/?>")
_BLOCK_CLOSE_RE = re.compile(r"(?i)</(p|div|li)\s*>")
_BLANKLINES_RE = re.compile(r"\n{3,}")


def _strip_html(raw: Any) -> str:
    """Merchant descriptions arrive as `{"html": "..."}` (occasionally a bare
    string). Render to plain text: block/line breaks become newlines, every
    other tag is dropped, entities are unescaped."""
    if isinstance(raw, dict):
        text = raw.get("html") or raw.get("text") or ""
    elif isinstance(raw, str):
        text = raw
    else:
        text = ""
    if not text:
        return ""
    text = _BR_RE.sub("\n", text)
    text = _BLOCK_CLOSE_RE.sub("\n\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = _BLANKLINES_RE.sub("\n\n", text)
    return text.strip()


def _minor_to_price(amount: Any) -> str | None:
    """UCP catalog prices are integer minor units, same convention as
    ucp.client._format_amount, kept local so this module doesn't reach into
    another module's private helper."""
    if amount is None:
        return None
    try:
        return f"{float(amount) / 100:.2f}"
    except (TypeError, ValueError):
        return None


def _variant_label(variant: dict) -> str:
    labels = [
        o.get("label") for o in (variant.get("options") or [])
        if o.get("label") and o.get("label") != "Default Title"
    ]
    if labels:
        return " / ".join(labels)
    return variant.get("title") or "Option"


def _build_variant_chips(raw: dict, fallback_price: str | None, fallback_currency: str | None) -> list[dict]:
    """Read-only variant chips (sizes/colors) for the detail modal.

    UCP's `get_product` is documented to return every option VALUE (e.g. all
    of S/M/L/XL/XXL) under `options`, but — at least for the live merchants
    checked here — only the currently-selected variant's own price/
    availability under `variants`. We merge the two: a chip whose label
    matches a returned variant gets that variant's real price/availability;
    every other chip falls back to the product's own price and is treated as
    available (no live stock signal for it, and this is a browsing surface,
    not checkout — final availability is re-checked wherever a purchase
    actually happens).
    """
    variants_raw = raw.get("variants") or []
    options_raw = raw.get("options") or []
    variant_by_label = {_variant_label(v): v for v in variants_raw}

    chips: list[dict] = []
    if len(options_raw) == 1 and options_raw[0].get("name") not in (None, "Title"):
        for value in options_raw[0].get("values") or []:
            label = value.get("label")
            if not label:
                continue
            matched = variant_by_label.get(label)
            price_obj = (matched or {}).get("price") or {}
            chips.append({
                "id": (matched or {}).get("id"),
                "label": label,
                "price": _minor_to_price(price_obj.get("amount")) or fallback_price,
                "currency": price_obj.get("currency") or fallback_currency,
                "available": (
                    bool((matched.get("availability") or {}).get("available"))
                    if matched else bool(value.get("available", True))
                ),
            })
    else:
        for v in variants_raw:
            price_obj = v.get("price") or {}
            chips.append({
                "id": v.get("id"),
                "label": _variant_label(v),
                "price": _minor_to_price(price_obj.get("amount")) or fallback_price,
                "currency": price_obj.get("currency") or fallback_currency,
                "available": bool((v.get("availability") or {}).get("available")),
            })

    # A single "Default Title"/"Option" chip is not a real choice — suppress it.
    if len(chips) <= 1 and all(c["label"] in ("Default Title", "Option") for c in chips):
        return []
    return chips


def _build_product_detail(
    product: Any, raw: dict, budget: float | None
) -> dict:
    """Combine the normalized Product (price/variant already picked as
    "cheapest in stock") with the raw UCP payload (all images, every variant,
    description) into what the detail modal needs."""
    fields = _fields(product, PRODUCT_FIELDS)

    images = [m.get("url") for m in (raw.get("media") or []) if m.get("url")]
    if not images and fields.get("image_url"):
        images = [fields["image_url"]]

    detail: dict[str, Any] = {
        **fields,
        "images": images,
        "description": _strip_html(raw.get("description")),
        "variants": _build_variant_chips(raw, fields.get("price"), fields.get("currency")),
    }

    price = _parse_amount(fields.get("price"))
    if budget is not None and price is not None:
        detail["budget_headroom"] = {
            "budget": budget,
            "spend": price,
            "remaining": round(budget - price, 2),
            "currency": fields.get("currency") or CURRENCY,
        }
    return detail


def _tool_mint_scoped_card(conv: Conversation, args: dict) -> dict:
    if conv.budget is None:
        return {"error": "No budget on record. Call set_gift_context first."}
    amount = _parse_amount(args.get("amount"))
    if amount is None or amount <= 0:
        return {"error": "amount must be a positive decimal string."}

    # HARD BUDGET GUARD — enforced here, not in the prompt.
    if amount > conv.budget + 1e-9:
        log.warning("budget guard blocked mint: %.2f > %.2f", amount, conv.budget)
        return {
            "error": "budget_exceeded",
            "message": (
                f"Refused: {amount:.2f} {CURRENCY} is above the approved budget of "
                f"{conv.budget:.2f} {CURRENCY}. Pick something within budget or ask the "
                f"buyer to raise the budget explicitly."
            ),
            "requested_amount": amount, "budget": conv.budget,
        }

    merchant_name = args.get("merchant_name") or ""
    merchant_url = args.get("merchant_url") or ""
    description = args.get("description") or "Gift"
    amount_str = f"{amount:.2f}"

    session: dict[str, Any] | None = None
    stub = False
    degraded: str | None = None
    client = _load_prava()
    if client is not None:
        try:
            # Real signature (WP1): create_session(user_id, user_email, total_amount,
            # currency, merchant_name, merchant_url, country_code_iso2, product_details,
            # description=None, effective_until_minutes=15, integration_type="full_checkout").
            # country_code_iso2 and product_details are REQUIRED — omitting them used to
            # raise TypeError and silently drop the whole flow onto the stub.
            raw = client.create_session(
                user_id=BUYER_ID,
                user_email=BUYER_EMAIL,
                total_amount=amount_str,
                currency=CURRENCY,
                merchant_name=merchant_name,
                merchant_url=merchant_url,
                country_code_iso2=MERCHANT_COUNTRY,
                product_details=[
                    {"description": description, "unit_price": amount_str, "quantity": 1}
                ],
                description=description,
            )
            session = _fields(raw, ("session_id", "iframe_url", "expires_at"))
        except Exception as exc:
            degraded = _mark_degraded("prava", "create_session", exc)
            session = None
    if not session or not session.get("session_id"):
        stub = True
        sid = f"stub_sess_{uuid.uuid4().hex[:10]}"
        session = {
            "session_id": sid,
            "iframe_url": f"https://sandbox.prava.space/stub/checkout/{sid}",
            "expires_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 15 * 60)
            ),
        }

    conv.minted[session["session_id"]] = {
        "merchant_name": merchant_name, "merchant_url": merchant_url,
        "amount": amount, "iframe_url": session["iframe_url"],
        "expires_at": session["expires_at"], "credential": None,
        "txn_ref_id": None, "polls": 0, "stub": stub, "completed": False,
    }
    log.info("minted scoped card session=%s amount=%.2f stub=%s",
             session["session_id"], amount, stub)
    result = dict(session)
    result["amount"] = amount_str
    result["currency"] = CURRENCY
    result["next_step"] = "Ask the buyer to approve in the Prava window, then poll get_card_status."
    if stub:
        result["stub"] = True
        result["degraded"] = True
        result["degraded_reason"] = degraded or (
            MODULE_STATUS["prava"]["detail"] or "Prava client is unavailable"
        )
        result["warning"] = (
            "SIMULATED payment session — no real Prava card was minted. Tell the buyer."
        )
    return result


def _tool_get_card_status(conv: Conversation, args: dict) -> dict:
    session_id = args.get("session_id") or ""
    record = conv.minted.get(session_id)
    if record is None:
        return {"error": f"Unknown session_id {session_id!r}. Mint a card first."}
    if record["credential"]:
        return {"status": "approved", "ready": True, "txn_ref_id": record["txn_ref_id"]}

    record["polls"] += 1
    credential: dict | None = None
    degraded: str | None = None
    client = None if record["stub"] else _load_prava()
    if client is not None:
        try:
            # Real signature (WP1): wait_for_result(session_id, timeout_seconds=120.0,
            # poll_interval=3.0). It RETURNS the last PaymentResult on timeout rather
            # than raising, so an exception here is a genuine failure, not "pending".
            # `timeout=` used to be dropped by the flexible adapter, which meant each
            # poll blocked the HTTP request for the full two minutes.
            result = client.wait_for_result(
                session_id=session_id,
                timeout_seconds=CARD_POLL_TIMEOUT,
                poll_interval=2.0,
            )
            if result is not None:
                raw = result.first_credential() if hasattr(result, "first_credential") else result
                if raw:
                    credential = _fields(
                        raw,
                        ("token", "dynamic_cvv", "expiry_month", "expiry_year", "txn_ref_id"),
                    )
        except Exception as exc:
            degraded = _mark_degraded("prava", "wait_for_result", exc)
    elif record["stub"] and record["polls"] >= 2:
        # Stub: first poll is pending (buyer is approving), then it clears.
        credential = {
            "token": "4111111111111111", "dynamic_cvv": "123",
            "expiry_month": "12", "expiry_year": "27",
            "txn_ref_id": f"stub_txn_{uuid.uuid4().hex[:8]}",
        }

    if credential and credential.get("token"):
        # Server-side only. Never returned to the model, never logged.
        record["credential"] = credential
        record["txn_ref_id"] = credential.get("txn_ref_id")
        log.info("credential received for session=%s (not logged)", session_id)
        return {"status": "approved", "ready": True, "txn_ref_id": record["txn_ref_id"],
                **({"stub": True} if record["stub"] else {})}

    return {
        "status": "pending", "ready": False,
        "message": "Buyer has not finished approving yet. Ask them to complete the Prava window.",
        **({"stub": True} if record["stub"] else {}),
        **({"degraded": True, "degraded_reason": degraded} if degraded else {}),
    }


def _tool_complete_checkout(conv: Conversation, args: dict) -> dict:
    session_id = args.get("session_id") or ""
    product_url = args.get("product_url") or ""
    record = conv.minted.get(session_id)
    if record is None:
        return {"error": f"Unknown session_id {session_id!r}. Mint a card first."}
    if record["completed"]:
        return {"error": "This card was already used. One-time cards cannot be replayed."}
    if not record["credential"]:
        return {"error": "Card not approved yet. Call get_card_status until ready is true."}

    # Re-check the guard at spend time.
    price = conv.prices.get(product_url)
    if conv.budget is not None:
        if price is not None and price > conv.budget + 1e-9:
            return {"error": "budget_exceeded",
                    "message": f"Refused: that product costs {price:.2f} {CURRENCY}, above the "
                               f"{conv.budget:.2f} {CURRENCY} budget."}
        if record["amount"] > conv.budget + 1e-9:
            return {"error": "budget_exceeded",
                    "message": "Refused: the minted amount is above the approved budget."}
    if record["merchant_url"] and not _same_merchant(product_url, record["merchant_url"]):
        return {
            "error": "merchant_scope",
            "message": (f"Refused: this card is scoped to {record['merchant_url']} and cannot be "
                        f"used at {product_url}."),
        }

    credential = record["credential"]
    outcome: dict[str, Any]
    run_checkout = _load_checkout()
    # CHECKOUT_TOOL_DRY_RUN=1 fills the cart/form but stops before clicking Pay —
    # required for rehearsals so no real order is ever placed.
    dry_run = os.getenv("CHECKOUT_TOOL_DRY_RUN", "0") == "1"
    if run_checkout is not None:
        try:
            # Real signature (WP3): run_shopify_checkout(token, dynamic_cvv, expiry_month,
            # expiry_year, product_url=None, contact_email=..., headless=None, timeout_ms=...,
            # address_overrides=None, dry_run=False). Returns a CheckoutResult; payment
            # declines come back as a result, only setup problems raise CheckoutError.
            raw = run_checkout(
                token=credential["token"],
                dynamic_cvv=credential["dynamic_cvv"],
                expiry_month=credential["expiry_month"],
                expiry_year=credential["expiry_year"],
                product_url=product_url,
                dry_run=dry_run,
            )
            outcome = _fields(raw, ("success", "order_id", "status", "message"))
            if dry_run:
                outcome["dry_run"] = True
        except Exception as exc:
            if type(exc).__name__ == "CheckoutError":
                # A by-design refusal (non dev-store host, missing config) — the
                # module is healthy, the request was not allowed.
                log.warning("checkout refused for session=%s: %s", session_id, exc)
                outcome = {"success": False, "order_id": None, "status": "refused",
                           "message": str(exc)}
            else:
                reason = _mark_degraded("checkout", "run_shopify_checkout", exc)
                outcome = {"success": False, "order_id": None, "status": "failed",
                           "message": f"Checkout error: {exc}",
                           "degraded": True, "degraded_reason": reason}
    else:
        outcome = {"success": True, "order_id": f"STUB-{uuid.uuid4().hex[:6].upper()}",
                   "status": "paid", "message": "Stubbed checkout (WP3 not present).",
                   "stub": True, "degraded": True,
                   "degraded_reason": MODULE_STATUS["checkout"]["detail"]
                                      or "checkout module unavailable",
                   "warning": "SIMULATED order — nothing was actually purchased."}

    # Always report back to Prava, or the txn sticks in awaiting_result.
    client = _load_prava()
    if client is not None and record["txn_ref_id"]:
        try:
            # Real signature (WP1): report_status(session_id, txn_ref_id, status),
            # where status is exactly "APPROVED" or "DECLINED".
            client.report_status(
                session_id=session_id,
                txn_ref_id=record["txn_ref_id"],
                status="APPROVED" if outcome.get("success") else "DECLINED",
            )
        except Exception as exc:
            _mark_degraded("prava", "report_status", exc)
            outcome["report_status_failed"] = str(exc)

    record["completed"] = True
    record["credential"] = None  # burn the one-time credential
    log.info("checkout session=%s success=%s order=%s",
             session_id, outcome.get("success"), outcome.get("order_id"))
    outcome["amount"] = f"{record['amount']:.2f}"
    outcome["currency"] = CURRENCY
    outcome["merchant"] = record["merchant_name"]
    return outcome


def _tool_create_gift_link(conv: Conversation, args: dict) -> dict:
    """Mode B: mint an unguessable link the recipient can use to pick their own gift."""
    if conv.budget is None:
        return {"error": "No budget on record. Call set_gift_context first."}
    note = (args.get("note") or conv.note or "").strip()
    token = secrets.token_urlsafe(24)
    GIFTS[token] = {
        "budget": conv.budget,
        "note": note,
        "recipient": conv.recipient,
        "stores": list(UCP_STORES),
        "buyer_conversation_id": conv.id,
        "status": "awaiting_pick",
        "picked_product": None,
        "last_search": None,  # set by /gift/{token}/search — pagination state for /more
    }
    log.info("created gift link token=%s… budget=%.2f", token[:8], conv.budget)
    return {
        "ok": True,
        "gift_url": f"/gift/{token}",
        "token": token,
        "message": "Share this link with the recipient so they can pick their own gift.",
    }


def _tool_get_gift_status(conv: Conversation, args: dict) -> dict:
    """Mode B: has the recipient picked anything yet on this gift link?"""
    token = args.get("token") or ""
    gift = GIFTS.get(token)
    if gift is None:
        return {"error": f"Unknown gift token {token!r}."}
    result: dict[str, Any] = {
        "status": gift["status"],
        "picked": gift["picked_product"] is not None,
    }
    if gift["picked_product"]:
        result["picked_product"] = gift["picked_product"]
    return result


TOOL_HANDLERS = {
    "set_gift_context": _tool_set_gift_context,
    "search_products": _tool_search_products,
    "get_product": _tool_get_product,
    "mint_scoped_card": _tool_mint_scoped_card,
    "get_card_status": _tool_get_card_status,
    "complete_checkout": _tool_complete_checkout,
    "create_gift_link": _tool_create_gift_link,
    "get_gift_status": _tool_get_gift_status,
}


def dispatch_tool(conv: Conversation, name: str, args: dict) -> dict:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"error": f"Unknown tool {name!r}."}
    if name != "set_gift_context" and conv.budget is None:
        return {"error": "Call set_gift_context with the recipient and budget first."}
    try:
        return handler(conv, args or {})
    except Exception as exc:  # never crash the loop on a tool bug
        log.exception("tool %s raised", name)
        return {"error": f"{name} failed: {exc}"}


# ----------------------------------------------------------------- Claude loop


_anthropic_client: Any | None = None


def get_anthropic_client() -> Any:
    global _anthropic_client
    if _anthropic_client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise HTTPException(
                status_code=503,
                detail="ANTHROPIC_API_KEY is not set — add it to .env and restart the server.",
            )
        import anthropic

        _anthropic_client = anthropic.Anthropic(api_key=api_key)
    return _anthropic_client


def _serialize_block(block: Any) -> dict | None:
    kind = getattr(block, "type", None)
    if kind == "text":
        return {"type": "text", "text": block.text}
    if kind == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return None  # thinking / unknown blocks are not replayed


def run_agent_turn(conv: Conversation, user_message: str) -> dict[str, Any]:
    """Run the tool-use loop for one buyer message. Returns the /chat payload."""
    client = get_anthropic_client()
    conv.messages.append({"role": "user", "content": user_message})

    texts: list[str] = []
    cards: list[dict] = []
    action: dict | None = None
    has_more = False

    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=conv.messages,
        )
        blocks = list(response.content or [])
        assistant_content = [b for b in (_serialize_block(x) for x in blocks) if b]
        if assistant_content:
            conv.messages.append({"role": "assistant", "content": assistant_content})

        tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
        for block in blocks:
            if getattr(block, "type", None) == "text" and block.text.strip():
                texts.append(block.text.strip())
        if not tool_uses:
            break

        tool_results = []
        for block in tool_uses:
            result = dispatch_tool(conv, block.name, block.input or {})
            if block.name == "search_products":
                cards = result.get("products", []) or cards
                has_more = bool(result.get("has_more"))
            elif block.name == "get_product" and result.get("product"):
                cards = [result["product"]]
            elif block.name == "mint_scoped_card" and result.get("session_id"):
                action = {"type": "approve_payment", "iframe_url": result["iframe_url"],
                          "session_id": result["session_id"]}
            elif block.name == "complete_checkout" and result.get("success"):
                action = {"type": "receipt", "order_id": result.get("order_id"),
                          "amount": result.get("amount"), "merchant": result.get("merchant")}
            elif block.name == "create_gift_link" and result.get("gift_url"):
                action = {"type": "gift_link", "url": result["gift_url"], "token": result["token"]}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
                "is_error": bool(result.get("error")),
            })
        conv.messages.append({"role": "user", "content": tool_results})

    reply = "\n\n".join(texts) or "I'm still working on that — could you say that again?"
    return {
        "reply": reply,
        "cards": cards or None,
        "action": action,
        "budget": f"{conv.budget:.0f}" if conv.budget is not None else None,
        # True when the search that produced `cards` has another page — drives the
        # "Show more like this" chip. False (never null) so the UI never has to guess.
        "has_more": has_more,
    }


# ------------------------------------------------------------------------- app


class ChatRequest(BaseModel):
    conversation_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str
    cards: list[dict] | None = None
    action: dict | None = None
    # Optional; WP5's header chip updates from this when present.
    budget: str | None = None
    # WP-BROWSE: whether the "Show more like this" chip should render under cards.
    has_more: bool = False


class GiftSearchRequest(BaseModel):
    query: str
    store: str | None = None  # None/omitted = every configured store (unchanged default)


class GiftPickRequest(BaseModel):
    product_id: str
    title: str
    price: str


app = FastAPI(title="Agentic Gifting")


@app.middleware("http")
async def disable_cache_middleware(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/health")
def health() -> dict:
    """Prove the spine is not quietly running on stubs.

    `modules` is import-level truth (did the real work package load?), while
    `degraded` records whether any real module has since blown up at call time.
    """
    modules = probe_modules()
    detail = {}
    for name in MODULE_NAMES:
        status = MODULE_STATUS[name]
        entry = {k: status[k] for k in ("detail", "degraded", "last_error") if status[k]}
        if entry:
            detail[name] = entry
    payload: dict[str, Any] = {
        "ok": True,
        "modules": modules,
        "degraded": any(MODULE_STATUS[n]["degraded"] for n in MODULE_NAMES),
        "model": MODEL,
        "anthropic_key": bool(os.getenv("ANTHROPIC_API_KEY")),
    }
    if detail:
        payload["module_detail"] = detail
    return payload


@app.get("/")
def index():
    if INDEX_HTML.is_file():
        return FileResponse(str(INDEX_HTML))
    return PlainTextResponse(
        "Agentic Gifting backend is running. The chat UI (WP5) is not built yet.\n"
        "POST /chat  {conversation_id, message}\n"
        "GET  /health\n"
    )


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    conv = get_conversation(req.conversation_id)
    payload = run_agent_turn(conv, req.message)
    return ChatResponse(**payload)


@app.post("/chat/{conversation_id}/more")
def chat_more(conversation_id: str) -> dict:
    """"Show more like this" — appends the next page to the last search's
    card row. Deliberately bypasses the Claude tool loop entirely (no LLM
    call): this is plain pagination over the same query/stores/budget the
    agent already searched with."""
    conv = CONVERSATIONS.get(conversation_id)
    if conv is None or not conv.last_search:
        raise HTTPException(status_code=404, detail="No previous search to continue from.")
    ls = conv.last_search
    fresh, next_cursors = _more_products(
        ls["stores"], ls["query"], ls["max_price"], ls["cursors"], ls["shown_ids"]
    )
    ls["cursors"] = next_cursors
    ls["shown_ids"] |= {p["id"] for p in fresh if p.get("id")}
    return {
        "products": fresh,
        "has_more": bool(next_cursors) and len(ls["shown_ids"]) < MAX_SHOWN_PRODUCTS,
    }


@app.get("/product")
def product_detail(store: str, id: str, budget: float | None = None) -> dict:
    """Real UCP `get_product` call (no LLM) for the browsing detail modal/sheet
    on both buyer chat and the recipient page: every image, every variant
    (with availability), plain-text description, and budget headroom."""
    client = _load_ucp()
    if client is None:
        raise HTTPException(status_code=503, detail="Catalog service is unavailable.")
    try:
        product, raw = client.get_product_full(store, id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not load that product: {exc}")
    return _build_product_detail(product, raw, budget)


# ------------------------------------------------------------- Mode B: gift links


def _get_gift_or_404(token: str) -> dict:
    gift = GIFTS.get(token)
    if gift is None:
        raise HTTPException(status_code=404, detail="This gift link is invalid or has expired.")
    return gift


@app.get("/gift/{token}")
def gift_page(token: str):
    """Recipient-facing page. The token itself is the only lookup key — no auth,
    no cookies, no reference back to the buyer's conversation."""
    if GIFT_HTML.is_file():
        return FileResponse(str(GIFT_HTML))
    return PlainTextResponse("Recipient page is not built yet.", status_code=404)


@app.get("/gift/{token}/info")
def gift_info(token: str) -> dict:
    """Everything the recipient page is allowed to see: note, budget, pick status,
    and which stores are searchable (for the store toggle). Never the buyer's
    conversation id."""
    gift = _get_gift_or_404(token)
    return {
        "budget": gift["budget"],
        "currency": CURRENCY,
        "note": gift["note"],
        "status": gift["status"],
        "picked_product": gift["picked_product"],
        "stores": gift["stores"],
    }


@app.post("/gift/{token}/search")
def gift_search(token: str, req: GiftSearchRequest) -> dict:
    """Real, server-side UCP search across the gift's store set (or one store,
    for the store toggle), hard budget-filtered. The recipient never gets tool
    access — this is a plain product search, nothing more."""
    gift = _get_gift_or_404(token)
    max_price = gift["budget"]

    store_filter = (req.store or "").strip() or None
    if store_filter and store_filter not in gift["stores"]:
        raise HTTPException(status_code=400, detail=f"{store_filter!r} is not one of this gift's stores.")
    stores = [store_filter] if store_filter else gift["stores"]

    products, degraded_stores, client_missing, next_cursors = _multi_store_search(
        stores, req.query, max_price
    )
    products, dropped = _within_budget(products, max_price)
    products = products[:12]

    result: dict[str, Any] = {"stores_searched": stores}
    all_failed = client_missing or (
        bool(degraded_stores) and len(degraded_stores) == len(stores)
    )
    if all_failed:
        fallback_store = stores[0] if stores else DEFAULT_STORE
        products = _within_budget(_stub_products(fallback_store, req.query, max_price), max_price)[0]
        result["stub"] = True

    result["count"] = len(products)
    result["products"] = products
    result["max_price_enforced"] = max_price
    if dropped:
        result["filtered_out_over_budget"] = dropped

    if not result.get("stub"):
        shown_ids = {p["id"] for p in products if p.get("id")}
        gift["last_search"] = {
            "query": req.query, "max_price": max_price, "stores": stores,
            "cursors": next_cursors, "shown_ids": shown_ids,
        }
        result["has_more"] = bool(next_cursors) and len(shown_ids) < MAX_SHOWN_PRODUCTS
    return result


@app.post("/gift/{token}/more")
def gift_more(token: str) -> dict:
    """"Load more" on the recipient's browsing grid — same pagination as the
    buyer chat's "show more" chip, continuing the gift's last search."""
    gift = _get_gift_or_404(token)
    ls = gift.get("last_search")
    if not ls:
        raise HTTPException(status_code=404, detail="No previous search to continue from.")
    fresh, next_cursors = _more_products(
        ls["stores"], ls["query"], ls["max_price"], ls["cursors"], ls["shown_ids"]
    )
    ls["cursors"] = next_cursors
    ls["shown_ids"] |= {p["id"] for p in fresh if p.get("id")}
    return {
        "products": fresh,
        "has_more": bool(next_cursors) and len(ls["shown_ids"]) < MAX_SHOWN_PRODUCTS,
    }


@app.post("/gift/{token}/pick")
def gift_pick(token: str, req: GiftPickRequest) -> dict:
    """Recipient locks in a choice. Price is re-validated against the buyer's budget
    here, in code — the recipient's browser is not trusted to enforce that itself."""
    gift = _get_gift_or_404(token)
    if gift["status"] == "picked":
        return {"ok": False, "error": "A gift has already been picked with this link."}

    price = _parse_amount(req.price)
    if price is None or price <= 0:
        raise HTTPException(status_code=400, detail="price must be a positive amount.")
    if price > gift["budget"] + 1e-9:
        raise HTTPException(
            status_code=400,
            detail=(f"That item costs {price:.2f} {CURRENCY}, above the "
                    f"{gift['budget']:.2f} {CURRENCY} budget."),
        )

    gift["picked_product"] = {
        "product_id": req.product_id, "title": req.title, "price": f"{price:.2f}",
    }
    gift["status"] = "picked"
    log.info("gift token=%s… picked product_id=%s", token[:8], req.product_id)
    return {"ok": True, "status": "picked", "picked_product": gift["picked_product"]}
