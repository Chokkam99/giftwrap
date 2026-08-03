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


GIVA_MERCHANT_NAME = "GIVA"
GIVA_MERCHANT_URL = "https://giva-jewelry.myshopify.com"
GIVA_PRODUCT_URL = f"{GIVA_MERCHANT_URL}/products/silver-earrings"


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


def test_male_recipient_context_filters_bride_title_and_keeps_neutral_products(monkeypatch):
    """Explicit recipient context must deterministically outrank catalog noise."""
    fake = create_autospec(ucp_client.UCPClient, instance=True)
    fake.search_products_page.return_value = ([
        product("1499.00", '"The Bride Tribe" Jewellery Gift Box for Her', "gid://p/bride"),
        product("1799.00", "Minimal Leather Card Holder", "gid://p/neutral"),
        product("1899.00", "Classic Groom Gift Set", "gid://p/groom"),
    ], None)
    monkeypatch.setattr(main, "_load_ucp", lambda: fake)

    conv = main.Conversation(id="male-context")
    context = main._tool_set_gift_context(conv, {
        "budget": "₹2,000", "recipient": "DC", "gender": "Male",
        "occasion": "wedding", "note": "for his wedding",
    })
    result = main._tool_search_products(conv, {"query": "wedding gift", "store": main.DEFAULT_STORE})

    assert context["gender"] == "male"
    assert context["occasion"] == "wedding"
    assert [card["title"] for card in result["products"]] == [
        "Classic Groom Gift Set", "Minimal Leather Card Holder",
    ]
    assert result["filtered_out_incompatible"] == 1
    assert '"The Bride Tribe" Jewellery Gift Box for Her' not in str(result["products"])


def test_male_context_returns_safe_message_when_only_female_coded_titles_remain(monkeypatch):
    fake = create_autospec(ucp_client.UCPClient, instance=True)
    fake.search_products_page.return_value = ([
        product("1499.00", '"The Bride Tribe" Jewellery Gift Box for Her', "gid://p/bride"),
    ], None)
    monkeypatch.setattr(main, "_load_ucp", lambda: fake)

    conv = main.Conversation(id="male-no-match")
    main._tool_set_gift_context(conv, {
        "budget": "₹2,000", "recipient": "DC", "gender": "male", "occasion": "wedding",
    })
    result = main._tool_search_products(conv, {"query": "wedding gift", "store": main.DEFAULT_STORE})

    assert result["products"] == []
    assert result["user_message"] == (
        "I couldn't find a strong match in this store; try another style or store."
    )


def test_context_persists_explicit_and_inferred_age_range():
    conv = main.Conversation(id="age-context")
    result = main._tool_set_gift_context(conv, {
        "budget": "₹2,000", "recipient": "my dad", "note": "turning 60",
    })
    assert result["age_range"] == conv.age_range == "45–64"


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


def test_search_returns_only_real_ucp_cards(monkeypatch):
    fake = create_autospec(ucp_client.UCPClient, instance=True)
    fake.search_products_page.return_value = ([], None)
    monkeypatch.setattr(main, "_load_ucp", lambda: fake)

    result = main._tool_search_products(
        conversation(), {"query": "ski wax", "store": main.DEFAULT_STORE}
    )

    assert result["products"] == []
    assert "sandbox_checkout_item" not in result


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
        merchant_name=GIVA_MERCHANT_NAME,
        merchant_url=GIVA_MERCHANT_URL,
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
        "merchant_name": GIVA_MERCHANT_NAME,
        # Models commonly hand the product page here; the client must receive
        # the required bare origin instead.
        "merchant_url": GIVA_PRODUCT_URL,
        "amount": "2400.00",
        "description": "Silver earrings",
    })

    kwargs = fake.create_session.call_args.kwargs
    assert kwargs["total_amount"] == "2400.00"
    assert kwargs["currency"] == main.CURRENCY
    assert kwargs["country_code_iso2"] == "IN"
    assert kwargs["product_details"] == [
        {"description": "Silver earrings", "unit_price": "2400.00", "quantity": 1}
    ]
    assert kwargs["merchant_url"] == GIVA_MERCHANT_URL
    assert kwargs["merchant_name"] == GIVA_MERCHANT_NAME

    # Real session, so nothing may be flagged as simulated.
    assert result["session_id"] == "sess_live_1"
    assert "stub" not in result and "degraded" not in result
    assert conv.minted["sess_live_1"]["stub"] is False


def test_known_merchant_country_map_is_exact_and_a_salty_mint_uses_india(monkeypatch):
    assert main.merchant_country_for_url("https://salty.co.in/products/gift") == "IN"
    assert main.merchant_country_for_url("https://giva-jewelry.myshopify.com") == "IN"
    assert main.merchant_country_for_url(f"https://{main.DEMO_STORE}") == main.DEMO_MERCHANT_COUNTRY
    assert main.merchant_country_for_url("https://unknown.example") is None

    fake = create_autospec(prava_client.PravaClient, instance=True)
    fake.create_session.return_value = session_obj("sess_salty")
    monkeypatch.setattr(main, "_load_prava", lambda: fake)
    result = main._tool_mint_scoped_card(conversation(), {
        "merchant_name": "Salty", "merchant_url": "https://salty.co.in/products/gift",
        "amount": "1499.00", "description": "Gift box",
    })

    assert result["session_id"] == "sess_salty"
    kwargs = fake.create_session.call_args.kwargs
    assert kwargs["merchant_url"] == "https://salty.co.in"
    assert kwargs["merchant_name"] == "Salty"
    assert kwargs["country_code_iso2"] == "IN"


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
        "merchant_name": GIVA_MERCHANT_NAME, "merchant_url": GIVA_MERCHANT_URL,
        "amount": "2400.00", "description": "Silver earrings",
    })
    pending = main._tool_get_card_status(conv, {"session_id": "sess_live_1"})

    kwargs = fake.wait_for_result.call_args.kwargs
    assert kwargs["timeout_seconds"] == main.CARD_POLL_TIMEOUT
    assert "timeout" not in kwargs
    assert pending["ready"] is False and pending["status"] == "pending"
    assert "stub" not in pending


