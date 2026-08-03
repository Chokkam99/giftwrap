"""Agentic Gifting — FastAPI backend + provider-agnostic tool-use loop (WP4 + WP-INT).

The agent brain is swapped with one env var, `LLM_PROVIDER=anthropic|openai|gemini`
(see `llm/`); this module only speaks the neutral `llm.Msg` / `llm.ToolSpec` shapes.

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

from llm import LLMConfigError, LLMError, Msg, Provider, ToolSpec, get_provider

load_dotenv()

log = logging.getLogger("agentic_gifting")

MAX_TOOL_ITERATIONS = 10
DEFAULT_STORE = os.getenv("UCP_DEFAULT_STORE", "giva-jewelry.myshopify.com")
# Our controlled Shopify dev store. Its country is tracked independently from
# the Indian third-party catalog merchants below.
DEMO_STORE = "agentic-gifting-demo.myshopify.com"
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
UCP_QUERY_FILTER_FALLBACK_STORES: set[str] = set()
# Cap on total products shown across "show more" / "load more" pagination, per
# browsing session (buyer chat conversation, or one recipient gift-link query).
MAX_SHOWN_PRODUCTS = 36
CURRENCY = os.getenv("GIFT_CURRENCY", "INR")
CARD_POLL_TIMEOUT = 10  # seconds — short so the model can poll conversationally
BUYER_ID = os.getenv("PRAVA_USER_ID", "agentic-gifting-buyer")
# Forwarded to the card network during passkey registration. MUST be on a real
# delegated TLD -- a reserved/fake TLD (.local/.test/.demo/.invalid/...) passes
# every step and then fails at the very last one (PASSKEY_REG_FAILED).
# example.com is fine: .com is a real TLD.
BUYER_EMAIL = os.getenv("PRAVA_USER_EMAIL", "gifting-demo@example.com")
# This describes the controlled demo merchant's legal/business country, not the
# shopper's market or checkout currency. Keep the existing variable as a
# backwards-compatible fallback, but prefer the explicit name going forward.
_demo_country_raw = os.getenv("PRAVA_DEMO_MERCHANT_COUNTRY") or os.getenv(
    "PRAVA_MERCHANT_COUNTRY", "US"
)
DEMO_MERCHANT_COUNTRY = _demo_country_raw.strip().upper()
if not re.fullmatch(r"[A-Z]{2}", DEMO_MERCHANT_COUNTRY):
    raise RuntimeError("PRAVA_DEMO_MERCHANT_COUNTRY must be a two-letter ISO country code.")

# Exact metadata for the reviewed UCP catalog merchants. This replaces the
# previous one-global-country request body, which incorrectly sent Salty as US.
MERCHANT_COUNTRY_BY_HOST = {
    "giva-jewelry.myshopify.com": "IN",
    "mamaearth.in": "IN",
    "salty.co.in": "IN",
    "plumgoodness.com": "IN",
    "xyxxcrew.com": "IN",
    DEMO_STORE: DEMO_MERCHANT_COUNTRY,
}

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
   recipient, call `set_gift_context` — no other tool works until you have. When the buyer gives
   gender, age, style, or occasion alongside them (for example, "male, ₹2,000 for his wedding"),
   record those details too and move straight to a search; do not repeat a long questionnaire for
   details they already supplied. If age or style is genuinely missing and would make the search
   much better, ask one short follow-up — never a long checklist.
2. Find out who is picking the gift:
   * BUYER PICKS (the default — assume this unless told otherwise): search with
     `search_products`. Omit `store` to search every configured catalog store at once.
     Include any known occasion, gender, age range, and style in the search terms; the server also
     applies the stored recipient context to rank and filter results. Product cards render
     automatically in the buyer UI:
     after a successful search, reply with one short, natural sentence only (for example,
     "I found a few options within your budget."). Do NOT repeat product titles, prices, IDs,
     URLs, Markdown links, or a numbered list in your text.
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

The buyer's UI can send a structured product selection with their natural confirmation. Treat that
selection as the explicit approval for the exact product. The legacy literal message below may
still arrive from an older UI; recognise it, but never echo its internal product ID.
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
prices — only report what the tools returned. Never reveal implementation details, environment
variables, internal flags, provider diagnostics, raw tool errors, or server setup steps. If a tool
fails, do not invent a workaround or retry: the server will provide a safe buyer-facing message.
If a tool result carries "degraded": true or "stub": true, the live service was unreachable and
the data is fake — say so plainly rather than presenting it as real.
You never see the card number; that is by design."""

