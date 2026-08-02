"""WP-INT tests: main.py must speak the REAL WP1/WP2/WP3 signatures.

Every collaborator here is an autospec of the actual class/function, so a
signature drift (a renamed kwarg, a newly-required argument) raises TypeError
in the test instead of silently degrading to a stub in production — which is
exactly the failure mode WP-INT was created to kill.

No network, no Prava call, no browser: autospec replaces the transport
entirely. The one live thing in this repo (read-only UCP catalog) is exercised
by scripts/ucp_smoke.py, not from the test suite.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
from checkout import playwright_checkout  # noqa: E402
from prava import client as prava_client  # noqa: E402
from ucp import client as ucp_client  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    main.CONVERSATIONS.clear()
    for name in main.MODULE_NAMES:
        main.MODULE_STATUS[name] = {
            "mode": "unknown", "detail": None, "degraded": False, "last_error": None
        }
    yield
    main.CONVERSATIONS.clear()


def conversation(budget: float = 3000.0) -> main.Conversation:
    conv = main.Conversation(id="wiring")
    conv.budget = budget
    conv.recipient = "my sister"
    return conv


def product(price: str, title: str = "Silver Earrings", pid: str = "gid://p/1"):
    return ucp_client.Product(
        id=pid,
        title=title,
        price=price,
        currency="INR",
        image_url="https://cdn.shopify.com/x.jpg",
        product_url="https://giva-jewelry.myshopify.com/products/silver-earrings",
        merchant="giva-jewelry.myshopify.com",
        variant_id="gid://v/1",
    )


# ------------------------------------------------------------------ UCP wiring


def test_search_products_calls_the_real_ucp_signature(monkeypatch):
    fake = create_autospec(ucp_client.UCPClient, instance=True)
    # WP-BROWSE: search now goes through the cursor-aware page method so
    # main.py can thread pagination.cursor for "show more" — it returns
    # (products, next_cursor) instead of a bare list.
    fake.search_products_page.return_value = ([product("1999.00"), product("2500.00")], "cur_1")
    monkeypatch.setattr(main, "_load_ucp", lambda: fake)

    # Pin a single store explicitly: omitting it now fans the search out across every
    # configured store (WP6 multi-store search) — this test is about the per-store wiring.
    result = main._tool_search_products(
        conversation(), {"query": "earrings", "store": main.DEFAULT_STORE}
    )

    kwargs = fake.search_products_page.call_args.kwargs
    assert kwargs["store"] == main.DEFAULT_STORE
    assert kwargs["query"] == "earrings"
    assert kwargs["max_price"] == 3000.0  # budget flows through as the cap
    assert result["count"] == 2
    # Real data must never be labelled as a stub.
    assert "stub" not in result and "degraded" not in result
    assert all("stub" not in card for card in result["products"])
    assert result["products"][0]["title"] == "Silver Earrings"


def test_search_hard_filters_over_budget_results(monkeypatch):
    """UCP's price_range.max is a soft hint — our filter is the hard one."""
    fake = create_autospec(ucp_client.UCPClient, instance=True)
    fake.search_products_page.return_value = ([
        product("2999.00", "In budget"),
        product("5499.00", "Over budget"),      # the soft-hint leak WP2 observed
        product("3000.00", "Exactly at budget"),
        product("0.00", "Unpriced"),            # unknown price cannot be proven in budget
    ], None)
    monkeypatch.setattr(main, "_load_ucp", lambda: fake)

    # Pin a single store: WP6 multi-store search fans out across all configured stores
    # when store is omitted, which would double-count this mock's fixed return value.
    result = main._tool_search_products(
        conversation(3000.0), {"query": "necklace", "store": main.DEFAULT_STORE}
    )

    titles = [p["title"] for p in result["products"]]
    assert titles == ["In budget", "Exactly at budget"]
    assert result["filtered_out_over_budget"] == 2
    assert result["max_price_enforced"] == 3000.0


