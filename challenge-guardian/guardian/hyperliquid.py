"""Minimal read-only client for the public Hyperliquid Info API.

Useful for accounts that trade directly on Hyperliquid under their own wallet
address. Propr challenge accounts are internal to Propr — use guardian.propr
for those. Docs:
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
"""

from __future__ import annotations

import requests

from .snapshot import AccountSnapshot

MAINNET_URL = "https://api.hyperliquid.xyz/info"
TESTNET_URL = "https://api.hyperliquid-testnet.xyz/info"


class HyperliquidInfoClient:
    def __init__(self, address: str, base_url: str = MAINNET_URL, timeout: float = 10.0):
        if not address.startswith("0x") or len(address) != 42:
            raise ValueError(f"'{address}' does not look like a wallet address (0x + 40 hex chars)")
        self.address = address
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()

    @property
    def label(self) -> str:
        return f"{self.address[:6]}…{self.address[-4:]}"

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
