"""Pure rule engine: given current equity and tracked state, emit rule events.

No network or I/O here so the breach math is unit-testable in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import ChallengeConfig


# Event severities, in escalating order.
INFO = "INFO"
WARNING = "WARNING"
CRITICAL = "CRITICAL"
BREACH = "BREACH"
PASSED = "PASSED"


@dataclass
class RuleEvent:
    severity: str
    rule: str          # "daily_loss" | "drawdown" | "profit_target"
    message: str
    equity: float
    limit: float       # the equity floor (or target) for this rule
    headroom: float    # equity - limit (negative means breached)


@dataclass
class TrackerState:
    """Mutable monitoring state, persisted across restarts."""
    day_key: str = ""               # UTC date string of the current trading day
    day_start_equity: float = 0.0   # equity at the daily reset
    peak_equity: float = 0.0        # highest equity ever seen (incl. floating PnL)
    # Highest warn level already alerted per rule for the current period,
    # so the same warning is not re-sent every poll. Daily-loss entries reset
    # at the day rollover; drawdown entries re-arm when equity recovers.
    alerted: dict[str, float] = field(default_factory=dict)
    breached: bool = False
    passed: bool = False
    # Position keys seen on the last poll, for trade open/close notifications.
    open_position_ids: list[str] = field(default_factory=list)
    # UTC date the last daily digest was sent, so restarts don't resend it.
    last_digest_day: str = ""


def _utc_day_key(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


def daily_loss_floor(cfg: ChallengeConfig, state: TrackerState) -> float:
    """Daily loss limit is a fixed % of the starting balance, measured from
    the equity at the start of the trading day."""
    return state.day_start_equity - cfg.starting_balance * cfg.max_daily_loss_pct


def drawdown_floor(cfg: ChallengeConfig, state: TrackerState) -> float:
    if not cfg.trailing_drawdown:
        return cfg.starting_balance * (1 - cfg.max_drawdown_pct)
    if cfg.trailing_mode == "relative":
        return state.peak_equity * (1 - cfg.max_drawdown_pct)
    return state.peak_equity - cfg.starting_balance * cfg.max_drawdown_pct


def profit_target_equity(cfg: ChallengeConfig) -> float | None:
    if cfg.profit_target_pct is None:
        return None
    return cfg.starting_balance * (1 + cfg.profit_target_pct)


def evaluate(
    cfg: ChallengeConfig,
    state: TrackerState,
    equity: float,
    now: datetime | None = None,
) -> list[RuleEvent]:
    """Update state with a new equity reading and return events to alert on.

    Mutates `state` (day rollover, peak tracking, alert de-duplication).
    """
    now = now or datetime.now(timezone.utc)
    events: list[RuleEvent] = []

    # Day rollover (UTC): reset the daily-loss anchor and its sent warnings.
    day = _utc_day_key(now)
    if day != state.day_key:
        state.day_key = day
        state.day_start_equity = equity
        for key in [k for k in state.alerted if k.startswith("daily_loss")]:
            del state.alerted[key]

    state.peak_equity = max(state.peak_equity, equity)

    if state.breached:
        return events  # breaches are permanent; nothing more to say

    for rule, floor in (
        ("daily_loss", daily_loss_floor(cfg, state)),
        ("drawdown", drawdown_floor(cfg, state)),
    ):
        events.extend(_check_floor(cfg, state, rule, equity, floor))

    target = profit_target_equity(cfg)
    if target is not None and not state.passed and equity >= target:
        state.passed = True
        events.append(RuleEvent(
            severity=PASSED,
            rule="profit_target",
            message=(
                f"Profit target hit! Equity ${equity:,.2f} >= target ${target:,.2f}. "
                "Consider flattening to lock it in."
            ),
            equity=equity,
            limit=target,
            headroom=equity - target,
        ))

    return events


def _check_floor(
    cfg: ChallengeConfig,
    state: TrackerState,
    rule: str,
    equity: float,
    floor: float,
) -> list[RuleEvent]:
    events: list[RuleEvent] = []
    headroom = equity - floor

    if equity <= floor:
        state.breached = True
        events.append(RuleEvent(
            severity=BREACH,
            rule=rule,
            message=(
                f"{_rule_label(rule)} BREACHED: equity ${equity:,.2f} touched the "
                f"floor ${floor:,.2f}. Propr breaches are permanent."
            ),
            equity=equity,
            limit=floor,
            headroom=headroom,
        ))
        return events

    # Budget = distance from the period anchor down to the floor.
    anchor = state.day_start_equity if rule == "daily_loss" else _dd_anchor(cfg, state)
    budget = anchor - floor
    if budget <= 0:
        return events
    consumed = (anchor - equity) / budget

    # Re-arm warnings once equity recovers below the lowest warn level.
    if consumed < min(cfg.warn_levels) and rule in state.alerted:
        del state.alerted[rule]
        return events

    crossed = [lvl for lvl in cfg.warn_levels if consumed >= lvl]
    if not crossed:
        return events
    level = max(crossed)
    if state.alerted.get(rule, 0.0) >= level:
        return events
    state.alerted[rule] = level

    severity = CRITICAL if level >= max(cfg.warn_levels) else WARNING
    events.append(RuleEvent(
        severity=severity,
        rule=rule,
        message=(
            f"{_rule_label(rule)}: {consumed:.0%} of the loss budget is used. "
            f"Equity ${equity:,.2f}, breach floor ${floor:,.2f} "
            f"(${headroom:,.2f} of headroom left)."
        ),
        equity=equity,
        limit=floor,
        headroom=headroom,
    ))
    return events


def budget_consumed(cfg: ChallengeConfig, state: TrackerState, equity: float) -> dict[str, float]:
    """Fraction of each rule's loss budget consumed at this equity (0..1+)."""
    out: dict[str, float] = {}
    for rule, floor in (
        ("daily_loss", daily_loss_floor(cfg, state)),
        ("drawdown", drawdown_floor(cfg, state)),
    ):
        anchor = state.day_start_equity if rule == "daily_loss" else _dd_anchor(cfg, state)
        budget = anchor - floor
        out[rule] = (anchor - equity) / budget if budget > 0 else 0.0
    return out


def _dd_anchor(cfg: ChallengeConfig, state: TrackerState) -> float:
    return state.peak_equity if cfg.trailing_drawdown else cfg.starting_balance


def _rule_label(rule: str) -> str:
    return {"daily_loss": "Max daily loss", "drawdown": "Max drawdown"}[rule]