def test_explicit_max_price_below_budget_wins(monkeypatch):
    fake = create_autospec(ucp_client.UCPClient, instance=True)
    fake.search_products_page.return_value = (
        [product("1500.00", "Cheap"), product("2900.00", "Pricey")], None
    )
    monkeypatch.setattr(main, "_load_ucp", lambda: fake)

    # Pin a single store: WP6 multi-store search fans out across all configured stores
    # when store is omitted, which would double-count this mock's fixed return value.
    result = main._tool_search_products(
        conversation(3000.0), {"query": "ring", "max_price": 2000, "store": main.DEFAULT_STORE}
    )
    assert fake.search_products_page.call_args.kwargs["max_price"] == 2000.0
    assert [p["title"] for p in result["products"]] == ["Cheap"]


def test_empty_but_working_catalog_does_not_fake_products(monkeypatch):
    """Nothing in budget is real information — it must not become stub data."""
    fake = create_autospec(ucp_client.UCPClient, instance=True)
    fake.search_products_page.return_value = ([], None)
    monkeypatch.setattr(main, "_load_ucp", lambda: fake)

    result = main._tool_search_products(conversation(), {"query": "yacht"})
    assert result["count"] == 0
    assert result["products"] == []
    assert "stub" not in result
    assert "nothing at or below" in result["message"]


def test_ucp_failure_is_loud(monkeypatch):
    fake = create_autospec(ucp_client.UCPClient, instance=True)
    fake.search_products_page.side_effect = ucp_client.UCPError("discovery failed")
    monkeypatch.setattr(main, "_load_ucp", lambda: fake)

    result = main._tool_search_products(conversation(), {"query": "earrings"})
    assert result["stub"] is True and result["degraded"] is True
    assert "discovery failed" in result["degraded_reason"]
    assert main.MODULE_STATUS["ucp"]["degraded"] is True


def test_get_product_calls_the_real_signature(monkeypatch):
    fake = create_autospec(ucp_client.UCPClient, instance=True)
    fake.get_product.return_value = product("2999.00")
    monkeypatch.setattr(main, "_load_ucp", lambda: fake)

    result = main._tool_get_product(conversation(), {"product_id": "gid://p/1"})
    assert fake.get_product.call_args.kwargs == {
        "store": main.DEFAULT_STORE, "product_id": "gid://p/1"
    }
    assert result["product"]["price"] == "2999.00"
    assert "stub" not in result


# ---------------------------------------------------------------- Prava wiring


def session_obj(session_id: str = "sess_live_1"):
    return prava_client.Session(
        session_id=session_id,
        session_token="tok_abc",
        iframe_url=f"https://sandbox.prava.space/checkout/{session_id}",
        order_id="ord_1",
        expires_at="2026-08-01T12:00:00Z",
    )


def payment_result(session_id: str, with_credential: bool):
    item = prava_client.LineItem(
        txn_ref_id="txn_ref_9",
        merchant_name="GIVA",
        merchant_url="https://giva-jewelry.myshopify.com",
        total_amount="2400.00",
        status="awaiting_result" if with_credential else "pending",
        token="4111111111111111" if with_credential else None,
        dynamic_cvv="321" if with_credential else None,
        expiry_month="12" if with_credential else None,
        expiry_year="27" if with_credential else None,
    )
    return prava_client.PaymentResult(
        session_id=session_id,
        order_id="ord_1",
        status="awaiting_result",
        transactions=[prava_client.Transaction(
            txn_id="txn_1", status="awaiting_result", line_items=[item]
        )],
    )


