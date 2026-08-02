"""Unit tests for the pure logic in checkout.playwright_checkout.

No browser is launched here -- these cover order-id extraction, decline
detection, outcome classification, env handling, card/expiry normalisation,
the dev-store guard, and result mapping.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from checkout.playwright_checkout import (  # noqa: E402
    DEFAULT_SHIPPING,
    STATUS_APPROVED,
    STATUS_DECLINED,
    STATUS_FAILED,
    CheckoutError,
    CheckoutResult,
    assert_purchase_allowed,
    bogus_gateway_pan,
    build_result,
    classify_outcome,
    detect_decline,
    effective_card_number,
    env_bool,
    extract_order_id,
    format_expiry,
    host_of,
    is_confirmation_url,
    is_dev_store_host,
    locale_for,
    looks_confirmed,
    mask_pan,
    resolve_address_country,
    resolve_headless,
    resolve_product_url,
    shipping_address,
    shipping_profile,
)

DEV_PRODUCT_URL = "https://gifting-demo.myshopify.com/products/silver-pendant"


# --------------------------------------------------------------------------
# order id extraction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Thank you, Aarav!\nOrder #1001\nYour order is confirmed", "1001"),
        ("ORDER #GIFT-2049 confirmed", "GIFT-2049"),
        ("Order number: 1234", "1234"),
        ("Confirmation #  A9981", "A9981"),
        ("Your order 1002 has been placed", "1002"),
        ("order #1001", "1001"),
    ],
)
def test_extract_order_id_from_text(text, expected):
    assert extract_order_id(text, "") == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Order summary",
        "Order updates will be emailed to you",
        "Order details",
        "Place your order again",
        "Nothing relevant here at all",
    ],
)
def test_extract_order_id_ignores_ordinary_copy(text):
    assert extract_order_id(text, "") is None


def test_extract_order_id_prefers_hash_form_over_prose():
    text = "Order summary\nSubtotal 2,499\nOrder #1042 confirmed"
    assert extract_order_id(text, "") == "1042"


def test_extract_order_id_falls_back_to_url():
    url = "https://gifting-demo.myshopify.com/orders/abc123def456?key=xyz"
    assert extract_order_id("Thank you!", url) == "abc123def456"


def test_extract_order_id_text_wins_over_url():
    url = "https://gifting-demo.myshopify.com/orders/abc123def456"
    assert extract_order_id("Order #1001", url) == "1001"


# --------------------------------------------------------------------------
# decline detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,marker",
    [
        ("Your card was declined.", "declined"),
        ("PAYMENT NOT PROCESSED", "payment not processed"),
        ("We were unable to process your payment", "unable to process your payment"),
        ("Insufficient funds on this card", "insufficient funds"),
        ("The transaction was not authorized", "transaction was not authorized"),
        ("This purchase exceeds the limit on your card", "exceeds the limit"),
    ],
)
def test_detect_decline_finds_marker(text, marker):
    assert detect_decline(text) == marker


@pytest.mark.parametrize(
    "text",
    ["", "Thank you for your order!", "Order #1001 confirmed", "Shipping to Bengaluru"],
)
def test_detect_decline_negative(text):
    assert detect_decline(text) is None


# --------------------------------------------------------------------------
# confirmation detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://s.myshopify.com/checkouts/c/abc/thank_you",
        "https://s.myshopify.com/checkouts/cn/abc/thank-you",
        "https://s.myshopify.com/checkout_success?order=1",
        "https://s.myshopify.com/orders/abc123",
    ],
)
def test_is_confirmation_url_positive(url):
    assert is_confirmation_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://s.myshopify.com/checkouts/c/abc",
        "https://s.myshopify.com/cart",
        "https://s.myshopify.com/products/silver-pendant",
    ],
)
def test_is_confirmation_url_negative(url):
    assert is_confirmation_url(url) is False


def test_looks_confirmed_without_confirmation_url_needs_thank_you_and_order():
    assert looks_confirmed("https://s.myshopify.com/checkouts/c/abc", "Order #1001") is False
    assert (
        looks_confirmed("https://s.myshopify.com/checkouts/c/abc", "Thank you! Order #1001")
        is True
    )


# --------------------------------------------------------------------------
# outcome classification
# --------------------------------------------------------------------------


def test_classify_outcome_approved():
    result = classify_outcome(
        "https://gifting-demo.myshopify.com/checkouts/c/tok/thank_you",
        "Thank you, Aarav!\nOrder #1001\nYour order is confirmed.",
    )
    assert result.status == STATUS_APPROVED
    assert result.success is True
    assert result.order_id == "1001"
    assert result.details["final_url"].endswith("/thank_you")


def test_classify_outcome_approved_without_order_id_still_approves():
    result = classify_outcome(
        "https://gifting-demo.myshopify.com/checkouts/c/tok/thank_you",
        "Thank you! Your order is confirmed.",
    )
    assert result.status == STATUS_APPROVED
    assert result.success is True
    assert result.order_id is None


def test_classify_outcome_declined():
    result = classify_outcome(
        "https://gifting-demo.myshopify.com/checkouts/c/tok",
        "Your card was declined. Please try a different payment method.",
    )
    assert result.status == STATUS_DECLINED
    assert result.success is False
    assert result.order_id is None
    assert result.details["decline_marker"] == "declined"


def test_classify_outcome_failed_when_no_signal():
    result = classify_outcome(
        "https://gifting-demo.myshopify.com/checkouts/c/tok",
        "Shipping method\nStandard shipping",
    )
    assert result.status == STATUS_FAILED
    assert result.success is False
    assert result.order_id is None


def test_classify_outcome_confirmation_beats_stray_decline_wording():
    result = classify_outcome(
        "https://gifting-demo.myshopify.com/checkouts/c/tok/thank_you",
        "Order #1001. If a card is declined we email you.",
    )
    assert result.status == STATUS_APPROVED
    assert result.order_id == "1001"


# --------------------------------------------------------------------------
# result mapping
# --------------------------------------------------------------------------


def test_build_result_approved_shape():
    result = build_result(STATUS_APPROVED, "ok", order_id="1001", details={"a": 1})
    assert isinstance(result, CheckoutResult)
    assert result.to_dict() == {
        "success": True,
        "order_id": "1001",
        "status": "APPROVED",
        "message": "ok",
        "details": {"a": 1},
    }


def test_build_result_drops_order_id_for_non_approved():
    assert build_result(STATUS_DECLINED, "nope", order_id="1001").order_id is None
    assert build_result(STATUS_FAILED, "nope", order_id="1001").order_id is None


def test_build_result_rejects_unknown_status():
    with pytest.raises(ValueError):
        build_result("PENDING", "hmm")


def test_checkout_result_defaults_details_to_empty_dict():
    assert CheckoutResult(False, None, STATUS_FAILED, "x").details == {}


# --------------------------------------------------------------------------
# env handling
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("1", True), ("true", True), ("YES", True), ("on", True),
     ("0", False), ("false", False), ("no", False), ("off", False)],
)
def test_env_bool_parsing(raw, expected):
    assert env_bool("X", not expected, env={"X": raw}) is expected


def test_env_bool_missing_and_garbage_use_default():
    assert env_bool("X", True, env={}) is True
    assert env_bool("X", False, env={}) is False
    assert env_bool("X", True, env={"X": "maybe"}) is True


def test_resolve_headless_precedence():
    assert resolve_headless(False, env={"CHECKOUT_HEADLESS": "1"}) is False
    assert resolve_headless(True, env={"CHECKOUT_HEADLESS": "0"}) is True
    assert resolve_headless(None, env={"CHECKOUT_HEADLESS": "0"}) is False
    assert resolve_headless(None, env={}) is True


def test_resolve_product_url_argument_wins():
    assert resolve_product_url(DEV_PRODUCT_URL, env={}) == DEV_PRODUCT_URL


def test_resolve_product_url_from_env():
    env = {"SHOPIFY_DEV_STORE_PRODUCT_URL": DEV_PRODUCT_URL}
    assert resolve_product_url(None, env=env) == DEV_PRODUCT_URL


def test_resolve_product_url_missing_raises():
    with pytest.raises(CheckoutError) as exc:
        resolve_product_url(None, env={})
    assert "SHOPIFY_DEV_STORE_PRODUCT_URL" in str(exc.value)


def test_resolve_product_url_rejects_placeholder_and_garbage():
    with pytest.raises(CheckoutError):
        resolve_product_url("https://your-store.myshopify.com/products/your-product", env={})
    with pytest.raises(CheckoutError):
        resolve_product_url("not-a-url", env={})


def test_shipping_address_defaults_are_indian():
    address = shipping_address(env={})
    assert address["city"] == "Bengaluru"
    assert address["province_code"] == "KA"
    assert address["zip"] == "560001"
    assert address["country_code"] == "IN"
    assert address["phone"].startswith("+91")
    assert address is not DEFAULT_SHIPPING


def test_shipping_address_env_and_argument_overrides():
    address = shipping_address(
        overrides={"first_name": "Meera"},
        env={"CHECKOUT_SHIP_CITY": "Mysuru", "CHECKOUT_SHIP_ZIP": "570001"},
    )
    assert address["city"] == "Mysuru"
    assert address["zip"] == "570001"
    assert address["first_name"] == "Meera"
    assert address["province"] == "Karnataka"


# --------------------------------------------------------------------------
# region-aware shipping profiles (US dev store)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, "IN"),
        ({"CHECKOUT_ADDRESS_COUNTRY": "US"}, "US"),
        ({"CHECKOUT_ADDRESS_COUNTRY": "us"}, "US"),
        ({"CHECKOUT_ADDRESS_COUNTRY": " us "}, "US"),
        ({"CHECKOUT_ADDRESS_COUNTRY": "ZZ"}, "IN"),  # unknown -> default
        ({"CHECKOUT_ADDRESS_COUNTRY": ""}, "IN"),
    ],
)
def test_resolve_address_country(env, expected):
    assert resolve_address_country(env=env) == expected


def test_resolve_address_country_argument_wins():
    assert resolve_address_country("US", env={"CHECKOUT_ADDRESS_COUNTRY": "IN"}) == "US"


def test_shipping_profile_us_is_a_valid_us_address():
    address = shipping_profile("US", env={})
    assert address["country_code"] == "US"
    assert address["province_code"] == "CA"
    assert address["city"] == "San Francisco"
    assert address["zip"] == "94104"
    assert address["phone"].startswith("+1")
    assert address is not DEFAULT_SHIPPING


def test_shipping_profile_returns_a_copy():
    first = shipping_profile("US", env={})
    first["city"] = "Mutated"
    assert shipping_profile("US", env={})["city"] == "San Francisco"


def test_shipping_address_follows_country_env():
    address = shipping_address(env={"CHECKOUT_ADDRESS_COUNTRY": "US"})
    assert address["country_code"] == "US"
    assert address["city"] == "San Francisco"


def test_shipping_address_field_overrides_apply_on_top_of_us_profile():
    address = shipping_address(
        env={"CHECKOUT_ADDRESS_COUNTRY": "US", "CHECKOUT_SHIP_CITY": "Oakland"}
    )
    assert address["city"] == "Oakland"
    assert address["province_code"] == "CA"
    assert address["country_code"] == "US"


@pytest.mark.parametrize(
    "code,expected", [("US", "en-US"), ("IN", "en-IN"), ("", "en-US"), ("ZZ", "en-US")]
)
def test_locale_for_follows_country(code, expected):
    assert locale_for({"country_code": code}) == expected


# --------------------------------------------------------------------------
# card handling
# --------------------------------------------------------------------------


def test_mask_pan_shows_last_four_only():
    assert mask_pan("4111111111111111") == "**** **** **** 1111"
    assert "411111111111" not in mask_pan("4111111111111111")
    assert mask_pan("4111 1111 1111 1234") == "**** **** **** 1234"


def test_mask_pan_edge_cases():
    assert mask_pan(None) == "****"
    assert mask_pan("") == "****"
    assert mask_pan("123") == "***"


@pytest.mark.parametrize(
    "month,year,expected",
    [(12, 2027, "1227"), ("1", "2030", "0130"), ("09", "27", "0927"), (7, "2027", "0727")],
)
def test_format_expiry(month, year, expected):
    assert format_expiry(month, year) == expected


@pytest.mark.parametrize("month,year", [(0, 2027), (13, 2027), ("abc", 2027), (12, "7"), (12, "20277")])
def test_format_expiry_rejects_bad_input(month, year):
    with pytest.raises(CheckoutError):
        format_expiry(month, year)


def test_effective_card_number_uses_token_by_default():
    assert effective_card_number("4111 1111 1111 1111", env={}) == "4111111111111111"


def test_effective_card_number_rejects_empty_token():
    with pytest.raises(CheckoutError):
        effective_card_number("", env={})


@pytest.mark.parametrize(
    "raw,expected", [("1", "1"), ("2", "2"), ("3", "3"), ("true", "1"), ("yes", "1")]
)
def test_bogus_gateway_pan_modes(raw, expected):
    assert bogus_gateway_pan(env={"BOGUS_GATEWAY": raw}) == expected
    assert effective_card_number("4111111111111111", env={"BOGUS_GATEWAY": raw}) == expected


@pytest.mark.parametrize("env", [{}, {"BOGUS_GATEWAY": "0"}, {"BOGUS_GATEWAY": ""}])
def test_bogus_gateway_disabled(env):
    assert bogus_gateway_pan(env=env) is None


# --------------------------------------------------------------------------
# dev-store guard
# --------------------------------------------------------------------------


def test_host_of_and_dev_store_detection():
    assert host_of(DEV_PRODUCT_URL) == "gifting-demo.myshopify.com"
    assert is_dev_store_host("gifting-demo.myshopify.com") is True
    assert is_dev_store_host("giva.co") is False


def test_assert_purchase_allowed_permits_dev_store():
    assert_purchase_allowed(DEV_PRODUCT_URL, env={})


@pytest.mark.parametrize(
    "url",
    [
        "https://www.giva.co/products/silver-pendant",
        "https://giva.co/products/x",
        "https://www.mamaearth.com/product/y",
        "https://www.amazon.in/dp/B0",
        "https://www.nykaa.com/p/1",
    ],
)
def test_assert_purchase_allowed_blocks_real_merchants(url):
    with pytest.raises(CheckoutError) as exc:
        assert_purchase_allowed(url, env={})
    assert "real merchant" in str(exc.value).lower()


def test_real_merchant_block_cannot_be_overridden():
    with pytest.raises(CheckoutError):
        assert_purchase_allowed(
            "https://www.giva.co/products/x", env={"CHECKOUT_ALLOW_ANY_HOST": "1"}
        )


def test_assert_purchase_allowed_blocks_unknown_host_without_override():
    with pytest.raises(CheckoutError) as exc:
        assert_purchase_allowed("https://shop.example.com/products/x", env={})
    assert "myshopify.com" in str(exc.value)


def test_assert_purchase_allowed_allows_custom_host_with_override():
    assert_purchase_allowed(
        "https://shop.example.com/products/x", env={"CHECKOUT_ALLOW_ANY_HOST": "1"}
    )


# --------------------------------------------------------------------------
# dry-run safety: the Pay button must never be reached
# --------------------------------------------------------------------------


class _FakePage:
    url = "https://gifting-demo.myshopify.com/checkouts/c/tok"

    def __getattr__(self, _name):  # every page call is a harmless no-op
        return lambda *a, **k: None


def _stub_browser_flow(monkeypatch, calls):
    """Replace Playwright and every DOM step so only the flow logic runs."""
    import checkout.playwright_checkout as mod

    noop = lambda *a, **k: None  # noqa: E731
    monkeypatch.setattr(mod, "_import_playwright", lambda: object())

    class _Ctx:
        def new_page(self):
            return _FakePage()

        def close(self):
            return None

    monkeypatch.setattr(
        mod,
        "_launch",
        lambda *a, **k: (type("M", (), {"stop": noop})(), type("B", (), {"close": noop})(), _Ctx()),
    )
    for name in (
        "_open_product_page",
        "_add_to_cart",
        "_go_to_checkout",
        "_fill_contact",
        "_fill_shipping",
        "_select_shipping_method",
        "_advance_to_payment",
        "_fill_payment",
        "_screenshot",
    ):
        monkeypatch.setattr(mod, name, lambda *a, **k: calls.append("step"))
    monkeypatch.setattr(mod, "_submit_payment", lambda *a, **k: calls.append("PAID"))
    monkeypatch.setattr(
        mod,
        "_await_outcome",
        lambda *a, **k: build_result(STATUS_APPROVED, "ok", order_id="1001"),
    )
    return mod


def test_dry_run_never_submits_payment(monkeypatch):
    calls = []
    mod = _stub_browser_flow(monkeypatch, calls)
    result = mod.run_shopify_checkout(
        token="4111111111111111",
        dynamic_cvv="123",
        expiry_month=12,
        expiry_year=2029,
        product_url=DEV_PRODUCT_URL,
        dry_run=True,
    )
    assert "PAID" not in calls
    assert result.status == STATUS_FAILED
    assert result.order_id is None
    assert "dry_run_stopped_before_pay" in result.details["steps"]


def test_non_dry_run_does_submit_payment(monkeypatch):
    calls = []
    mod = _stub_browser_flow(monkeypatch, calls)
    result = mod.run_shopify_checkout(
        token="4111111111111111",
        dynamic_cvv="123",
        expiry_month=12,
        expiry_year=2029,
        product_url=DEV_PRODUCT_URL,
        dry_run=False,
    )
    assert "PAID" in calls
    assert result.status == STATUS_APPROVED


# --------------------------------------------------------------------------
# smoke CLI argument safety (no browser involved)
# --------------------------------------------------------------------------


def _load_smoke_module():
    import importlib.util

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts",
        "checkout_smoke.py",
    )
    spec = importlib.util.spec_from_file_location("checkout_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_refuses_purchase_without_product_url(monkeypatch, capsys):
    smoke = _load_smoke_module()
    monkeypatch.delenv("SHOPIFY_DEV_STORE_PRODUCT_URL", raising=False)
    code = smoke.main(
        ["--token", "4111111111111111", "--cvv", "123", "--month", "12", "--year", "2027"]
    )
    assert code == 2
    assert "Refusing to run a purchase" in capsys.readouterr().err


def test_smoke_refuses_purchase_without_card_args(monkeypatch, capsys):
    smoke = _load_smoke_module()
    monkeypatch.setenv("SHOPIFY_DEV_STORE_PRODUCT_URL", DEV_PRODUCT_URL)
    code = smoke.main(["--product-url", DEV_PRODUCT_URL])
    assert code == 2
    assert "missing card arguments" in capsys.readouterr().err


def test_smoke_parser_accepts_documented_flags():
    smoke = _load_smoke_module()
    args = smoke.build_parser().parse_args(
        [
            "--product-url", DEV_PRODUCT_URL,
            "--token", "4111111111111111",
            "--cvv", "123",
            "--month", "12",
            "--year", "2027",
            "--headed",
            "--country", "US",
            "--dry-run",
        ]
    )
    assert args.product_url == DEV_PRODUCT_URL
    assert args.headed is True
    assert args.country == "US"
    assert args.dry_run is True
    assert args.verify_store is False


def test_smoke_country_defaults_to_none_and_rejects_unknown_regions():
    smoke = _load_smoke_module()
    assert smoke.build_parser().parse_args([]).country is None
    with pytest.raises(SystemExit):
        smoke.build_parser().parse_args(["--country", "ZZ"])
