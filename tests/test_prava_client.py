"""Tests for prava/client.py.

All network calls are mocked via httpx.MockTransport -- this suite MUST
NEVER hit the real Prava sandbox API (30 txn/day cap, shared with the
orchestrator).
"""

from __future__ import annotations

import json

import httpx
import pytest

from prava.client import PravaClient, PravaError, mask_secret

SESSION_ID = "sess_abc123"
ORDER_ID = "order_xyz789"
TXN_REF_ID = "txn_ref_001"


def make_client(handler) -> PravaClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(
        base_url="https://sandbox.api.prava.space",
        headers={"Authorization": "Bearer sk_test_fake"},
        transport=transport,
    )
    return PravaClient(secret_key="sk_test_fake", client=http_client)


def create_session_kwargs(**overrides):
    kwargs = dict(
        user_id="user-1",
        user_email="buyer@example.com",
        total_amount="2999.00",
        currency="INR",
        merchant_name="Giva Jewelry",
        merchant_url="https://giva.co",
        country_code_iso2="IN",
        product_details=[{"description": "Necklace", "unit_price": "2999.00", "quantity": 1}],
    )
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# Happy path: create -> poll (awaiting_result with credential) -> report
# ---------------------------------------------------------------------------


def test_happy_path_create_poll_report():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))

        if request.method == "POST" and request.url.path == "/v1/sessions":
            body = json.loads(request.content)
            assert body["user_id"] == "user-1"
            assert body["purchase_context"][0]["merchant_details"]["name"] == "Giva Jewelry"
            assert len(body["purchase_context"]) == 1
            assert body["integration_type"] == "full_checkout"
            return httpx.Response(
                201,
                json={
                    "session_id": SESSION_ID,
                    "session_token": "stok_1",
                    "iframe_url": "https://sandbox.api.prava.space/iframe/abc",
                    "order_id": ORDER_ID,
                    "expires_at": "2026-08-01T12:15:00Z",
                },
            )

        if request.method == "GET" and request.url.path == f"/v1/sessions/{SESSION_ID}/payment-result":
            return httpx.Response(
                200,
                json={
                    "session_id": SESSION_ID,
                    "order_id": ORDER_ID,
                    "status": "awaiting_result",
                    "transactions": [
                        {
                            "txn_id": "txn_1",
                            "status": "awaiting_result",
                            "line_items": [
                                {
                                    "txn_ref_id": TXN_REF_ID,
                                    "merchant_name": "Giva Jewelry",
                                    "merchant_url": "https://giva.co",
                                    "total_amount": "2999.00",
                                    "status": "awaiting_result",
                                    "token": "4111111111111234",
                                    "dynamic_cvv": "999",
                                    "expiry_month": "12",
                                    "expiry_year": "27",
                                    "products": [],
                                }
                            ],
                        }
                    ],
                },
            )

        if (
            request.method == "POST"
            and request.url.path == f"/v1/sessions/{SESSION_ID}/report-status"
        ):
            body = json.loads(request.content)
            assert body == {"txn_ref_id": TXN_REF_ID, "txn_status": "APPROVED"}
            return httpx.Response(200, json={})

        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    client = make_client(handler)

    session = client.create_session(**create_session_kwargs())
    assert session.session_id == SESSION_ID
    assert session.order_id == ORDER_ID
    assert session.iframe_url.startswith("https://")

    result = client.get_payment_result(session.session_id)
    assert result.status == "awaiting_result"
    credential = result.first_credential()
    assert credential is not None
    assert credential.txn_ref_id == TXN_REF_ID
    assert credential.token == "4111111111111234"
    assert credential.dynamic_cvv == "999"

    client.report_status(session.session_id, credential.txn_ref_id, "APPROVED")

    assert ("POST", "/v1/sessions") in calls
    assert ("GET", f"/v1/sessions/{SESSION_ID}/payment-result") in calls
    assert ("POST", f"/v1/sessions/{SESSION_ID}/report-status") in calls


# ---------------------------------------------------------------------------
# Error mapping: 400 with error body -> PravaError with code
# ---------------------------------------------------------------------------


def test_error_mapping_400_raises_prava_error_with_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "invalid_request",
                    "message": "total_amount is required",
                    "details": {"field": "total_amount"},
                }
            },
        )

    client = make_client(handler)

    with pytest.raises(PravaError) as exc_info:
        client.create_session(**create_session_kwargs())

    err = exc_info.value
    assert err.code == "invalid_request"
    assert err.http_status == 400
    assert "total_amount is required" in err.message


def test_error_mapping_non_json_error_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal server error")

    client = make_client(handler)

    with pytest.raises(PravaError) as exc_info:
        client.get_payment_result(SESSION_ID)

    err = exc_info.value
    assert err.http_status == 500
    assert err.code is None