def test_mint_calls_create_session_with_every_required_argument(monkeypatch):
    """create_session REQUIRES country_code_iso2 and product_details.

    Omitting them is the drift that used to dump the whole payment leg onto the
    stub without a word.
    """
    fake = create_autospec(prava_client.PravaClient, instance=True)
    fake.create_session.return_value = session_obj()
    monkeypatch.setattr(main, "_load_prava", lambda: fake)

    conv = conversation()
    result = main._tool_mint_scoped_card(conv, {
        "merchant_name": "GIVA",
        "merchant_url": "https://giva-jewelry.myshopify.com",
        "amount": "2400.00",
        "description": "Silver earrings",
    })

    kwargs = fake.create_session.call_args.kwargs
    assert kwargs["total_amount"] == "2400.00"
    assert kwargs["currency"] == main.CURRENCY
    assert kwargs["country_code_iso2"] == main.MERCHANT_COUNTRY
    assert kwargs["product_details"] == [
        {"description": "Silver earrings", "unit_price": "2400.00", "quantity": 1}
    ]
    assert kwargs["merchant_url"] == "https://giva-jewelry.myshopify.com"

    # Real session, so nothing may be flagged as simulated.
    assert result["session_id"] == "sess_live_1"
    assert "stub" not in result and "degraded" not in result
    assert conv.minted["sess_live_1"]["stub"] is False


def test_mint_budget_guard_runs_before_prava_is_ever_called(monkeypatch):
    fake = create_autospec(prava_client.PravaClient, instance=True)
    monkeypatch.setattr(main, "_load_prava", lambda: fake)

    result = main._tool_mint_scoped_card(conversation(3000.0), {
        "merchant_name": "GIVA",
        "merchant_url": "https://giva-jewelry.myshopify.com",
        "amount": "5200.00",
        "description": "Necklace",
    })
    assert result["error"] == "budget_exceeded"
    fake.create_session.assert_not_called()  # no session burned on an over-budget ask


def test_card_status_uses_timeout_seconds_not_timeout(monkeypatch):
    """wait_for_result takes timeout_seconds; `timeout=` was silently dropped,
    leaving each poll blocked for the full 120s default."""
    fake = create_autospec(prava_client.PravaClient, instance=True)
    fake.create_session.return_value = session_obj()
    fake.wait_for_result.return_value = payment_result("sess_live_1", with_credential=False)
    monkeypatch.setattr(main, "_load_prava", lambda: fake)

    conv = conversation()
    main._tool_mint_scoped_card(conv, {
        "merchant_name": "GIVA", "merchant_url": "https://giva-jewelry.myshopify.com",
        "amount": "2400.00", "description": "Silver earrings",
    })
    pending = main._tool_get_card_status(conv, {"session_id": "sess_live_1"})

    kwargs = fake.wait_for_result.call_args.kwargs
    assert kwargs["timeout_seconds"] == main.CARD_POLL_TIMEOUT
    assert "timeout" not in kwargs
    assert pending["ready"] is False and pending["status"] == "pending"
    assert "stub" not in pending


def test_approved_credential_is_captured_but_never_returned(monkeypatch):
    fake = create_autospec(prava_client.PravaClient, instance=True)
    fake.create_session.return_value = session_obj()
    fake.wait_for_result.return_value = payment_result("sess_live_1", with_credential=True)
    monkeypatch.setattr(main, "_load_prava", lambda: fake)

    conv = conversation()
    main._tool_mint_scoped_card(conv, {
        "merchant_name": "GIVA", "merchant_url": "https://giva-jewelry.myshopify.com",
        "amount": "2400.00", "description": "Silver earrings",
    })
    status = main._tool_get_card_status(conv, {"session_id": "sess_live_1"})

    assert status == {"status": "approved", "ready": True, "txn_ref_id": "txn_ref_9"}
    # Held server-side only.
    assert conv.minted["sess_live_1"]["credential"]["token"] == "4111111111111111"


# ------------------------------------------------------------- checkout wiring


