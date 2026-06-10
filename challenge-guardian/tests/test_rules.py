"""Unit tests for the breach math — the part that must never be wrong."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from guardian.config import ChallengeConfig, make_preset
from guardian.rules import (
    BREACH,
    CRITICAL,
    PASSED,
    WARNING,
    TrackerState,
    daily_loss_floor,
    drawdown_floor,
    evaluate,
)

T0 = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)


def one_step(balance: float = 10_000.0) -> ChallengeConfig:
    return make_preset("1step", balance)


def two_step(balance: float = 10_000.0) -> ChallengeConfig:
    return make_preset("2step-1", balance)


def fresh_state(cfg: ChallengeConfig, now: datetime = T0) -> TrackerState:
    state = TrackerState()
    evaluate(cfg, state, cfg.starting_balance, now)  # seed day anchor & peak
    return state


def test_floors_for_1step():
    cfg = one_step()
    state = fresh_state(cfg)
    assert daily_loss_floor(cfg, state) == pytest.approx(9_700.0)   # 3% of 10k
    assert drawdown_floor(cfg, state) == pytest.approx(9_400.0)     # 6% static


def test_no_events_with_healthy_equity():
    cfg = one_step()
    state = fresh_state(cfg)
    assert evaluate(cfg, state, 9_950.0, T0) == []


def test_warning_then_critical_then_breach_daily_loss():
    cfg = one_step()
    state = fresh_state(cfg)

    # 70% of the 300 USDC daily budget consumed -> equity 9790
    events = evaluate(cfg, state, 9_790.0, T0)
    assert [e.severity for e in events] == [WARNING]
    assert events[0].rule == "daily_loss"

    # Same level again: deduplicated.
    assert evaluate(cfg, state, 9_789.0, T0) == []

    # 90% consumed -> equity 9730 -> CRITICAL
    events = evaluate(cfg, state, 9_730.0, T0)
    assert [e.severity for e in events] == [CRITICAL]

    # Touch the floor -> BREACH, and breaches are permanent.
    events = evaluate(cfg, state, 9_700.0, T0)
    assert BREACH in [e.severity for e in events]
    assert state.breached
    assert evaluate(cfg, state, 10_500.0, T0) == []


def test_floating_pnl_breach_uses_equity_not_closed_balance():
    # Propr breaches trigger on equity incl. floating PnL; the engine only
    # ever sees equity, so a drop below the floor breaches immediately.
    cfg = one_step()
    state = fresh_state(cfg)
    events = evaluate(cfg, state, 9_399.0, T0)
    assert {e.rule for e in events if e.severity == BREACH} == {"daily_loss", "drawdown"}


def test_warning_rearms_after_recovery():
    cfg = one_step()
    state = fresh_state(cfg)
    assert [e.severity for e in evaluate(cfg, state, 9_790.0, T0)] == [WARNING]
    # Recover well above the lowest warn level, then dip again -> warn again.
    assert evaluate(cfg, state, 9_990.0, T0) == []
    assert [e.severity for e in evaluate(cfg, state, 9_790.0, T0)] == [WARNING]


def test_daily_reset_moves_anchor_and_rearms():
    cfg = one_step()
    state = fresh_state(cfg)
    evaluate(cfg, state, 9_790.0, T0)  # warning sent today
    next_day = T0 + timedelta(days=1)

    # New day anchored at 9,790: floor = 9,790 - 300 = 9,490.
    evaluate(cfg, state, 9_790.0, next_day)
    assert state.day_start_equity == pytest.approx(9_790.0)
    assert daily_loss_floor(cfg, state) == pytest.approx(9_490.0)

    # 70% of the fresh budget -> 9,580 -> daily warning fires again despite
    # yesterday's alert (the static drawdown rule also warns at this level).
    events = evaluate(cfg, state, 9_580.0, next_day)
    daily = [e for e in events if e.rule == "daily_loss"]
    assert [e.severity for e in daily] == [WARNING]


def test_trailing_drawdown_follows_peak():
    cfg = two_step()
    state = fresh_state(cfg)
    assert drawdown_floor(cfg, state) == pytest.approx(9_200.0)

    evaluate(cfg, state, 11_000.0, T0)  # new peak (incl. floating PnL)
    assert state.peak_equity == pytest.approx(11_000.0)
    assert drawdown_floor(cfg, state) == pytest.approx(11_000.0 * 0.92)

    # Falling back below the trailed floor breaches even though equity > start.
    events = evaluate(cfg, state, 10_100.0, T0)
    assert any(e.severity == BREACH and e.rule == "drawdown" for e in events)


def test_trailing_absolute_mode():
    cfg = ChallengeConfig(
        name="abs", starting_balance=10_000.0, max_daily_loss_pct=0.05,
        max_drawdown_pct=0.08, trailing_drawdown=True, profit_target_pct=None,
        trailing_mode="absolute",
    )
    state = fresh_state(cfg)
    evaluate(cfg, state, 11_000.0, T0)
    assert drawdown_floor(cfg, state) == pytest.approx(11_000.0 - 800.0)


def test_profit_target_fires_once():
    cfg = two_step()  # step 1 target: 5% -> 10,500
    state = fresh_state(cfg)
    events = evaluate(cfg, state, 10_500.0, T0)
    assert [e.severity for e in events] == [PASSED]
    assert evaluate(cfg, state, 10_600.0, T0) == []


def test_funded_preset_has_no_target():
    cfg = make_preset("funded", 10_000.0)
    state = fresh_state(cfg)
    assert evaluate(cfg, state, 50_000.0, T0) == []


def test_config_validation():
    with pytest.raises(ValueError):
        make_preset("1step", -5.0)
    with pytest.raises(ValueError):
        make_preset("nope", 10_000.0)
