"""Tests for the multi-tenant worker: store REST calls, reconciliation, stop."""

from __future__ import annotations

import threading
import time

from guardian.config import make_preset
from guardian.monitor import run_monitor
from guardian.snapshot import AccountSnapshot
from guardian.tenants import SupabaseBoard, SupabaseStore, TenantRunner


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def make_store(tenants_payload):
    store = SupabaseStore("https://x.supabase.co", "service-key")
    store.calls = []

    def fake_get(url, params=None, timeout=None):
        store.calls.append(("GET", url, params))
        return FakeResponse(tenants_payload)

    def fake_post(url, json=None, timeout=None):
        store.calls.append(("POST", url, json))
        return FakeResponse({})

    store.session.get = fake_get
    store.session.post = fake_post
    return store


def test_store_requests_use_service_key_and_rest_paths():
    store = make_store([])
    assert store.session.headers["apikey"] == "service-key"
    assert store.session.headers["Authorization"] == "Bearer service-key"
    store.fetch_tenants()
    method, url, params = store.calls[0]
    assert url == "https://x.supabase.co/rest/v1/tenants"
    assert params["active"] == "eq.true"
    store.insert("guardian_events", {"tenant_id": "t1", "message": "hi"})
    method, url, body = store.calls[1]
    assert url == "https://x.supabase.co/rest/v1/guardian_events"
    assert body["message"] == "hi"


def test_board_throttles_sample_pushes():
    store = make_store([])
    board = SupabaseBoard(store, "t1", every=9999)
    board.update("acct", equity=1.0, daily_floor=0.5, dd_floor=0.4, peak=1.0, positions=0)
    board.update("acct", equity=2.0, daily_floor=0.5, dd_floor=0.4, peak=2.0, positions=0)
    pushes = [c for c in store.calls if c[0] == "POST"]
    assert len(pushes) == 1                     # second update inside throttle window
    assert pushes[0][2]["equity"] == 1.0
    assert pushes[0][2]["tenant_id"] == "t1"


def tenant_row(tid="t1", **overrides):
    row = {"id": tid, "propr_api_key": "pk_live_x", "preset": "1step",
           "active": True, "enable_actions": False}
    row.update(overrides)
    return row


class GuardSpy:
    def __init__(self):
        self.started = []
        self.stops = []

    def __call__(self, row, store, stop_event):
        self.started.append(row["id"])
        self.stops.append(stop_event)
        stop_event.wait(5)


def test_runner_starts_stops_and_restarts_tenants():
    store = make_store([tenant_row("t1")])
    spy = GuardSpy()
    runner = TenantRunner(store, guard=spy)

    runner.reconcile()
    time.sleep(0.05)
    assert spy.started == ["t1"]

    # Same row again: nothing new starts.
    runner.reconcile()
    assert spy.started == ["t1"]

    # Settings change: old guard stopped, new one started.
    store.session.get = lambda url, params=None, timeout=None: FakeResponse(
        [tenant_row("t1", preset="2step-1")])
    runner.reconcile()
    time.sleep(0.05)
    assert spy.stops[0].is_set()
    assert spy.started == ["t1", "t1"]

    # Tenant deactivated: guard stopped, not restarted.
    store.session.get = lambda url, params=None, timeout=None: FakeResponse([])
    runner.reconcile()
    assert spy.stops[1].is_set()
    assert spy.started == ["t1", "t1"]
    assert runner.running == {}


def test_runner_survives_guard_crash_and_logs_event():
    store = make_store([tenant_row("t9")])

    def crashing(row, s, stop_event):
        raise RuntimeError("bad key")

    runner = TenantRunner(store, guard=crashing)
    runner.reconcile()
    time.sleep(0.1)
    events = [c for c in store.calls
              if c[0] == "POST" and c[1].endswith("guardian_events")]
    assert events and "bad key" in events[0][2]["message"]


def test_monitor_stop_event_exits_cleanly(tmp_path):
    cfg = make_preset("1step", 10_000.0)
    stop = threading.Event()

    class Client:
        label = "stub"

        def fetch_snapshot(self):
            return AccountSnapshot(equity=10_000.0)

    stop.set()
    code = run_monitor(cfg, Client(), [], tmp_path / "s.json",
                       poll_interval=0, stop_event=stop)
    assert code == 0
