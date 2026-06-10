"""Minimal read-only client for the public Hyperliquid Info API.

Propr runs on Hyperliquid, so a trader's challenge account equity (including
floating PnL) is readable for any wallet address with no authentication:
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

MAINNET_URL = "https://api.hyperliquid.xyz/info"
TESTNET_URL = "https://api.hyperliquid-testnet.xyz/info"


@dataclass
class AccountSnapshot:
    equity: float                 # accountValue: margin balance incl. unrealized PnL
    withdrawable: float
    open_positions: list[dict]    # raw assetPosition entries


class HyperliquidInfoClient:
    def __init__(self, address: str, base_url: str = MAINNET_URL, timeout: float = 10.0):
        if not address.startswith("0x") or len(address) != 42:
            raise ValueError(f"'{address}' does not look like a wallet address (0x + 40 hex chars)")
        self.address = address
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()

    def fetch_snapshot(self) -> AccountSnapshot:
        resp = self.session.post(
            self.base_url,
            json={"type": "clearinghouseState", "user": self.address},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        margin = data.get("marginSummary") or {}
        return AccountSnapshot(
            equity=float(margin.get("accountValue", 0.0)),
            withdrawable=float(data.get("withdrawable", 0.0)),
            open_positions=data.get("assetPositions", []),
        )
