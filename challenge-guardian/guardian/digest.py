"""Daily digest: summarize the day's trading from the Propr trade history."""

from __future__ import annotations

from datetime import datetime, timezone

from .config import ChallengeConfig
from .rules import TrackerState, budget_consumed, drawdown_floor, profit_target_equity

# Trade types that realize PnL (per the Propr docs).
_CLOSING_TYPES = {"reduce", "close", "flip", "liquidation"}


def _executed_today(trade: dict, now: datetime) -> bool:
    raw = trade.get("executedAt") or trade.get("createdAt") or ""
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return when.astimezone(timezone.utc).date() == now.date()


def build_digest(
    trades: list[dict],
    cfg: ChallengeConfig,
    state: TrackerState,
    equity: float,
    open_positions: int,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    todays = [t for t in trades if _executed_today(t, now)]
    closing = [t for t in todays if (t.get("type") or "") in _CLOSING_TYPES]

    realized = sum(float(t.get("realizedPnl") or 0) for t in closing)
    fees = sum(float(t.get("fee") or 0) for t in todays)
    wins = sum(1 for t in closing if float(t.get("realizedPnl") or 0) > 0)
    win_rate = f"{wins / len(closing):.0%}" if closing else "n/a"

    consumed = budget_consumed(cfg, state, equity)
    lines = [
        f"📊 Daily digest ({now.strftime('%Y-%m-%d')} UTC)",
        f"Equity: ${equity:,.2f} (day started at ${state.day_start_equity:,.2f})",
        f"Realized PnL today: {realized:+,.2f} USDC "
        f"({len(closing)} closing trade{'s' if len(closing) != 1 else ''}, win rate {win_rate})",
        f"Fees paid today: ${fees:,.2f} across {len(todays)} fill(s)",
        f"Daily loss budget used: {max(consumed['daily_loss'], 0):.0%} | "
        f"drawdown budget used: {max(consumed['drawdown'], 0):.0%} "
        f"(floor ${drawdown_floor(cfg, state):,.2f})",
        f"Open positions going into tomorrow: {open_positions}",
    ]
    target = profit_target_equity(cfg)
    if target is not None:
        if equity >= target:
            lines.append(f"Profit target ${target:,.2f}: REACHED 🏆")
        else:
            lines.append(f"Profit target ${target:,.2f}: ${target - equity:,.2f} to go")
    return "\n".join(lines)