def test_error_mapping_preserves_top_level_code_and_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"code": "PROVISION_ERROR", "message": "Request failed with status code 400"},
        )

    client = make_client(handler)

    with pytest.raises(PravaError) as exc_info:
        client.create_session(**create_session_kwargs())

    assert exc_info.value.code == "PROVISION_ERROR"
    assert exc_info.value.message == "Request failed with status code 400"


def test_create_session_rejects_invalid_merchant_country_before_network_call():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid merchant country must not reach Prava")

    client = make_client(handler)
    with pytest.raises(PravaError, match="two-letter ISO") as exc_info:
        client.create_session(**create_session_kwargs(country_code_iso2="India"))

    assert exc_info.value.code == "INVALID_MERCHANT_COUNTRY"


# ---------------------------------------------------------------------------
# wait_for_result returns as soon as a credential appears
# ---------------------------------------------------------------------------


def test_wait_for_result_returns_on_credential():
    poll_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions":
            return httpx.Response(
                201,
                json={
                    "session_id": SESSION_ID,
                    "session_token": "stok_1",
                    "iframe_url": "https://sandbox.api.prava.space/iframe/abc",
                    "order_id": ORDER_ID,
                    "expires_at": "2026-08-01T12:15:00Z",
                },
            )

        poll_count["n"] += 1
        if poll_count["n"] == 1:
            # First poll: still pending, no credential yet.
            return httpx.Response(
                200,
                json={
                    "session_id": SESSION_ID,
                    "order_id": ORDER_ID,
                    "status": "pending",
                    "transactions": [],
                },
            )

        # Second poll: credential now available.
        return httpx.Response(
            200,
            json={
                "session_id": SESSION_ID,
                "order_id": ORDER_ID,
                "status": "awaiting_result",
                "transactions": [
                    {
                        "txn_id": "txn_1",
                        "status": "awaiting_result",
                        "line_items": [
                            {
                                "txn_ref_id": TXN_REF_ID,
                                "merchant_name": "Giva Jewelry",
                                "merchant_url": "https://giva.co",
                                "total_amount": "2999.00",
                                "status": "awaiting_result",
                                "token": "4111111111111234",
                                "dynamic_cvv": "999",
                                "expiry_month": "12",
                                "expiry_year": "27",
                                "products": [],
                            }
                        ],
                    }
                ],
            },
        )

    client = make_client(handler)
    result = client.wait_for_result(SESSION_ID, timeout_seconds=5, poll_interval=0.01)

    assert result.first_credential() is not None
    assert poll_count["n"] == 2


def test_wait_for_result_returns_on_terminal_status_without_credential():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "session_id": SESSION_ID,
                "order_id": ORDER_ID,
                "status": "failed",
                "transactions": [],
            },
        )

    client = make_client(handler)
    result = client.wait_for_result(SESSION_ID, timeout_seconds=5, poll_interval=0.01)

    assert result.status == "failed"
    assert result.first_credential() is None
    assert result.is_terminal()


def test_payment_result_get_never_advertises_an_empty_json_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("content-type") is None
        assert request.content == b""
        return httpx.Response(
            200,
            json={"session_id": SESSION_ID, "order_id": ORDER_ID, "status": "pending", "transactions": []},
        )

    client = make_client(handler)
    assert client.get_payment_result(SESSION_ID).status == "pending"


# ---------------------------------------------------------------------------
# report_status rejects invalid status strings locally (no network call)
# ---------------------------------------------------------------------------


def test_report_status_rejects_invalid_status_locally():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("report_status must validate locally before making a network call")

    client = make_client(handler)

    with pytest.raises(PravaError):
        client.report_status(SESSION_ID, TXN_REF_ID, "MAYBE")


@pytest.mark.parametrize("status", ["APPROVED", "DECLINED"])
def test_report_status_accepts_valid_statuses(status):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["txn_status"] == status
        return httpx.Response(200, json={})

    client = make_client(handler)
    client.report_status(SESSION_ID, TXN_REF_ID, status)


# ---------------------------------------------------------------------------
# Secret masking
# ---------------------------------------------------------------------------


def test_mask_secret_shows_only_last_four():
    assert mask_secret("4111111111111234") == "************1234"
    assert mask_secret(None) is None
    assert mask_secret("12") == "**"


def test_line_item_repr_never_exposes_full_token():
    from prava.client import LineItem

    item = LineItem(
        txn_ref_id=TXN_REF_ID,
        merchant_name="Giva Jewelry",
        merchant_url="https://giva.co",
        total_amount="2999.00",
        status="awaiting_result",
        token="4111111111111234",
        dynamic_cvv="999",
    )
    r = repr(item)
    assert "4111111111111234" not in r
    assert "999" not in r  # cvv is <=4 chars, so fully masked
    assert "1234" in r  # last 4 of token remain visible by design
