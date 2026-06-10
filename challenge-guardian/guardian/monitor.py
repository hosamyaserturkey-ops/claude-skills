"""Polling loop: fetch equity, evaluate rules, dispatch alerts, persist state."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

from .alerts import dispatch
from .config import ChallengeConfig
from .rules import BREACH, PASSED, RuleEvent, daily_loss_floor, drawdown_floor, evaluate
from .state import load_state, save_state


def run_monitor(
    cfg: ChallengeConfig,
    client,
    alerters: list,
    state_path: Path,
    poll_interval: float = 10.0,
    status_every: int = 60,
    max_iterations: int | None = None,
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

    consecutive_failures = 0
    iteration = 0
    while True:
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
        events.extend(_server_verdict(snapshot, state))
        for event in events:
            dispatch(alerters, event, label)
        save_state(state_path, state)

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
        time.sleep(poll_interval)


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