def test_complete_checkout_calls_the_real_checkout_and_reports_status(monkeypatch):
    prava = create_autospec(prava_client.PravaClient, instance=True)
    prava.create_session.return_value = session_obj()
    prava.wait_for_result.return_value = payment_result("sess_live_1", with_credential=True)
    monkeypatch.setattr(main, "_load_prava", lambda: prava)

    run_checkout = create_autospec(playwright_checkout.run_shopify_checkout)
    run_checkout.return_value = playwright_checkout.CheckoutResult(
        success=True, order_id="#1042", status="APPROVED", message="Order placed (#1042)."
    )
    monkeypatch.setattr(main, "_load_checkout", lambda: run_checkout)

    conv = conversation()
    url = "https://giva-jewelry.myshopify.com/products/silver-earrings"
    conv.prices[url] = 2400.0
    main._tool_mint_scoped_card(conv, {
        "merchant_name": "GIVA", "merchant_url": "https://giva-jewelry.myshopify.com",
        "amount": "2400.00", "description": "Silver earrings",
    })
    main._tool_get_card_status(conv, {"session_id": "sess_live_1"})
    outcome = main._tool_complete_checkout(
        conv, {"session_id": "sess_live_1", "product_url": url}
    )

    checkout_kwargs = run_checkout.call_args.kwargs
    assert checkout_kwargs["token"] == "4111111111111111"
    assert checkout_kwargs["dynamic_cvv"] == "321"
    assert checkout_kwargs["expiry_month"] == "12"
    assert checkout_kwargs["expiry_year"] == "27"
    assert checkout_kwargs["product_url"] == url

    # report_status(session_id, txn_ref_id, status) — "txn_status" is not a kwarg.
    report_kwargs = prava.report_status.call_args.kwargs
    assert report_kwargs == {
        "session_id": "sess_live_1", "txn_ref_id": "txn_ref_9", "status": "APPROVED"
    }

    assert outcome["success"] is True and outcome["order_id"] == "#1042"
    assert "stub" not in outcome
    assert conv.minted["sess_live_1"]["credential"] is None  # one-time card burned


def test_checkout_refusal_is_reported_without_marking_the_module_degraded(monkeypatch):
    """assert_purchase_allowed refusing a real merchant is by design, not a fault."""
    prava = create_autospec(prava_client.PravaClient, instance=True)
    prava.create_session.return_value = session_obj()
    prava.wait_for_result.return_value = payment_result("sess_live_1", with_credential=True)
    monkeypatch.setattr(main, "_load_prava", lambda: prava)

    run_checkout = create_autospec(playwright_checkout.run_shopify_checkout)
    run_checkout.side_effect = playwright_checkout.CheckoutError(
        "Refusing to run a checkout against real merchant host 'giva.co'."
    )
    monkeypatch.setattr(main, "_load_checkout", lambda: run_checkout)

    conv = conversation()
    url = "https://giva-jewelry.myshopify.com/products/silver-earrings"
    conv.prices[url] = 2400.0
    main._tool_mint_scoped_card(conv, {
        "merchant_name": "GIVA", "merchant_url": "https://giva-jewelry.myshopify.com",
        "amount": "2400.00", "description": "Silver earrings",
    })
    main._tool_get_card_status(conv, {"session_id": "sess_live_1"})
    outcome = main._tool_complete_checkout(
        conv, {"session_id": "sess_live_1", "product_url": url}
    )

    assert outcome["success"] is False and outcome["status"] == "refused"
    assert main.MODULE_STATUS["checkout"]["degraded"] is False
    assert prava.report_status.call_args.kwargs["status"] == "DECLINED"


def test_real_modules_import_and_construct_cleanly():
    """Import-level proof for /health: UCP and checkout need no credentials."""
    assert main._load_ucp() is not None
    assert main._load_checkout() is not None
    modules = main.probe_modules()
    assert modules["ucp"] == "real"
    assert modules["checkout"] == "real"
    # Prava depends on PRAVA_SECRET_KEY; whichever way it resolves must be honest.
    assert modules["prava"] in ("real", "stub")
    if modules["prava"] == "stub":
        assert "PRAVA_SECRET_KEY" in (main.MODULE_STATUS["prava"]["detail"] or "")
