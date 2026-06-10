"""Polling loop: fetch equity, evaluate rules, dispatch alerts, persist state."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

from .alerts import dispatch
from .config import ChallengeConfig
from .hyperliquid import HyperliquidInfoClient
from .rules import daily_loss_floor, drawdown_floor, evaluate
from .state import load_state, save_state


def run_monitor(
    cfg: ChallengeConfig,
    client: HyperliquidInfoClient,
    alerters: list,
    state_path: Path,
    poll_interval: float = 10.0,
    status_every: int = 60,
    max_iterations: int | None = None,
) -> int:
    """Run the monitoring loop. Returns an exit code (0 = passed/stopped, 2 = breached)."""
    state = load_state(state_path)
    label = f"{cfg.name} ({client.address[:6]}…{client.address[-4:]})"
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
                             "🛑 Guardian lost contact with the Hyperliquid API "
                             "(10 consecutive failures). Equity is NOT being monitored.")
                consecutive_failures = 0
            if max_iterations is not None and iteration >= max_iterations:
                return 1
            time.sleep(min(poll_interval * consecutive_failures, 60) or poll_interval)
            continue

        events = evaluate(cfg, state, snapshot.equity)
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
        if max_iterations is not None and iteration >= max_iterations:
            return 0
        time.sleep(poll_interval)


def dispatch_all(alerters: list, label: str, text: str) -> None:
    for alerter in alerters:
        alerter.send(f"{label} — {text}")
