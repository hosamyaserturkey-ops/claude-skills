"""Multi-tenant mode: guard every signed-up trader from a Supabase table.

The worker (this module) authenticates to Supabase with the service-role key
and reconciles its running guards against the `tenants` table once a minute:
new active rows start a guard, deactivated rows stop one, edited rows restart
one. Each tenant gets its own alert channels, Telegram command listener, and
state directory. Equity samples and alert events are written back to Supabase
for the dashboard, throttled and best-effort — a database hiccup must never
stop the guarding.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import requests

from .alerts import ConsoleAlerter, DiscordAlerter, TelegramAlerter
from .config import make_preset
from .monitor import run_monitor, run_parallel
from .propr import ProprClient
from .status import StatusBoard
from .telegram_commands import TelegramCommandListener


class SupabaseStore:
    """Thin REST client for the worker's reads/writes (service-role key)."""

    def __init__(self, url: str, service_key: str, timeout: float = 10.0):
        self.rest = url.rstrip("/") + "/rest/v1"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        })

    def fetch_tenants(self) -> list[dict]:
        resp = self.session.get(
            f"{self.rest}/tenants", params={"active": "eq.true", "select": "*"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def insert(self, table: str, row: dict) -> None:
        resp = self.session.post(f"{self.rest}/{table}", json=row, timeout=self.timeout)
        resp.raise_for_status()


class SupabaseBoard(StatusBoard):
    """StatusBoard that also streams throttled equity samples to Supabase."""

    def __init__(self, store: SupabaseStore, tenant_id: str, every: float = 30.0):
        super().__init__()
        self.store = store
        self.tenant_id = tenant_id
        self.every = every
        self._last_push: dict[str, float] = {}

    def update(self, label: str, **fields) -> None:
        super().update(label, **fields)
        now = time.time()
        if now - self._last_push.get(label, 0) < self.every:
            return
        self._last_push[label] = now
        try:
            self.store.insert("equity_samples", {
                "tenant_id": self.tenant_id,
                "account_label": label,
                "equity": fields.get("equity"),
                "daily_floor": fields.get("daily_floor"),
                "dd_floor": fields.get("dd_floor"),
                "peak": fields.get("peak"),
                "positions": fields.get("positions"),
            })
        except Exception as exc:  # dashboard data is best-effort
            print(f"sample push failed: {exc}", file=sys.stderr, flush=True)


class SupabaseAlerter:
    """Alert sink that records every alert in the dashboard's event feed."""

    def __init__(self, store: SupabaseStore, tenant_id: str):
        self.store = store
        self.tenant_id = tenant_id

    def send(self, text: str) -> None:
        try:
            self.store.insert("guardian_events",
                              {"tenant_id": self.tenant_id, "message": text[:1000]})
        except Exception as exc:
            print(f"event push failed: {exc}", file=sys.stderr, flush=True)


def _tenant_alerters(tenant: dict, store: SupabaseStore) -> list:
    alerters: list = [ConsoleAlerter(), SupabaseAlerter(store, tenant["id"])]
    if tenant.get("discord_webhook"):
        alerters.append(DiscordAlerter(tenant["discord_webhook"]))
    if tenant.get("telegram_token") and tenant.get("telegram_chat_id"):
        alerters.append(TelegramAlerter(tenant["telegram_token"],
                                        str(tenant["telegram_chat_id"])))
    return alerters


def guard_tenant(tenant: dict, store: SupabaseStore, stop_event: threading.Event) -> int:
    """Guard all of one tenant's accounts until stopped. Runs in its own thread."""
    tenant_id = tenant["id"]
    api_key = tenant["propr_api_key"]
    alerters = _tenant_alerters(tenant, store)
    board = SupabaseBoard(store, tenant_id)

    client = ProprClient(api_key)
    accounts = client.list_accounts()
    if not accounts:
        SupabaseAlerter(store, tenant_id).send(
            "⚠️ Guardian: no active challenge or funded account found on this "
            "Propr API key. Purchase a challenge, then toggle the guardian off "
            "and on again.")
        return 1

    action_clients = None
    if tenant.get("enable_actions"):
        action_clients = {}
        for acc in accounts:
            c = ProprClient(api_key)
            c.select(acc)
            action_clients[c.label] = c
    if tenant.get("telegram_token") and tenant.get("telegram_chat_id"):
        TelegramCommandListener(tenant["telegram_token"], str(tenant["telegram_chat_id"]),
                                board, action_clients=action_clients).start()

    jobs = []
    for acc in accounts:
        c = ProprClient(api_key)
        c.select(acc)
        detected = c.detect_challenge_config()
        balance = detected.get("starting_balance") or float(tenant.get("balance") or 0)
        if balance <= 0:
            SupabaseAlerter(store, tenant_id).send(
                f"⚠️ Guardian: could not detect the starting balance for "
                f"{c.account_id}. Set 'balance' in your guardian settings.")
            continue
        cfg = make_preset(tenant.get("preset") or "1step", balance)
        overrides = {k: v for k, v in detected.items()
                     if k in ("max_daily_loss_pct", "max_drawdown_pct")}
        if overrides:
            cfg = replace(cfg, **overrides)
        state_path = Path("state") / tenant_id / f"{c.account_id.replace(':', '_')}.json"
        auto_flatten = (float(tenant["auto_flatten_at"])
                        if tenant.get("enable_actions") and tenant.get("auto_flatten_at")
                        else None)
        digest_hour = tenant.get("digest_hour")
        jobs.append(lambda c=c, cfg=cfg, sp=state_path, af=auto_flatten, dh=digest_hour:
                    run_monitor(cfg, c, alerters, sp,
                                auto_flatten_at=af,
                                digest_hour=int(dh) if dh is not None else None,
                                stop_event=stop_event,
                                status_board=board))
    if not jobs:
        return 1
    return run_parallel(jobs)


def _row_hash(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()


class TenantRunner:
    """Reconcile running guard threads against the tenants table."""

    def __init__(self, store: SupabaseStore, guard=guard_tenant):
        self.store = store
        self.guard = guard
        # tenant id -> (thread, stop_event, row_hash)
        self.running: dict[str, tuple[threading.Thread, threading.Event, str]] = {}

    def reconcile(self) -> None:
        rows = {r["id"]: r for r in self.store.fetch_tenants()}

        for tid in list(self.running):
            thread, stop, row_hash = self.running[tid]
            gone = tid not in rows
            changed = not gone and _row_hash(rows[tid]) != row_hash
            if gone or changed or not thread.is_alive():
                stop.set()
                del self.running[tid]
                if changed:
                    print(f"tenant {tid}: settings changed, restarting", flush=True)
                elif gone:
                    print(f"tenant {tid}: deactivated, stopping", flush=True)

        for tid, row in rows.items():
            if tid in self.running:
                continue
            stop = threading.Event()
            thread = threading.Thread(target=self._run, args=(row, stop),
                                      daemon=True, name=f"tenant-{tid[:8]}")
            thread.start()
            self.running[tid] = (thread, stop, _row_hash(row))
            print(f"tenant {tid}: guard started", flush=True)

    def _run(self, row: dict, stop: threading.Event) -> None:
        try:
            self.guard(row, self.store, stop)
        except Exception as exc:  # a broken tenant must not kill the worker
            print(f"tenant {row.get('id')}: guard crashed: {exc}",
                  file=sys.stderr, flush=True)
            try:
                SupabaseAlerter(self.store, row["id"]).send(
                    f"🛑 Guardian stopped with an error: {exc}. "
                    "Check your API key and settings, then save them again to restart.")
            except Exception:
                pass

    def loop(self, interval: float = 60.0) -> None:
        print("Multi-tenant worker started.", flush=True)
        while True:
            try:
                self.reconcile()
            except Exception as exc:
                print(f"reconcile failed: {exc}", file=sys.stderr, flush=True)
            time.sleep(interval)
