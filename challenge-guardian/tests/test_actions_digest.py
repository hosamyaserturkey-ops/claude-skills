"""Tests for order placement (close/flatten), auto-flatten, ULIDs, and the digest."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from guardian.config import make_preset
from guardian.digest import build_digest
from guardian.monitor import run_monitor
from guardian.propr import ProprClient, new_ulid
from guardian.rules import TrackerState, budget_consumed, evaluate
from guardian.snapshot import AccountSnapshot

NOW = datetime(2026, 6, 10, 20, 5, tzinfo=timezone.utc)


def test_ulid_format_and_uniqueness():
    ulids = {new_ulid() for _ in range(200)}
    assert len(ulids) == 200
    for u in ulids:
        assert len(u) == 26
        assert all(c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for c in u)


class OrderCaptureClient(ProprClient):
    def __init__(self, positions):
        super().__init__(api_key="pk_test_fake")
        self.account_id = "urn:prp-account:abc"
        self.positions = positions
        self.posted: list[tuple[str, dict]] = []

    def fetch_open_positions(self):
        return self.positions

    def _post(self, path, payload):
        self.posted.append((path, payload))
        return {"data": [{"orderId": "o1", "status": "filled"}]}


LONG_BTC = {"positionId": "p1", "positionSide": "long", "quantity": "0.5",
            "base": "BTC", "asset": "BTC", "quote": "USDC"}
SHORT_ETH = {"positionId": "p2", "positionSide": "short", "quantity": "2",
             "base": "ETH", "asset": "ETH", "quote": "USDC"}


def test_close_position_builds_reduce_only_market_order():
    client = OrderCaptureClient([LONG_BTC])
    result = client.close_position(LONG_BTC)
    assert result["status"] == "filled"
    path, payload = client.posted[0]
    assert path == "/accounts/urn:prp-account:abc/orders"
    order = payload["orders"][0]
    assert order["side"] == "sell"            # closing a long sells
    assert order["reduceOnly"] is True
    assert order["closePosition"] is True
    assert order["type"] == "market"
    assert order["timeInForce"] == "IOC"
    assert order["quantity"] == "0.5"
    assert len(order["intentId"]) == 26


def test_flatten_closes_everything_and_filters_by_base():
    client = OrderCaptureClient([LONG_BTC, SHORT_ETH])
    results = client.flatten_positions()
    assert [r["ok"] for r in results] == [True, True]
    sides = [p["orders"][0]["side"] for _, p in client.posted]
    assert sides == ["sell", "buy"]           # close long sells, close short buys

    client.posted.clear()
    results = client.flatten_positions(base="eth")
    assert len(results) == 1 and "ETH" in results[0]["position"]


def test_flatten_continues_after_one_failure():
    client = OrderCaptureClient([LONG_BTC, SHORT_ETH])
    original = client._post

    def flaky(path, payload):
        if payload["orders"][0]["base"] == "BTC":
            raise RuntimeError("rejected")
        return original(path, payload)

    client._post = flaky
    results = client.flatten_positions()
    assert [r["ok"] for r in results] == [False, True]


def test_budget_consumed():
    cfg = make_preset("1step", 10_000.0)
    state = TrackerState()
    evaluate(cfg, state, 10_000.0)
    consumed = budget_consumed(cfg, state, 9_790.0)   # daily budget 300 -> 70%
    assert consumed["daily_loss"] == pytest.approx(0.7)
    assert consumed["drawdown"] == pytest.approx(210 / 600)


def test_auto_flatten_triggers_in_monitor(tmp_path):
    cfg = make_preset("1step", 10_000.0)

    class Client:
        label = "stub"
        flattened = 0
        snaps = [
            AccountSnapshot(equity=10_000.0, open_positions=[LONG_BTC]),
            AccountSnapshot(equity=9_715.0, open_positions=[LONG_BTC]),  # 95% of daily
        ]

        def fetch_snapshot(self):
            return self.snaps.pop(0)

        def flatten_positions(self, base=None):
            self.flattened += 1
            return [{"position": "LONG 0.5 BTC", "ok": True, "detail": "filled"}]

    sent = []

    class Capture:
        def send(self, text):
            sent.append(text)

    client = Client()
    run_monitor(cfg, client, [Capture()], tmp_path / "s.json",
                poll_interval=0, max_iterations=2, auto_flatten_at=0.95)
    assert client.flattened == 1
    assert any("AUTO-FLATTEN" in m for m in sent)


def test_auto_flatten_disarmed_without_threshold(tmp_path):
    cfg = make_preset("1step", 10_000.0)

    class Client:
        label = "stub"
        flattened = 0
        snaps = [AccountSnapshot(equity=9_715.0, open_positions=[LONG_BTC])]

        def fetch_snapshot(self):
            return self.snaps.pop(0)

        def flatten_positions(self, base=None):
            self.flattened += 1
            return []

    client = Client()
    run_monitor(cfg, client, [], tmp_path / "s.json",
                poll_interval=0, max_iterations=1, auto_flatten_at=None)
    assert client.flattened == 0


def trade(pnl, fee, ttype="close", when="2026-06-10T15:00:00Z"):
    return {"realizedPnl": str(pnl), "fee": str(fee), "type": ttype, "executedAt": when}


def test_digest_summarizes_today_only():
    cfg = make_preset("2step-1", 10_000.0)   # step 1 target 5% -> 10,500
    state = TrackerState()
    evaluate(cfg, state, 10_000.0, NOW)
    trades = [
        trade(50, 1.0),
        trade(-20, 0.5),
        trade(30, 0.2, ttype="open"),                      # not a closing trade
        trade(999, 9.9, when="2026-06-09T15:00:00Z"),      # yesterday: ignored
    ]
    text = build_digest(trades, cfg, state, equity=10_030.0, open_positions=1, now=NOW)
    assert "+30.00 USDC" in text                # 50 - 20, yesterday excluded
    assert "2 closing trades" in text
    assert "win rate 50%" in text
    assert "$1.70" in text                      # fees: 1.0 + 0.5 + 0.2
    assert "$470.00 to go" in text              # 10,500 - 10,030
    assert "Open positions going into tomorrow: 1" in text


def test_digest_sent_once_per_day_via_monitor(tmp_path):
    cfg = make_preset("1step", 10_000.0)

    class Client:
        label = "stub"
        snaps = [AccountSnapshot(equity=10_000.0)] * 3

        def fetch_snapshot(self):
            return self.snaps.pop(0)

        def fetch_trades(self, limit=100):
            return []

    sent = []

    class Capture:
        def send(self, text):
            sent.append(text)

    run_monitor(cfg, Client(), [Capture()], tmp_path / "s.json",
                poll_interval=0, max_iterations=3, digest_hour=0)
    digests = [m for m in sent if "Daily digest" in m]
    assert len(digests) == 1
