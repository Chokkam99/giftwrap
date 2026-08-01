"""Agentic Gifting — FastAPI backend + Claude tool-use loop (WP4).

Runs standalone today: `prava/`, `ucp/` and `checkout/` are imported lazily
inside the tool handlers and fall back to clearly-marked stubs when they are
not importable yet, so integration is automatic once WP1-WP3 land.

Safety invariants enforced here (not just in the prompt):
  * the one-time card `token` / `dynamic_cvv` never reach the model or the logs
    - they live in server-side conversation state, addressed only by session_id;
  * `mint_scoped_card` refuses to mint above the buyer's stated budget, and
    `complete_checkout` re-checks price and merchant scope before paying.
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import os
import re
import time
import uuid
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
CURRENCY = os.getenv("GIFT_CURRENCY", "INR")
CARD_POLL_TIMEOUT = 10  # seconds — short so the model can poll conversationally
BUYER_ID = os.getenv("PRAVA_USER_ID", "agentic-gifting-buyer")
BUYER_EMAIL = os.getenv("PRAVA_USER_EMAIL", "buyer@example.com")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_HTML = STATIC_DIR / "index.html"

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
2. Search ONE store with `search_products` (default store: {DEFAULT_STORE}). Present 2-3 options
   that are within budget, with prices, and ask the buyer to pick one.
3. NEVER call `mint_scoped_card` until the buyer has explicitly approved a specific product.
   Mint for the exact price of that product, scoped to that merchant.
4. After minting, tell the buyer to approve the payment in the Prava window that just opened.
5. Poll `get_card_status`. While it is pending, tell the buyer to finish the approval. Once it is
   ready, call `complete_checkout` for the approved product and present the receipt (order id,
   amount, merchant) in plain language.

Rules: be warm and brief. Quote prices with the currency. Never invent products, order ids, or
prices — only report what the tools returned. If a tool refuses (e.g. the budget guard), explain
the refusal honestly to the buyer instead of retrying with different numbers.
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
          "Search a merchant's live catalog for gift options.",
          ["query"],
          store=_field(f"Store domain. Default {DEFAULT_STORE}."),
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


CONVERSATIONS: dict[str, Conversation] = {}


def get_conversation(conversation_id: str) -> Conversation:
    conv = CONVERSATIONS.get(conversation_id)
    if conv is None:
        conv = Conversation(id=conversation_id)
        CONVERSATIONS[conversation_id] = conv
    return conv


# ------------------------------------------------------------------ lazy WP modules


_MODULES: dict[str, Any] = {}


def _import(name: str) -> Any | None:
    if name not in _MODULES:
        try:
            _MODULES[name] = importlib.import_module(name)
        except Exception:  # not built yet, or broken mid-edit — use the stub
            _MODULES[name] = None
    return _MODULES[name]


def _load_ucp() -> Any | None:
    """WP2's ucp.client.UCPClient, or None (→ stub)."""
    if "ucp_instance" not in _MODULES:
        mod = _import("ucp.client")
        try:
            _MODULES["ucp_instance"] = mod.UCPClient() if mod else None
        except Exception:
            _MODULES["ucp_instance"] = None
    return _MODULES["ucp_instance"]


def _load_prava() -> Any | None:
    """WP1's prava.client.PravaClient, or None (→ stub). Never used in tests."""
    if "prava_instance" not in _MODULES:
        mod = _import("prava.client")
        try:
            _MODULES["prava_instance"] = mod.PravaClient() if mod else None
        except Exception:
            _MODULES["prava_instance"] = None
    return _MODULES["prava_instance"]


def _load_checkout() -> Any | None:
    """WP3's checkout.playwright_checkout.run_shopify_checkout, or None (→ stub)."""
    mod = _import("checkout.playwright_checkout")
    return getattr(mod, "run_shopify_checkout", None) if mod else None


def _call_flexible(fn: Any, **kwargs: Any) -> Any:
    """Call `fn` with only the kwargs its signature accepts.

    This is the single adaptation point for WP1/WP2 signature drift: extra
    keywords are dropped rather than raising TypeError.
    """
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return fn(**kwargs)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return fn(**kwargs)
    allowed = {
        p.name for p in params
        if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    }
    return fn(**{k: v for k, v in kwargs.items() if k in allowed})


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


def _tool_search_products(conv: Conversation, args: dict) -> dict:
    store = args.get("store") or DEFAULT_STORE
    query = args.get("query") or ""
    max_price = _parse_amount(args.get("max_price"))
    if conv.budget is not None:
        max_price = min(max_price, conv.budget) if max_price else conv.budget

    client = _load_ucp()
    products: list[dict] = []
    if client is not None:
        try:
            raw = _call_flexible(
                client.search_products, store=store, query=query,
                max_price=max_price, limit=6,
            )
            products = [_fields(p, PRODUCT_FIELDS) for p in (raw or [])]
        except Exception as exc:
            log.warning("ucp.search_products failed (%s); using stub", exc)
    if not products:
        products = _stub_products(store, query, max_price)

    for p in products:
        price = _parse_amount(p.get("price"))
        if price is not None and p.get("product_url"):
            conv.prices[p["product_url"]] = price
    return {"store": store, "count": len(products), "products": products}


