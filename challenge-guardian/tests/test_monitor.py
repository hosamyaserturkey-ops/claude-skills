"""End-to-end loop test with a stubbed Hyperliquid client and capture alerter."""

from __future__ import annotations

import requests

from guardian.config import make_preset
from guardian.hyperliquid import AccountSnapshot
from guardian.monitor import run_monitor
from guardian.state import load_state


class StubClient:
    label = "stub"

    def __init__(self, equities):
        self.equities = list(equities)

    def fetch_snapshot(self) -> AccountSnapshot:
        value = self.equities.pop(0)
        if value is None:
            raise requests.ConnectionError("stubbed outage")
        return AccountSnapshot(equity=value, withdrawable=value, open_positions=[])


class CaptureAlerter:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, text: str) -> None:
        self.messages.append(text)


def test_breach_stops_loop_and_persists(tmp_path):
    cfg = make_preset("1step", 10_000.0)
    capture = CaptureAlerter()
    state_path = tmp_path / "state.json"
    # Healthy -> warning territory -> breach. Floor: daily 9,700.
    client = StubClient([10_000.0, 9_780.0, 9_650.0, 9_999.0])

    code = run_monitor(cfg, client, [capture], state_path,
                       poll_interval=0, max_iterations=10)

    assert code == 2
    assert any("BREACH" in m for m in capture.messages)
    assert load_state(state_path).breached
    assert client.equities == [9_999.0]  # loop stopped at the breach

    # A restart refuses to "guard" an already-breached account.
    assert run_monitor(cfg, client, [capture], state_path,
                       poll_interval=0, max_iterations=10) == 2


def test_once_exits_nonzero_when_api_unreachable(tmp_path):
    cfg = make_preset("1step", 10_000.0)
    client = StubClient([None])
    code = run_monitor(cfg, client, [CaptureAlerter()], tmp_path / "s.json",
                       poll_interval=0, max_iterations=1)
    assert code == 1


def test_state_survives_restart_and_keeps_trailing_peak(tmp_path):
    cfg = make_preset("2step-1", 10_000.0)
    state_path = tmp_path / "state.json"
    run_monitor(cfg, StubClient([11_000.0]), [CaptureAlerter()], state_path,
                poll_interval=0, max_iterations=1)
    assert load_state(state_path).peak_equity == 11_000.0

    # After restart the trailed floor (11,000 * 0.92 = 10,120) still applies.
    capture = CaptureAlerter()
    code = run_monitor(cfg, StubClient([10_100.0]), [capture], state_path,
                       poll_interval=0, max_iterations=1)
    assert code == 2
    assert any("drawdown" in m.lower() or "BREACH" in m for m in capture.messages)
