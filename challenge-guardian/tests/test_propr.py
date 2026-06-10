"""Tests for the Propr API client against canned responses from the docs."""

from __future__ import annotations

import pytest

from guardian.monitor import run_monitor
from guardian.propr import ProprClient, _as_fraction


class FakeProprClient(ProprClient):
    """ProprClient with _get replaced by canned responses keyed by path."""

    def __init__(self, responses: dict):
        super().__init__(api_key="pk_test_fake")
        self.responses = responses
        self.calls: list[str] = []

    def _get(self, path: str, params: dict | None = None):
        self.calls.append(path)
        result = self.responses[path]
        if isinstance(result, Exception):
            raise result
        return result


ACCOUNT_ID = "urn:prp-account:abc123XY"
ATTEMPT_ID = "urn:prp-attempt:zzz999"

BASE_RESPONSES = {
    "/book-account-issuances": {"data": []},
    "/challenge-attempts": {"data": [{
        "attemptId": ATTEMPT_ID,
        "accountId": ACCOUNT_ID,
        "challengeId": "urn:prp-challenge:c1",
        "status": "active",
    }]},
    f"/challenge-attempts/{ATTEMPT_ID}": {"status": "active", "failureReason": None},
    f"/accounts/{ACCOUNT_ID}": {
        "balance": "9900.50",
        "availableBalance": "9000",
        "totalUnrealizedPnl": "-150.25",
        "isolatedPositionMargin": "0",
        "highWaterMark": "10200",
    },
    f"/accounts/{ACCOUNT_ID}/positions": {"data": [
        {"positionId": "p1", "quantity": "0.5", "unrealizedPnl": "-150.25"},
        {"positionId": "p2", "quantity": "0", "unrealizedPnl": "0"},  # closed remnant
    ]},
    "/challenges": {"data": [{
        "challengeId": "urn:prp-challenge:c1",
        "initialBalance": "10000",
        "maxDailyLoss": 3,
        "maxDrawdown": 6,
    }]},
}


def test_discover_prefers_funded_account():
    responses = dict(BASE_RESPONSES)
    responses["/book-account-issuances"] = {"data": [{
        "issuanceId": "urn:prp-issuance:f1",
        "accountId": "urn:prp-account:funded1",
        "status": "active",
    }]}
    client = FakeProprClient(responses)
    client.discover()
    assert client.kind == "funded"
    assert client.account_id == "urn:prp-account:funded1"


def test_discover_falls_back_to_challenge():
    client = FakeProprClient(dict(BASE_RESPONSES))
    client.discover()
    assert client.kind == "challenge"
    assert client.account_id == ACCOUNT_ID
    assert client.record_id == ATTEMPT_ID


def test_discover_with_no_accounts_exits():
    client = FakeProprClient({
        "/book-account-issuances": {"data": []},
        "/challenge-attempts": {"data": []},
    })
    with pytest.raises(SystemExit):
        client.discover()


def test_snapshot_equity_formula_and_zero_position_filter():
    client = FakeProprClient(dict(BASE_RESPONSES))
    client.discover()
    snap = client.fetch_snapshot()
    # equity = balance + totalUnrealizedPnl + isolatedPositionMargin
    assert snap.equity == pytest.approx(9900.50 - 150.25)
    assert snap.high_water_mark == pytest.approx(10200.0)
    assert [p["positionId"] for p in snap.open_positions] == ["p1"]
    assert snap.server_status == "active"


def test_detect_challenge_config_normalizes_percent():
    client = FakeProprClient(dict(BASE_RESPONSES))
    client.discover()
    detected = client.detect_challenge_config()
    assert detected == {
        "starting_balance": 10000.0,
        "max_daily_loss_pct": 0.03,
        "max_drawdown_pct": 0.06,
    }


def test_as_fraction():
    assert _as_fraction(3) == 0.03
    assert _as_fraction(0.03) == 0.03


def test_server_declared_failure_stops_monitor(tmp_path):
    from guardian.config import make_preset

    responses = dict(BASE_RESPONSES)
    responses[f"/challenge-attempts/{ATTEMPT_ID}"] = {
        "status": "failed", "failureReason": "max_daily_loss_hit",
    }
    # Equity alone looks healthy — only the server verdict says failed.
    responses[f"/accounts/{ACCOUNT_ID}"] = {
        "balance": "9950", "availableBalance": "9950",
        "totalUnrealizedPnl": "0", "isolatedPositionMargin": "0",
        "highWaterMark": "10000",
    }
    client = FakeProprClient(responses)
    client.discover()

    sent = []

    class Capture:
        def send(self, text):
            sent.append(text)

    code = run_monitor(make_preset("1step", 10_000.0), client, [Capture()],
                       tmp_path / "s.json", poll_interval=0, max_iterations=5)
    assert code == 2
    assert any("max_daily_loss_hit" in m for m in sent)


def test_server_high_water_mark_tightens_trailing_floor(tmp_path):
    from guardian.config import make_preset

    # Local peak never saw 10,200, but the server HWM did. With an 8% trailing
    # relative floor that means 10,200 * 0.92 = 9,384 — equity 9,750 is healthy,
    # but the floor must reflect the server peak.
    client = FakeProprClient(dict(BASE_RESPONSES))
    client.discover()

    from guardian.state import load_state

    code = run_monitor(make_preset("2step-1", 10_000.0), client, [],
                       tmp_path / "s.json", poll_interval=0, max_iterations=1)
    assert code == 0
    assert load_state(tmp_path / "s.json").peak_equity == pytest.approx(10_200.0)