def test_terminal_prava_failure_preserves_the_provider_code_without_retrying(monkeypatch):
    fake = create_autospec(prava_client.PravaClient, instance=True)
    fake.create_session.return_value = session_obj()
    failed = payment_result("sess_live_1", with_credential=False)
    failed.status = "failed"
    failed.transactions[0].error = {
        "code": "PROVISION_ERROR", "message": "Request failed with status code 400"
    }
    fake.wait_for_result.return_value = failed
    monkeypatch.setattr(main, "_load_prava", lambda: fake)

    conv = conversation()
    main._tool_mint_scoped_card(conv, {
        "merchant_name": GIVA_MERCHANT_NAME, "merchant_url": GIVA_MERCHANT_URL,
        "amount": "2400.00", "description": "Silver earrings",
    })

    status = main._tool_get_card_status(conv, {"session_id": "sess_live_1"})
    again = main._tool_get_card_status(conv, {"session_id": "sess_live_1"})

    assert status["terminal"] is True
    assert "PROVISION_ERROR" in status["message"]
    assert "Request failed with status code 400" in status["message"]
    assert again == status
    assert fake.wait_for_result.call_count == 1


def test_approved_credential_is_captured_but_never_returned(monkeypatch):
    fake = create_autospec(prava_client.PravaClient, instance=True)
    fake.create_session.return_value = session_obj()
    fake.wait_for_result.return_value = payment_result("sess_live_1", with_credential=True)
    monkeypatch.setattr(main, "_load_prava", lambda: fake)

    conv = conversation()
    main._tool_mint_scoped_card(conv, {
        "merchant_name": GIVA_MERCHANT_NAME, "merchant_url": GIVA_MERCHANT_URL,
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
    url = GIVA_PRODUCT_URL
    conv.prices[url] = 2400.0
    main._tool_mint_scoped_card(conv, {
        "merchant_name": GIVA_MERCHANT_NAME, "merchant_url": GIVA_MERCHANT_URL,
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


def test_checkout_allows_a_scoped_external_product(monkeypatch):
    """An approved Salty credential reaches checkout when the merchant scope matches."""
    prava = create_autospec(prava_client.PravaClient, instance=True)
    conv = conversation()
    conv.minted["old-session"] = {
        "merchant_name": "Salty", "merchant_url": "https://salty.co.in",
        "amount": 1499.0,
        "credential": {"token": "not-used", "dynamic_cvv": "123", "expiry_month": "12", "expiry_year": "27"},
        "txn_ref_id": "txn_salty", "completed": False,
    }
    run_checkout = create_autospec(playwright_checkout.run_shopify_checkout)
    run_checkout.return_value = playwright_checkout.CheckoutResult(
        success=True, order_id="#salty", status="APPROVED", message="Order placed."
    )
    monkeypatch.setattr(main, "_load_checkout", lambda: run_checkout)
    monkeypatch.setattr(main, "_load_prava", lambda: prava)

    outcome = main._tool_complete_checkout(conv, {
        "session_id": "old-session", "product_url": "https://salty.co.in/products/gift",
    })

    assert outcome["success"] is True
    assert run_checkout.call_args.kwargs["product_url"] == "https://salty.co.in/products/gift"
    assert prava.report_status.call_args.kwargs["status"] == "APPROVED"


def test_checkout_success_keeps_receipt_when_sandbox_status_reporting_fails(monkeypatch):
    prava = create_autospec(prava_client.PravaClient, instance=True)
    prava.create_session.return_value = session_obj()
    prava.wait_for_result.return_value = payment_result("sess_live_1", with_credential=True)
    prava.report_status.side_effect = prava_client.PravaError("sandbox dashboard delayed")
    monkeypatch.setattr(main, "_load_prava", lambda: prava)

    run_checkout = create_autospec(playwright_checkout.run_shopify_checkout)
    run_checkout.return_value = playwright_checkout.CheckoutResult(
        success=True, order_id="#1043", status="APPROVED", message="Order placed (#1043)."
    )
    monkeypatch.setattr(main, "_load_checkout", lambda: run_checkout)

    conv = conversation()
    conv.prices[GIVA_PRODUCT_URL] = 2400.0
    main._tool_mint_scoped_card(conv, {
        "merchant_name": GIVA_MERCHANT_NAME, "merchant_url": GIVA_MERCHANT_URL,
        "amount": "2400.00", "description": "Demo item",
    })
    main._tool_get_card_status(conv, {"session_id": "sess_live_1"})
    outcome = main._tool_complete_checkout(
        conv, {"session_id": "sess_live_1", "product_url": GIVA_PRODUCT_URL}
    )

    assert outcome["success"] is True and outcome["order_id"] == "#1043"
    assert "report_status_failed" not in outcome
    assert conv.minted["sess_live_1"]["prava_report_status"]["state"] == "failed"


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
