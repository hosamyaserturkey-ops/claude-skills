"""Challenge configuration and presets for Propr challenge types.

Numbers come from Propr's official rulebook (https://www.propr.xyz/rules):
  - 1-Step:  max daily loss 3%, max drawdown 6% (static)
  - 2-Step:  max daily loss 5%, max drawdown 8% (trailing on highest equity
             ever recorded, including floating PnL); Step 1 target 5%,
             Step 2 target 10%
  - Breaches include floating PnL and are permanent, so warnings must fire
    while positions are still open.

Always verify against the live rulebook before trusting an account to this
tool — Propr can change these numbers at any time, and every value here can
be overridden via CLI flags.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChallengeConfig:
    name: str
    starting_balance: float
    max_daily_loss_pct: float          # fraction of starting balance, e.g. 0.03
    max_drawdown_pct: float            # fraction, e.g. 0.06
    trailing_drawdown: bool            # False = static floor from starting balance
    profit_target_pct: float | None    # None for funded accounts (no target)
    # How the trailing floor is computed when trailing_drawdown is True:
    #   "relative": floor = peak_equity * (1 - max_drawdown_pct)
    #   "absolute": floor = peak_equity - starting_balance * max_drawdown_pct
    # Propr's rulebook says the trailing drawdown is calculated from the
    # highest equity ever recorded but does not spell out the formula
    # publicly; "relative" is the more conservative default (higher floor,
    # earlier warnings).
    trailing_mode: str = "relative"
    # Fractions of a limit's budget consumed at which to send warnings.
    warn_levels: tuple[float, ...] = (0.7, 0.9)

    def __post_init__(self) -> None:
        if self.starting_balance <= 0:
            raise ValueError("starting_balance must be positive")
        if not 0 < self.max_daily_loss_pct < 1:
            raise ValueError("max_daily_loss_pct must be a fraction between 0 and 1")
        if not 0 < self.max_drawdown_pct < 1:
            raise ValueError("max_drawdown_pct must be a fraction between 0 and 1")
        if self.trailing_mode not in ("relative", "absolute"):
            raise ValueError("trailing_mode must be 'relative' or 'absolute'")
        if self.profit_target_pct is not None and self.profit_target_pct <= 0:
            raise ValueError("profit_target_pct must be positive or None")


def make_preset(preset: str, starting_balance: float) -> ChallengeConfig:
    """Build a ChallengeConfig from a named Propr preset."""
    presets = {
        "1step": dict(
            name="Propr 1-Step",
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.06,
            trailing_drawdown=False,
            profit_target_pct=0.10,
        ),
        "2step-1": dict(
            name="Propr 2-Step (Step 1)",
            max_daily_loss_pct=0.05,
            max_drawdown_pct=0.08,
            trailing_drawdown=True,
            profit_target_pct=0.05,
        ),
        "2step-2": dict(
            name="Propr 2-Step (Step 2)",
            max_daily_loss_pct=0.05,
            max_drawdown_pct=0.08,
            trailing_drawdown=True,
            profit_target_pct=0.10,
        ),
        "funded": dict(
            name="Propr Funded",
            max_daily_loss_pct=0.03,
            max_drawdown_pct=0.06,
            trailing_drawdown=False,
            profit_target_pct=None,
        ),
    }
    if preset not in presets:
        raise ValueError(f"Unknown preset '{preset}'. Choose from: {', '.join(presets)}")
    return ChallengeConfig(starting_balance=starting_balance, **presets[preset])