def _tool_get_product(conv: Conversation, args: dict) -> dict:
    store = args.get("store") or DEFAULT_STORE
    product_id = args.get("product_id") or ""
    client = _load_ucp()
    product: dict | None = None
    if client is not None:
        try:
            raw = _call_flexible(client.get_product, store=store, product_id=product_id)
            product = _fields(raw, PRODUCT_FIELDS) if raw else None
        except Exception as exc:
            log.warning("ucp.get_product failed (%s); using stub", exc)
    if not product:
        product = _stub_products(store, product_id or "gift", conv.budget, limit=1)[0]
        product["id"] = product_id or product["id"]
    price = _parse_amount(product.get("price"))
    if price is not None and product.get("product_url"):
        conv.prices[product["product_url"]] = price
    return {"product": product}


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
    client = _load_prava()
    if client is not None:
        try:
            raw = _call_flexible(
                client.create_session,
                user_id=BUYER_ID, user_email=BUYER_EMAIL,
                total_amount=amount_str, currency=CURRENCY,
                merchant_name=merchant_name, merchant_url=merchant_url,
                description=description,
            )
            session = _fields(raw, ("session_id", "iframe_url", "expires_at"))
        except Exception as exc:
            log.warning("prava.create_session failed (%s); using stub", exc)
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
    client = None if record["stub"] else _load_prava()
    if client is not None:
        try:
            result = _call_flexible(
                client.wait_for_result, session_id=session_id, timeout=CARD_POLL_TIMEOUT
            )
            if result is not None:
                raw = result.first_credential() if hasattr(result, "first_credential") else result
                if raw:
                    credential = _fields(
                        raw,
                        ("token", "dynamic_cvv", "expiry_month", "expiry_year", "txn_ref_id"),
                    )
        except Exception as exc:  # timeout or still pending
            log.info("prava.wait_for_result pending for session=%s (%s)", session_id, type(exc).__name__)
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
    if run_checkout is not None:
        try:
            raw = _call_flexible(
                run_checkout,
                token=credential["token"], dynamic_cvv=credential["dynamic_cvv"],
                expiry_month=credential["expiry_month"], expiry_year=credential["expiry_year"],
                product_url=product_url,
            )
            outcome = _fields(raw, ("success", "order_id", "status", "message"))
        except Exception as exc:
            log.warning("checkout failed for session=%s: %s", session_id, exc)
            outcome = {"success": False, "order_id": None, "status": "failed",
                       "message": f"Checkout error: {exc}"}
    else:
        outcome = {"success": True, "order_id": f"STUB-{uuid.uuid4().hex[:6].upper()}",
                   "status": "paid", "message": "Stubbed checkout (WP3 not present).", "stub": True}

    # Always report back to Prava, or the txn sticks in awaiting_result.
    client = _load_prava()
    if client is not None and record["txn_ref_id"]:
        try:
            _call_flexible(
                client.report_status, session_id=session_id,
                txn_ref_id=record["txn_ref_id"],
                txn_status="APPROVED" if outcome.get("success") else "DECLINED",
                status="APPROVED" if outcome.get("success") else "DECLINED",
            )
        except Exception as exc:
            log.error("report_status failed for session=%s: %s", session_id, exc)

    record["completed"] = True
    record["credential"] = None  # burn the one-time credential
    log.info("checkout session=%s success=%s order=%s",
             session_id, outcome.get("success"), outcome.get("order_id"))
    outcome["amount"] = f"{record['amount']:.2f}"
    outcome["currency"] = CURRENCY
    outcome["merchant"] = record["merchant_name"]
    return outcome


TOOL_HANDLERS = {
    "set_gift_context": _tool_set_gift_context,
    "search_products": _tool_search_products,
    "get_product": _tool_get_product,
    "mint_scoped_card": _tool_mint_scoped_card,
    "get_card_status": _tool_get_card_status,
    "complete_checkout": _tool_complete_checkout,
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
            elif block.name == "get_product" and result.get("product"):
                cards = [result["product"]]
            elif block.name == "mint_scoped_card" and result.get("session_id"):
                action = {"type": "approve_payment", "iframe_url": result["iframe_url"],
                          "session_id": result["session_id"]}
            elif block.name == "complete_checkout" and result.get("success"):
                action = {"type": "receipt", "order_id": result.get("order_id"),
                          "amount": result.get("amount"), "merchant": result.get("merchant")}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
                "is_error": bool(result.get("error")),
            })
        conv.messages.append({"role": "user", "content": tool_results})

    reply = "\n\n".join(texts) or "I'm still working on that — could you say that again?"
    return {"reply": reply, "cards": cards or None, "action": action}


# ------------------------------------------------------------------------- app


class ChatRequest(BaseModel):
    conversation_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str
    cards: list[dict] | None = None
    action: dict | None = None


app = FastAPI(title="Agentic Gifting")

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


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
