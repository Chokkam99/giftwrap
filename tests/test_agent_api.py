"""WP4 tests: /chat tool loop, budget guard, credential containment.

The Anthropic client is always mocked (no API key needed), and the lazy WP1-WP3
loaders are forced to None so the run is deterministic against the stubs even
while those modules are landing in parallel. No real Prava or checkout call is
ever made from here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402


# --------------------------------------------------------------- fake Anthropic


class Text:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class ToolUse:
    type = "tool_use"

    def __init__(self, name: str, tool_input: dict, block_id: str | None = None) -> None:
        self.id = block_id or f"toolu_{name}"
        self.name = name
        self.input = tool_input


class Response:
    def __init__(self, content: list) -> None:
        self.content = content
        self.stop_reason = "tool_use" if any(
            getattr(b, "type", None) == "tool_use" for b in content
        ) else "end_turn"


class FakeMessages:
    def __init__(self, script: list[Response]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.script:
            return self.script.pop(0)
        return Response([Text("All set.")])


class FakeAnthropic:
    def __init__(self, script: list[Response]) -> None:
        self.messages = FakeMessages(script)


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    """Fresh state + stubs only (never the real WP1/WP2/WP3 modules)."""
    main.CONVERSATIONS.clear()
    monkeypatch.setattr(main, "_load_ucp", lambda: None)
    monkeypatch.setattr(main, "_load_prava", lambda: None)
    monkeypatch.setattr(main, "_load_checkout", lambda: None)
    yield
    main.CONVERSATIONS.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


def script(monkeypatch, *responses: Response) -> FakeAnthropic:
    fake = FakeAnthropic(list(responses))
    monkeypatch.setattr(main, "get_anthropic_client", lambda: fake)
    return fake


def tool_results(fake: FakeAnthropic) -> dict[str, dict]:
    """Map tool_use_id -> parsed tool_result payload handed back to the model."""
    out: dict[str, dict] = {}
    for call in fake.messages.calls:
        for message in call["messages"]:
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        out[block["tool_use_id"]] = {
                            "payload": json.loads(block["content"]),
                            "is_error": block.get("is_error"),
                        }
    return out


def raw_tool_text(fake: FakeAnthropic) -> str:
    return json.dumps(
        [c["messages"] for c in fake.messages.calls], default=str
    )


CONTEXT = ToolUse("set_gift_context", {"budget": "₹3,000", "recipient": "my sister"}, "t_ctx")


# ------------------------------------------------------------------------ tests


def test_health(client):
    assert client.get("/health").json() == {"ok": True}


def test_root_serves_something(client):
    response = client.get("/")
    assert response.status_code == 200
    if not main.INDEX_HTML.is_file():  # WP5 may land index.html in parallel
        assert "Agentic Gifting backend is running" in response.text


def test_chat_roundtrip_plain_reply(client, monkeypatch):
    script(monkeypatch, Response([Text("Who is the gift for?")]))
    body = client.post("/chat", json={"conversation_id": "c1", "message": "hi"}).json()
    assert body["reply"] == "Who is the gift for?"
    assert body["cards"] is None and body["action"] is None


def test_search_populates_cards(client, monkeypatch):
    fake = script(
        monkeypatch,
        Response([CONTEXT]),
        Response([ToolUse("search_products", {"query": "silver earrings"}, "t_search")]),
        Response([Text("Here are three options.")]),
    )
    body = client.post(
        "/chat", json={"conversation_id": "c2", "message": "gift for my sister, ₹3000"}
    ).json()
    assert body["cards"] and len(body["cards"]) == 3
    assert all(card["stub"] for card in body["cards"])
    assert all(float(card["price"]) <= 3000 for card in body["cards"])
    assert tool_results(fake)["t_ctx"]["payload"]["budget"] == 3000.0


def test_tools_blocked_until_context_is_set(client, monkeypatch):
    fake = script(
        monkeypatch,
        Response([ToolUse("search_products", {"query": "earrings"}, "t_early")]),
        Response([Text("What is your budget?")]),
    )
    client.post("/chat", json={"conversation_id": "c3", "message": "find a gift"})
    result = tool_results(fake)["t_early"]
    assert result["is_error"] is True
    assert "set_gift_context" in result["payload"]["error"]


def test_budget_guard_rejects_over_budget_mint(client, monkeypatch):
    fake = script(
        monkeypatch,
        Response([CONTEXT]),
        Response([ToolUse(
            "mint_scoped_card",
            {"merchant_name": "GIVA", "merchant_url": "https://giva-jewelry.myshopify.com",
             "amount": "5200.00", "description": "Necklace"},
            "t_mint",
        )]),
        Response([Text("That one is over budget.")]),
    )
    body = client.post(
        "/chat", json={"conversation_id": "c4", "message": "buy the ₹5200 necklace"}
    ).json()

    result = tool_results(fake)["t_mint"]
    assert result["is_error"] is True
    assert result["payload"]["error"] == "budget_exceeded"
    assert result["payload"]["budget"] == 3000.0
    assert body["action"] is None
    assert main.CONVERSATIONS["c4"].minted == {}  # nothing was ever minted


def test_mint_returns_approval_action_and_never_leaks_credentials(client, monkeypatch):
    mint = ToolUse(
        "mint_scoped_card",
        {"merchant_name": "GIVA", "merchant_url": "https://giva-jewelry.myshopify.com",
         "amount": "2400.00", "description": "Silver earrings"},
        "t_mint",
    )
    fake = script(
        monkeypatch,
        Response([CONTEXT]),
        Response([mint]),
        Response([Text("Approve in the Prava window, then tell me.")]),
    )
    body = client.post(
        "/chat", json={"conversation_id": "c5", "message": "yes, buy option 1"}
    ).json()
    assert body["action"]["type"] == "approve_payment"
    session_id = body["action"]["session_id"]
    assert body["action"]["iframe_url"]

    conv = main.CONVERSATIONS["c5"]
    assert conv.minted[session_id]["amount"] == 2400.0

    # Poll twice: stub is pending on the first poll, approved on the second.
    poll = lambda tag: Response([ToolUse("get_card_status", {"session_id": session_id}, tag)])
    fake2 = script(monkeypatch, poll("t_p1"), poll("t_p2"), Response([Text("Approved.")]))
    client.post("/chat", json={"conversation_id": "c5", "message": "I approved it"})

    results = tool_results(fake2)
    assert results["t_p1"]["payload"] == {
        "status": "pending", "ready": False,
        "message": results["t_p1"]["payload"]["message"], "stub": True,
    }
    assert results["t_p2"]["payload"]["ready"] is True
    assert set(results["t_p2"]["payload"]) <= {"status", "ready", "txn_ref_id", "stub"}

    # Credential is held server-side only, and never shown to the model.
    assert conv.minted[session_id]["credential"]["token"]
    blob = raw_tool_text(fake) + raw_tool_text(fake2)
    for secret in ("dynamic_cvv", "4111111111111111", "expiry_month", '"token"'):
        assert secret not in blob


def _mint_and_approve(client, monkeypatch, conversation_id: str, amount: str = "2400.00") -> str:
    script(
        monkeypatch,
        Response([CONTEXT]),
        Response([ToolUse(
            "mint_scoped_card",
            {"merchant_name": "GIVA", "merchant_url": "https://giva-jewelry.myshopify.com",
             "amount": amount, "description": "Silver earrings"},
            "t_mint",
        )]),
        Response([Text("Approve please.")]),
    )
    body = client.post(
        "/chat", json={"conversation_id": conversation_id, "message": "buy it"}
    ).json()
    session_id = body["action"]["session_id"]

    poll = lambda tag: Response([ToolUse("get_card_status", {"session_id": session_id}, tag)])
    script(monkeypatch, poll("a"), poll("b"), Response([Text("Approved.")]))
    client.post("/chat", json={"conversation_id": conversation_id, "message": "done"})
    return session_id


def test_full_stub_flow_reaches_receipt(client, monkeypatch):
    session_id = _mint_and_approve(client, monkeypatch, "c6")
    fake = script(
        monkeypatch,
        Response([ToolUse(
            "complete_checkout",
            {"session_id": session_id,
             "product_url": "https://giva-jewelry.myshopify.com/products/silver-earrings-1"},
            "t_done",
        )]),
        Response([Text("Ordered! Here is the receipt.")]),
    )
    body = client.post("/chat", json={"conversation_id": "c6", "message": "go ahead"}).json()

    assert body["action"]["type"] == "receipt"
    assert body["action"]["order_id"].startswith("STUB-")
    assert body["action"]["amount"] == "2400.00"
    assert body["action"]["merchant"] == "GIVA"
    assert tool_results(fake)["t_done"]["payload"]["status"] == "paid"

    record = main.CONVERSATIONS["c6"].minted[session_id]
    assert record["completed"] is True
    assert record["credential"] is None  # one-time credential burned after use


def test_checkout_refuses_a_different_merchant(client, monkeypatch):
    session_id = _mint_and_approve(client, monkeypatch, "c7")
    fake = script(
        monkeypatch,
        Response([ToolUse(
            "complete_checkout",
            {"session_id": session_id, "product_url": "https://not-the-store.example/products/x"},
            "t_wrong",
        )]),
        Response([Text("I cannot use that card there.")]),
    )
    body = client.post("/chat", json={"conversation_id": "c7", "message": "buy elsewhere"}).json()
    result = tool_results(fake)["t_wrong"]
    assert result["is_error"] is True
    assert result["payload"]["error"] == "merchant_scope"
    assert body["action"] is None
    assert main.CONVERSATIONS["c7"].minted[session_id]["completed"] is False


def test_checkout_requires_approval_first(client, monkeypatch):
    script(
        monkeypatch,
        Response([CONTEXT]),
        Response([ToolUse(
            "mint_scoped_card",
            {"merchant_name": "GIVA", "merchant_url": "https://giva-jewelry.myshopify.com",
             "amount": "1000.00", "description": "Ring"},
            "t_mint",
        )]),
        Response([Text("Approve please.")]),
    )
    session_id = client.post(
        "/chat", json={"conversation_id": "c8", "message": "buy"}
    ).json()["action"]["session_id"]

    fake = script(
        monkeypatch,
        Response([ToolUse(
            "complete_checkout",
            {"session_id": session_id,
             "product_url": "https://giva-jewelry.myshopify.com/products/ring-1"},
            "t_early_checkout",
        )]),
        Response([Text("Please approve first.")]),
    )
    client.post("/chat", json={"conversation_id": "c8", "message": "just pay"})
    result = tool_results(fake)["t_early_checkout"]
    assert result["is_error"] is True
    assert "not approved yet" in result["payload"]["error"]


def test_missing_api_key_is_a_clean_503(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(main, "_anthropic_client", None)
    response = client.post("/chat", json={"conversation_id": "c10", "message": "hi"})
    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]


def test_tool_loop_is_bounded(client, monkeypatch):
    """A model that never stops calling tools must not spin forever."""
    responses = [Response([CONTEXT])] + [
        Response([ToolUse("search_products", {"query": "gift"}, f"t{i}")])
        for i in range(30)
    ]
    fake = script(monkeypatch, *responses)
    client.post("/chat", json={"conversation_id": "c9", "message": "loop"})
    assert len(fake.messages.calls) == main.MAX_TOOL_ITERATIONS
