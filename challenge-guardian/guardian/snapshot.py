"""Shared account snapshot type returned by all data-source clients."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AccountSnapshot:
    equity: float                 # account value including unrealized PnL
    withdrawable: float = 0.0
    open_positions: list = field(default_factory=list)
    # Server-declared lifecycle, when the data source knows it (Propr does):
    # e.g. "active", "passed", "failed", "closed", "review_pending".
    server_status: str | None = None
    server_reason: str | None = None   # failureReason / closureReason
    # Server-tracked highest balance, used to harden trailing-drawdown peaks.
    high_water_mark: float | None = None
