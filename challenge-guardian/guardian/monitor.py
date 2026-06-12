"""Polling loop: fetch equity, evaluate rules, dispatch alerts, persist state."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

from datetime import datetime, timezone

from .alerts import dispatch
from .config import ChallengeConfig
from .digest import build_digest
from .rules import (
    BREACH,
    INFO,
    PASSED,
    RuleEvent,
    budget_consumed,
    daily_loss_floor,
    drawdown_floor,
    evaluate,
)
from .state import load_state, save_state


def run_monitor(
    cfg: ChallengeConfig,
    client,
    alerters: list,
    state_path: Path,
    poll_interval: float = 10.0,
    status_every: int = 60,
    max_iterations: int | None = None,
    trade_alerts: bool = True,
    status_board=None,
    auto_flatten_at: float | None = None,
    digest_hour: int | None = None,
    nudge=None,
    stop_event=None,
) -> int:
    """Run the monitoring loop. Returns an exit code (0 = passed/stopped, 2 = breached)."""
    state = load_state(state_path)
    label = f"{cfg.name} ({client.label})"
    print(f"Guarding {label}: starting balance ${cfg.starting_balance:,.2f}, "
          f"daily loss {cfg.max_daily_loss_pct:.0%}, "
          f"drawdown {cfg.max_drawdown_pct:.0%} "
          f"({'trailing' if cfg.trailing_drawdown else 'static'}), "
          f"poll every {poll_interval:.0f}s", flush=True)
    if state.breached:
        print("State file says this account already breached. Nothing to guard.", flush=True)
        return 2
    if auto_flatten_at:
        print(f"Auto-flatten armed at {auto_flatten_at:.0%} of a loss budget.", flush=True)

    consecutive_failures = 0
    iteration = 0
    last_flatten_attempt = 0.0
    while True:
        if stop_event is not None and stop_event.is_set():
            print(f"{label}: stop requested, exiting monitor.", flush=True)
            return 0
        iteration += 1
        try:
            snapshot = client.fetch_snapshot()
            consecutive_failures = 0
        except (requests.RequestException, ValueError) as exc:
            consecutive_failures += 1
            print(f"Fetch failed ({consecutive_failures}): {exc}", file=sys.stderr, flush=True)
            if consecutive_failures >= 10:
                dispatch_all(alerters, label,
                             "🛑 Guardian lost contact with the API "
                             "(10 consecutive failures). Equity is NOT being monitored.")
                consecutive_failures = 0
            if max_iterations is not None and iteration >= max_iterations:
                return 1
            time.sleep(min(poll_interval * consecutive_failures, 60) or poll_interval)
            continue

        # A server-tracked high-water mark can only tighten a trailing floor,
        # never loosen it, so merge it into the local peak before evaluating.
        if snapshot.high_water_mark is not None:
            state.peak_equity = max(state.peak_equity, snapshot.high_water_mark)

        events = evaluate(cfg, state, snapshot.equity)
        events.extend(_position_events(cfg, state, snapshot, trade_alerts))
        events.extend(_server_verdict(snapshot, state))
        for event in events:
            dispatch(alerters, event, label)

        # Auto-flatten: when armed, close everything as the last line of
        # defense before a breach. Local floors fire BEFORE Propr's engine
        # does, so acting here can still save the account.
        if (auto_flatten_at and not state.breached and snapshot.open_positions
                and hasattr(client, "flatten_positions")):
            worst = max(budget_consumed(cfg, state, snapshot.equity).values())
            if worst >= auto_flatten_at and time.time() - last_flatten_attempt >= 60:
                last_flatten_attempt = time.time()
                _auto_flatten(client, alerters, label, worst)

        _maybe_send_digest(cfg, state, snapshot, client, alerters, label, digest_hour)
        save_state(state_path, state)

        if status_board is not None:
            status_board.update(
                label,
                equity=snapshot.equity,
                daily_floor=daily_loss_floor(cfg, state),
                dd_floor=drawdown_floor(cfg, state),
                peak=state.peak_equity,
                positions=len(snapshot.open_positions),
            )

        if iteration % status_every == 1:
            dl_floor = daily_loss_floor(cfg, state)
            dd_floor = drawdown_floor(cfg, state)
            print(f"equity=${snapshot.equity:,.2f} "
                  f"daily_floor=${dl_floor:,.2f} dd_floor=${dd_floor:,.2f} "
                  f"peak=${state.peak_equity:,.2f} positions={len(snapshot.open_positions)}",
                  flush=True)

        if state.breached:
            print("Account breached. Stopping monitor.", flush=True)
            return 2
        if state.passed and snapshot.server_status in ("passed",):
            print("Challenge passed (server-confirmed). Stopping monitor.", flush=True)
            return 0
        if max_iterations is not None and iteration >= max_iterations:
            return 0
        if nudge is not None:
            # Sleep until the poll interval elapses OR the WebSocket reports
            # account activity — whichever comes first.
            nudge.wait(poll_interval)
            nudge.clear()
        else:
            time.sleep(poll_interval)


def _auto_flatten(client, alerters: list, label: str, worst: float) -> None:
    try:
        results = client.flatten_positions()
    except Exception as exc:
        dispatch_all(alerters, label,
                     f"🛑 AUTO-FLATTEN FAILED at {worst:.0%} of a loss budget: {exc}. "
                     "Close your positions manually NOW.")
        return
    ok = sum(1 for r in results if r["ok"])
    detail = "; ".join(f"{r['position']}: {r['detail']}" for r in results)
    text = (f"🛡️ AUTO-FLATTEN: {worst:.0%} of a loss budget used — "
            f"closed {ok}/{len(results)} position(s). {detail}")
    if ok < len(results):
        text += " — some closes FAILED, check the account immediately."
    dispatch_all(alerters, label, text)


def _maybe_send_digest(cfg, state, snapshot, client, alerters, label, digest_hour) -> None:
    if digest_hour is None or not hasattr(client, "fetch_trades"):
        return
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour < digest_hour or state.last_digest_day == today:
        return
    state.last_digest_day = today
    try:
        text = build_digest(client.fetch_trades(), cfg, state, snapshot.equity,
                            len(snapshot.open_positions), now)
    except Exception as exc:
        print(f"Digest failed: {exc}", file=sys.stderr, flush=True)
        return
    dispatch_all(alerters, label, text)


def _position_events(cfg, state, snapshot, trade_alerts: bool) -> list[RuleEvent]:
    """Notify when a position appears or disappears between polls."""
    current = {_position_key(p, i): p for i, p in enumerate(snapshot.open_positions)}
    previous = set(state.open_position_ids)
    state.open_position_ids = list(current)
    if not trade_alerts or not previous and not current:
        return []

    headroom = snapshot.equity - daily_loss_floor(cfg, state)
    suffix = (f"Equity ${snapshot.equity:,.2f}, "
              f"${headroom:,.2f} above today's loss floor.")
    events = []
    for key, p in current.items():
        if key not in previous:
            events.append(RuleEvent(
                severity=INFO, rule="position",
                message=f"📈 Trade opened: {_describe_position(p)}. {suffix}",
                equity=snapshot.equity, limit=0.0, headroom=headroom,
            ))
    for key in previous - set(current):
        events.append(RuleEvent(
            severity=INFO, rule="position",
            message=f"📉 Trade closed ({key}). {suffix}",
            equity=snapshot.equity, limit=0.0, headroom=headroom,
        ))
    return events


def _position_key(p: dict, index: int) -> str:
    # Propr positions carry positionId; Hyperliquid wallet mode nests a coin.
    return str(
        p.get("positionId")
        or (p.get("position") or {}).get("coin")
        or f"position-{index}"
    )


def _describe_position(p: dict) -> str:
    side = (p.get("positionSide") or "").upper()
    qty = p.get("quantity") or (p.get("position") or {}).get("szi") or "?"
    base = p.get("base") or (p.get("position") or {}).get("coin") or ""
    entry = p.get("entryPrice")
    desc = " ".join(s for s in (side, str(qty), base) if s) or "position"
    if entry:
        desc += f" @ {float(entry):,.2f}"
    return desc


def _server_verdict(snapshot, state) -> list[RuleEvent]:
    """Propr's risk engine is the source of truth: if the server says the
    account failed or passed, report that verdict even if local math missed it."""
    status = snapshot.server_status
    if not status or status == "active":
        return []
    verdict = _terminal(status)
    if verdict == "failed" and not state.breached:
        state.breached = True
        reason = snapshot.server_reason or "no reason given"
        return [RuleEvent(
            severity=BREACH, rule="server",
            message=f"Propr marked this account '{status}' (reason: {reason}).",
            equity=snapshot.equity, limit=0.0, headroom=0.0,
        )]
    if verdict == "passed" and not state.passed:
        state.passed = True
        return [RuleEvent(
            severity=PASSED, rule="server",
            message="Propr marked this challenge as PASSED. Congratulations!",
            equity=snapshot.equity, limit=0.0, headroom=0.0,
        )]
    return []


def _terminal(status: str) -> str | None:
    if status in ("failed", "closed"):
        return "failed"
    if status == "passed":
        return "passed"
    return None


def dispatch_all(alerters: list, label: str, text: str) -> None:
    for alerter in alerters:
        alerter.send(f"{label} — {text}")


def run_parallel(jobs: list) -> int:
    """Run several monitor jobs (no-arg callables returning an exit code)
    concurrently, one thread each. Returns the worst exit code."""
    import threading

    codes: dict[int, int] = {}

    def runner(index: int, job) -> None:
        try:
            codes[index] = job()
        except Exception as exc:  # one account failing must not kill the rest
            print(f"Monitor {index} crashed: {exc}", file=sys.stderr, flush=True)
            codes[index] = 1

    threads = [
        threading.Thread(target=runner, args=(i, job), daemon=True)
        for i, job in enumerate(jobs)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return max(codes.values(), default=0)
