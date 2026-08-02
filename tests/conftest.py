import pytest


@pytest.fixture(autouse=True)
def _prava_allow_real_by_default(monkeypatch):
    """The PRAVA_ALLOW_REAL quota-burn guard (see main._tool_mint_scoped_card)
    refuses to call Prava's real API unless explicitly opted in — that guard is
    for humans running the server locally against a live key by accident.

    Every test that exercises the mint flow does so through a mocked/autospec'd
    PravaClient (see test_integration_wiring.py) — never a real network call —
    so the guard has nothing to protect against here. Default it on for the
    whole test session; a test that specifically wants to exercise the guard's
    refusal path can override with monkeypatch.delenv("PRAVA_ALLOW_REAL",
    raising=False).
    """
    monkeypatch.setenv("PRAVA_ALLOW_REAL", "1")