def _field(description: str = "", kind: str = "string") -> dict:
    return {"type": kind, "description": description} if description else {"type": kind}


def _tool(name: str, description: str, required: list[str], /, **properties: dict) -> ToolSpec:
    # positional-only, so a tool may itself have a "description" input field.
    # `parameters` is plain JSON Schema; each provider translates it to its own
    # dialect (Gemini strips the keywords it does not support).
    return ToolSpec(
        name=name,
        description=description,
        parameters={"type": "object", "properties": properties, "required": required},
    )


TOOLS: list[ToolSpec] = [
    _tool("set_gift_context",
          "Record the gift context. MUST be called before any other tool. The budget recorded "
          "here is enforced in code: minting above it is rejected.",
          ["budget", "recipient"],
          budget=_field("Max total spend, e.g. '3000' or '₹3,000'."),
          recipient=_field("Who the gift is for."),
          note=_field("Occasion / vibe / preferences."),
          gender=_field("Optional recipient gender or pronouns when the buyer supplied them."),
          age_range=_field("Optional recipient age or age range when the buyer supplied it."),
          occasion=_field("Optional occasion, such as wedding or birthday."),
          preferences=_field("Optional style, interests, and no-gos.")),
    _tool("search_products",
          "Search live catalogs for gift options. If `store` is omitted, all configured "
          "catalog stores are searched concurrently and the results are merged.",
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
    # Provider-neutral history (llm.Msg); each adapter translates it to its own
    # wire format, so switching LLM_PROVIDER changes nothing here.
    messages: list[Msg] = field(default_factory=list)
    budget: float | None = None
    recipient: str | None = None
    note: str | None = None
    # Explicit recipient cues are remembered independently from free-form chat so
    # catalog relevance can be enforced deterministically, not left to an LLM.
    gender: str | None = None
    age_range: str | None = None
    occasion: str | None = None
    preferences: str | None = None
    # session_id -> {merchant_name, merchant_url, amount, iframe_url, expires_at,
    #                credential (server-only), txn_ref_id, polls, stub, completed}
    minted: dict[str, dict[str, Any]] = field(default_factory=dict)
    prices: dict[str, float] = field(default_factory=dict)  # product_url -> price
    # Product cards most recently shown to this buyer. Kept server-side so a card
    # click can submit an opaque product id without rendering or trusting it as prose.
    products: dict[str, dict[str, Any]] = field(default_factory=dict)
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


def _exc_detail(exc: BaseException) -> str:
    """Full provider error detail — never collapse a PravaError to its message.

    PravaError carries `code` and `http_status` that the message alone drops;
    those are exactly what makes a sandbox failure diagnosable.
    """
    detail = f"{type(exc).__name__}: {exc}"
    extras = [
        f"{label}={value}"
        for label, value in (
            ("code", getattr(exc, "code", None)),
            ("http_status", getattr(exc, "http_status", None)),
        )
        if value
    ]
    return f"{detail} ({', '.join(extras)})" if extras else detail


def _mark_degraded(name: str, call: str, exc: BaseException) -> str:
    """Record — LOUDLY — that a real module failed at call time."""
    reason = f"{call}: {_exc_detail(exc)}"
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


def _prava_failure_reason(result: Any) -> str:
    """Human-readable reason for a terminal `failed` Prava payment result.

    Pulls the per-transaction `error` detail through instead of collapsing
    every failure into one generic string.
    """
    details: list[str] = []
    for txn in getattr(result, "transactions", None) or []:
        error = getattr(txn, "error", None)
        if not error:
            continue
        if isinstance(error, dict):
            code = error.get("code") or error.get("type")
            message = error.get("message") or error.get("error")
            details.append(": ".join(str(part) for part in (code, message) if part))
        else:
            details.append(str(error))
    suffix = f" Prava reported: {'; '.join(detail for detail in details if detail)}." if details else ""
    return (
        "The Prava payment session failed and cannot be reused." + suffix +
        " Do NOT retry this transaction — explain the failure to the buyer and stop."
    )


def _normalize_merchant_url(url: str) -> str:
    """Bare-https-origin guard for `merchant_details.url` (see prava.client).

    Delegates to the canonical validator so the rule lives in one place; the
    inline fallback only runs if prava.client is not importable (stub mode),
    and is deliberately the same shape: https + real host + strip path.
    """
    mod = _import("prava.client")
    fn = getattr(mod, "normalize_merchant_url", None) if mod is not None else None
    if fn is not None:
        return fn(url)

    parsed = urlparse((url or "").strip())
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme.lower() != "https" or "." not in hostname:
        raise ValueError(
            f"Invalid merchant_url {url!r}: need a bare https origin on a real domain, "
            f"e.g. https://example.com."
        )
    return f"https://{hostname}" + (f":{parsed.port}" if parsed.port else "")


def _same_merchant(a: str | None, b: str | None) -> bool:
    ha = (urlparse(a or "").hostname or "").lower().removeprefix("www.")
    hb = (urlparse(b or "").hostname or "").lower().removeprefix("www.")
    return bool(ha) and ha == hb


def _merchant_host(url: str) -> str:
    """Return a normalized merchant host without accepting it as a checkout URL."""
    return (urlparse(url or "").hostname or "").rstrip(".").lower().removeprefix("www.")


def merchant_country_for_url(merchant_url: str) -> str | None:
    """Resolve country metadata for a reviewed merchant origin.

    This is deliberately an exact-host map, rather than a guessed TLD or a
    global environment value. A country is legally meaningful merchant data;
    `.in` or a buyer's currency are not enough to infer it for arbitrary hosts.
    """
    return MERCHANT_COUNTRY_BY_HOST.get(_merchant_host(merchant_url))


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


# These are intentionally narrow title signals, not a broad gender classifier.
# A buyer who says the recipient is male should not be shown something explicitly
# sold as a bridal / women's product, but neutral items remain valid options.
MALE_CONTEXT_SIGNALS = re.compile(
    r"\b(?:male|man|men|him|his|groom|husband|boyfriend|father|dad|brother|gentleman)\b",
    re.IGNORECASE,
)
FEMALE_CODED_TITLE_PATTERNS = (
    re.compile(r"\bbride\b", re.IGNORECASE),
    re.compile(r"\bbridal\b", re.IGNORECASE),
    re.compile(r"\bfor\s+her\b", re.IGNORECASE),
    re.compile(r"\bwomen(?:'s)?\b", re.IGNORECASE),
    re.compile(r"\bwoman\b", re.IGNORECASE),
    re.compile(r"\bladies\b", re.IGNORECASE),
    re.compile(r"\blady\b", re.IGNORECASE),
    re.compile(r"\bgirls?\b", re.IGNORECASE),
    re.compile(r"\bmaid\s+of\s+honou?r\b", re.IGNORECASE),
    re.compile(r"\bbachelorette\b", re.IGNORECASE),
    re.compile(r"\bhen\s+party\b", re.IGNORECASE),
)
MALE_AFFINITY_TITLE_PATTERNS = (
    re.compile(r"\bfor\s+him\b", re.IGNORECASE),
    re.compile(r"\bmen(?:'s)?\b", re.IGNORECASE),
    re.compile(r"\bmale\b", re.IGNORECASE),
    re.compile(r"\bgroom\b", re.IGNORECASE),
    re.compile(r"\bhusband\b", re.IGNORECASE),
    re.compile(r"\bboyfriend\b", re.IGNORECASE),
    re.compile(r"\bgent(?:leman)?\b", re.IGNORECASE),
)


def _nonempty_text(value: Any) -> str | None:
    """Normalize optional tool text without turning `None` into literal prose."""
    text = str(value).strip() if value is not None else ""
    return text or None


def _infer_gender(*parts: str | None) -> str | None:
    """Return only a deliberately explicit male cue; otherwise leave gender unknown."""
    context = " ".join(part for part in parts if part)
    return "male" if MALE_CONTEXT_SIGNALS.search(context) else None


def _infer_age_range(*parts: str | None) -> str | None:
    """Persist an explicit age, or a cautious relationship-based range when useful.

    These ranges guide the agent's search phrasing and follow-up judgement; they
    do not pretend that UCP merchant catalogs have reliable age metadata.
    """
    context = " ".join(part for part in parts if part).lower()
    age_match = re.search(
        r"\b(?:turning|age(?:d)?|is)\s*(\d{1,3})\b|\b(\d{1,3})\s*years?\s*old\b",
        context,
    )
    if age_match:
        age = int(age_match.group(1) or age_match.group(2))
        if 0 <= age <= 12:
            return "0–12"
        if age <= 17:
            return "13–17"
        if age <= 24:
            return "18–24"
        if age <= 34:
            return "25–34"
        if age <= 44:
            return "35–44"
        if age <= 64:
            return "45–64"
        if age <= 120:
            return "65+"
    if re.search(r"\b(?:baby|infant|toddler)\b", context):
        return "0–3"
    if re.search(r"\b(?:child|kid|son|daughter)\b", context):
        return "6–12"
    if re.search(r"\b(?:teen|teenager)\b", context):
        return "13–17"
    if re.search(r"\b(?:mom|mother|dad|father|parent)\b", context):
        return "45–64"
    return None


def _filter_and_rank_for_recipient(
    products: list[dict], conv: Conversation | None
) -> tuple[list[dict], int]:
    """Apply a deterministic compatibility filter and a stable relevance ranking.

    This is deliberately post-catalog: merchant UCP schemas do not expose a
    reliable gender facet. We only suppress titles with an explicit incompatible
    signal for a male recipient and otherwise preserve the merchant's ordering.
    """
    if conv is None or conv.gender != "male":
        return products, 0

    compatible: list[tuple[int, int, dict]] = []
    dropped = 0
    for index, product in enumerate(products):
        title = str(product.get("title") or "")
        if any(pattern.search(title) for pattern in FEMALE_CODED_TITLE_PATTERNS):
            dropped += 1
            continue
        affinity = 0 if any(pattern.search(title) for pattern in MALE_AFFINITY_TITLE_PATTERNS) else 1
        compatible.append((affinity, index, product))
    compatible.sort(key=lambda entry: (entry[0], entry[1]))
    return [product for _affinity, _index, product in compatible], dropped


def _tool_set_gift_context(conv: Conversation, args: dict) -> dict:
    budget = _parse_amount(args.get("budget"))
    if budget is None or budget <= 0:
        return {"error": "Could not read a numeric budget. Ask the buyer for an amount."}
    conv.budget = budget
    conv.recipient = _nonempty_text(args.get("recipient")) or conv.recipient
    conv.note = _nonempty_text(args.get("note")) or conv.note
    conv.occasion = _nonempty_text(args.get("occasion")) or conv.occasion
    conv.preferences = _nonempty_text(args.get("preferences")) or conv.preferences
    explicit_gender = _nonempty_text(args.get("gender"))
    explicit_age_range = _nonempty_text(args.get("age_range"))
    inferred_gender = _infer_gender(
        explicit_gender, conv.recipient, conv.note, conv.occasion, conv.preferences
    )
    # Only record a supported, explicit signal. Unknown is safer than guessing.
    if inferred_gender:
        conv.gender = inferred_gender
    conv.age_range = explicit_age_range or _infer_age_range(
        conv.recipient, conv.note, conv.occasion, conv.preferences
    ) or conv.age_range
    return {
        "ok": True, "budget": budget, "currency": CURRENCY,
        "recipient": conv.recipient, "note": conv.note,
        "gender": conv.gender, "age_range": conv.age_range,
        "occasion": conv.occasion, "preferences": conv.preferences,
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
            # See UCP_QUERY_FILTER_FALLBACK_STORES: on first page only (a stale
            # cursor from an empty-query listing wouldn't line up with one from a
            # text query), if a store known to ignore free-text queries came back
            # empty, re-list (still real data, still that store) and filter by
            # keyword ourselves instead of surfacing a false "nothing found".
            if not products and query.strip() and not cursors.get(store) and store in UCP_QUERY_FILTER_FALLBACK_STORES:
                listing, _ = client.search_products_page(store=store, query="", max_price=max_price, limit=limit)
                terms = query.lower().split()
                products = [p for p in listing if any(t in (p.title or "").lower() for t in terms)]
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
    conv: Conversation | None = None,
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
    products, _incompatible_dropped = _filter_and_rank_for_recipient(products, conv)
    # Some merchant pages repeat a product, including within one cursor page.
    # Deduplicate both against already-rendered cards and this new batch.
    seen_ids = set(shown_ids)
    fresh: list[dict] = []
    for product in products:
        product_id = product.get("id")
        if product_id and product_id in seen_ids:
            continue
        if product_id:
            seen_ids.add(product_id)
        fresh.append(product)
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
    products, incompatible_dropped = _filter_and_rank_for_recipient(products, conv)
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
        if p.get("id"):
            conv.products[p["id"]] = dict(p)

    result["count"] = len(products)
    result["products"] = products
    if max_price is not None:
        result["max_price_enforced"] = max_price
    if dropped:
        result["filtered_out_over_budget"] = dropped
    if incompatible_dropped:
        result["filtered_out_incompatible"] = incompatible_dropped
    if not products and not result.get("stub"):
        if incompatible_dropped:
            # Do not fall back to clearly incompatible items merely to populate a
            # card row. This string is passed through as buyer-safe text below.
            result["user_message"] = (
                "I couldn't find a strong match in this store; try another style or store."
            )
            result["message"] = result["user_message"]
        else:
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


def _format_amount(amount: float, currency: str) -> str:
    symbol = "₹" if currency == "INR" else f"{currency} "
    return f"{symbol}{amount:,.0f}" if amount.is_integer() else f"{symbol}{amount:,.2f}"


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
    raw_merchant_url = args.get("merchant_url") or ""
    description = args.get("description") or "Gift"
    amount_str = f"{amount:.2f}"

    # MERCHANT URL GUARD — the model supplies merchant_url, so it routinely
    # hands us a full product URL (…/products/x) or a bare hostname. Prava
    # requires a bare https origin on a real, delegated TLD; anything else
    # fails deep inside checkout. Normalize here so the tool result the model
    # sees is recoverable, and so the same value is what we scope the card to.
    try:
        merchant_url = _normalize_merchant_url(raw_merchant_url)
    except Exception as exc:
        log.warning("mint blocked: bad merchant_url %r — %s", raw_merchant_url, exc)
        return {
            "error": "invalid_merchant_url",
            "message": (
                f"Refused: {exc} Re-call mint_scoped_card with merchant_url set to the store's "
                f"bare https origin (scheme + host only, no path), e.g. https://giva.co — take "
                f"it from the product's own store domain."
            ),
            "error_detail": _exc_detail(exc),
            "supplied_merchant_url": raw_merchant_url,
        }
    if merchant_url != raw_merchant_url:
        log.info("normalized merchant_url %r -> %r", raw_merchant_url, merchant_url)

    merchant_country = merchant_country_for_url(merchant_url)
    if merchant_country is None:
        # Do not guess card-network country metadata for a new/unreviewed merchant.
        log.error("mint refused: no country mapping for host=%s", _merchant_host(merchant_url))
        return {
            "error": "merchant_country_unconfigured",
            "user_message": "We couldn't start the payment approval. Nothing was charged. Please try again.",
        }

    session: dict[str, Any] | None = None
    stub = False
    degraded: str | None = None
    client = _load_prava()
    # REAL-CALL SAFETY GUARD: even when a real PravaClient is loaded, refuse to
    # spend an actual quota slot unless the caller explicitly opted in with
    # PRAVA_ALLOW_REAL=1. This is a hard stop (a tool error), not a silent
    # fallback to the stub — the point is to make accidental local/dev runs
    # against a live key impossible, not to quietly paper over them.
    if client is not None and os.getenv("PRAVA_ALLOW_REAL") != "1":
        log.warning(
            "refusing real Prava mint_scoped_card call: PRAVA_ALLOW_REAL is not set to "
            "'1' (quota-burn guard). Set PRAVA_ALLOW_REAL=1 to allow real calls."
        )
        return {
            "error": "real_prava_calls_disabled",
            # This is deliberately safe for the model too: it must not infer or
            # explain server configuration to a buyer from a tool failure.
            "user_message": (
                "We couldn't start the payment approval. Nothing was charged. "
                "Please try again."
            ),
        }
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
                country_code_iso2=merchant_country,
                product_details=[
                    {"description": description, "unit_price": amount_str, "quantity": 1}
                ],
                description=description,
            )
            session = _fields(raw, ("session_id", "iframe_url", "expires_at"))
        except Exception as exc:
            degraded = _mark_degraded("prava", "create_session", exc)
            error_code = getattr(exc, "code", None)
            error_message = getattr(exc, "message", None)
            detail = ": ".join(str(part) for part in (error_code, error_message) if part)
            return {
                "error": "prava_session_failed",
                "user_message": (
                    "We couldn't start the payment approval. Nothing was charged. "
                    + (f"Prava reported {detail}. " if detail else "")
                    + "Please fix that issue before starting a fresh payment session."
                ),
                # Diagnostics remain server-side; never put raw provider output
                # into the tool result or buyer-facing response.
            }
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
        "merchant_country": merchant_country,
        "amount": amount, "iframe_url": session["iframe_url"],
        "expires_at": session["expires_at"], "credential": None,
        "txn_ref_id": None, "polls": 0, "stub": stub, "completed": False,
        "failed": False, "failed_reason": None,
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

    if record.get("failed"):
        return {
            "status": "failed", "ready": False, "terminal": True,
            "error": "payment_failed",
            "message": record.get("failed_reason") or (
                "This Prava transaction failed. Do NOT retry it — tell the buyer what "
                "happened and stop."
            ),
        }

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
                elif getattr(result, "status", None) == "failed":
                    # Terminal failure. NEVER auto-retry a failed Prava
                    # transaction — latch it so further polls short-circuit
                    # instead of looking like "still pending".
                    reason = _prava_failure_reason(result)
                    record["failed"] = True
                    record["failed_reason"] = reason
                    log.warning("prava session=%s terminal failure: %s", session_id, reason)
                    return {
                        "status": "failed", "ready": False, "terminal": True,
                        "error": "payment_failed", "message": reason,
                    }
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
                # A malformed target or missing checkout configuration — the
                # module is healthy, but this checkout request cannot proceed.
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
            # In the sandbox, a merchant checkout may be verified even when
            # Prava's dashboard later labels/reporting of that attempt failed.
            # The buyer receipt therefore follows the verified Shopify order,
            # not this asynchronous reporting acknowledgement. Keep the raw
            # diagnostic server-side for operators; never hand it to the model.
            _mark_degraded("prava", "report_status", exc)
            record["prava_report_status"] = {
                "state": "failed", "error": _exc_detail(exc),
            }
            log.warning("Prava status report failed after checkout session=%s: %s", session_id, exc)
        else:
            record["prava_report_status"] = {"state": "reported"}

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
        "message": "Perfect — they can now choose a gift. Share the link below, and come back once they’ve picked something.",
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


# ------------------------------------------------------------------- agent loop


def _safe_tool_error_message(result: dict[str, Any]) -> str:
    """Return buyer copy for a failed tool call, never its diagnostic payload."""
    code = result.get("error") or ""
    if result.get("user_message"):
        return str(result["user_message"])
    if code == "budget_exceeded":
        return "That item is above your budget. Pick one of the options within your cap."
    if code in {"merchant_scope", "invalid_merchant_url"}:
        return "We couldn't start payment for that item. Nothing was charged. Please choose another option."
    if code == "payment_failed":
        return str(result.get("message") or (
            "We couldn't complete the payment. Nothing was charged. "
            "Do not retry this payment session."
        ))
    if code:
        return "Something went wrong with that step. Nothing was charged. Please try again."
    return "Something went wrong. Please try again."


def _sanitize_buyer_reply(text: str) -> str:
    """Defence in depth: internal IDs, URLs, and environment flags never belong in chat copy."""
    text = text or ""
    # Keep a human label if a model emitted a Markdown product link, but never
    # put merchant URLs into a chat bubble (cards and the detail modal own it).
    text = re.sub(r"\[([^\]]+)\]\(https?://[^\s)]+\)", r"\1", text)
    text = re.sub(r"\b/?gift/[^\s)\]}]+", "", text)
    text = re.sub(r"(?:https?://|www\.)[^\s)\]}]+", "", text)
    text = re.sub(r"gid://[^\s)\]}]+", "", text)
    text = re.sub(r"\b(?:PRAVA|UCP|LLM|ANTHROPIC|OPENAI|GEMINI)_[A-Z0-9_]+\b", "", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _short_search_reply(cards: list[dict], budget: float | None) -> str:
    if not cards:
        return "I couldn't find an option within that budget. Want to try a different search?"
    if budget is None:
        return "I found a few gift options for you."
    return f"I found a few options within {_format_amount(budget, CURRENCY)}."


def _gift_link_reply(budget: float | None) -> str:
    if budget is None:
        return "Perfect — they can now choose a gift within budget. Share the link below, and come back once they’ve picked something."
    return (
        f"Perfect — they can now choose a gift within {_format_amount(budget, CURRENCY)}. "
        "Share the link below, and come back once they’ve picked something."
    )


def _selection_product(conv: Conversation, selection: "ProductSelection") -> dict[str, Any] | None:
    """Look up a card server-side; client-provided title/price are display hints only."""
    return conv.products.get(selection.id)


def approve_selection_turn(conv: Conversation, selection: "ProductSelection") -> dict[str, Any]:
    """Handle a card click without routing opaque product IDs through chat prose or an LLM."""
    product = _selection_product(conv, selection)
    if product is None:
        return {
            "reply": "That option is no longer available. Please choose one from the current results.",
            "cards": None, "action": None,
            "budget": f"{conv.budget:.0f}" if conv.budget is not None else None,
            "has_more": False,
        }

    product_url = product.get("product_url") or ""
    parsed = urlparse(product_url)
    merchant_url = f"https://{parsed.netloc}" if parsed.netloc else f"https://{product.get('merchant', '')}"
    result = _tool_mint_scoped_card(conv, {
        "merchant_name": product.get("merchant") or "GiftWrap merchant",
        "merchant_url": merchant_url,
        "amount": product.get("price"),
        "description": product.get("title") or "Gift",
    })
    if result.get("error"):
        return {
            "reply": _safe_tool_error_message(result), "cards": None, "action": None,
            "budget": f"{conv.budget:.0f}" if conv.budget is not None else None,
            "has_more": False,
        }
    return {
        "reply": "Great choice. Approve the payment in the secure window to continue.",
        "cards": None,
        "action": {"type": "approve_payment", "iframe_url": result["iframe_url"],
                   "session_id": result["session_id"]},
        "budget": f"{conv.budget:.0f}" if conv.budget is not None else None,
        "has_more": False,
    }


# The active LLM adapter (anthropic | openai | gemini, picked by $LLM_PROVIDER).
# Built once and cached: the provider object is stateless, and its SDK client is
# created lazily on the first real call.
_llm: Provider | None = None


def get_llm() -> Provider:
    global _llm
    if _llm is None:
        _llm = get_provider()
    return _llm


def system_prompt() -> str:
    """Shared prompt plus the active provider's optional addendum."""
    return SYSTEM_PROMPT + get_llm().prompt_suffix


def run_agent_turn(conv: Conversation, user_message: str) -> dict[str, Any]:
    """Run the tool-use loop for one buyer message. Returns the /chat payload."""
    try:
        provider = get_llm()
    except LLMConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    conv.messages.append(Msg(role="user", content=user_message))

    texts: list[str] = []
    cards: list[dict] = []
    action: dict | None = None
    has_more = False
    safe_error: str | None = None
    search_user_message: str | None = None

    for _ in range(MAX_TOOL_ITERATIONS):
        try:
            turn = provider.complete(
                system=system_prompt(), messages=conv.messages, tools=TOOLS
            )
        except LLMConfigError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except LLMError as exc:
            log.exception("%s provider call failed", provider.name)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        if turn.text or turn.tool_calls:
            conv.messages.append(
                Msg(role="assistant", content=turn.text, tool_calls=turn.tool_calls)
            )
        if turn.text and turn.text.strip():
            texts.append(turn.text.strip())
        if not turn.tool_calls:
            break

        for call in turn.tool_calls:
            result = dispatch_tool(conv, call.name, call.arguments or {})
            if call.name == "search_products":
                cards = result.get("products", []) or cards
                has_more = bool(result.get("has_more"))
                search_user_message = result.get("user_message")
            elif call.name == "get_product" and result.get("product"):
                cards = [result["product"]]
            elif call.name == "mint_scoped_card" and result.get("session_id"):
                action = {"type": "approve_payment", "iframe_url": result["iframe_url"],
                          "session_id": result["session_id"]}
            elif call.name == "complete_checkout" and result.get("success"):
                action = {"type": "receipt", "order_id": result.get("order_id"),
                          "amount": result.get("amount"), "merchant": result.get("merchant")}
            elif call.name == "create_gift_link" and result.get("gift_url"):
                action = {"type": "gift_link", "url": result["gift_url"], "token": result["token"]}
            conv.messages.append(Msg(
                role="tool_result",
                content=json.dumps(result, default=str),
                tool_call_id=call.id,
                tool_name=call.name,
                is_error=bool(result.get("error")),
            ))
            if result.get("error"):
                # Never ask the model to explain a failed payment/catalog tool:
                # its diagnostic details are not buyer-facing information.
                safe_error = _safe_tool_error_message(result)
                break
        if safe_error or search_user_message:
            break

    if safe_error:
        reply = safe_error
    elif search_user_message:
        reply = search_user_message
    elif action and action.get("type") == "gift_link":
        reply = _gift_link_reply(conv.budget)
    elif cards:
        # Product cards are the product presentation. Do not duplicate their
        # structured catalog data into a brittle Markdown list from the model.
        reply = _short_search_reply(cards, conv.budget)
    else:
        reply = _sanitize_buyer_reply(
            "\n\n".join(texts) or "I'm still working on that — could you say that again?"
        )
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


class ProductSelection(BaseModel):
    id: str
    title: str | None = None
    price: str | None = None
    store: str | None = None
    product_url: str | None = None


class ChatRequest(BaseModel):
    conversation_id: str
    message: str
    selection: ProductSelection | None = None


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
    try:
        provider = get_llm()
        llm_info = provider.info()
    except LLMConfigError as exc:  # unknown LLM_PROVIDER — say so instead of 500ing
        llm_info = {
            "provider": (os.getenv("LLM_PROVIDER") or "").strip().lower() or None,
            "model": None,
            "key_present": False,
            "error": str(exc),
        }
    payload: dict[str, Any] = {
        "ok": True,
        "modules": modules,
        "degraded": any(MODULE_STATUS[n]["degraded"] for n in MODULE_NAMES),
        # Which brain is actually answering /chat right now.
        "llm": llm_info,
        "model": llm_info["model"],
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
    # A card click carries its opaque ID separately; `message` stays natural
    # buyer prose and is the only text the UI renders back into the chat.
    if req.selection is not None:
        conv.messages.append(Msg(role="user", content=req.message))
        payload = approve_selection_turn(conv, req.selection)
    else:
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
        ls["stores"], ls["query"], ls["max_price"], ls["cursors"], ls["shown_ids"], conv
    )
    ls["cursors"] = next_cursors
    ls["shown_ids"] |= {p["id"] for p in fresh if p.get("id")}
    has_more = bool(next_cursors) and len(ls["shown_ids"]) < MAX_SHOWN_PRODUCTS
    return {
        "products": fresh,
        "has_more": has_more,
        "message": None if has_more else "That’s everything we found for this search.",
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
