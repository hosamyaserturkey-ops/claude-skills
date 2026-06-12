"""Read-only-by-default client for the Propr API (https://www.propr.xyz/developers).

Auth is an API key in the X-API-Key header (one key per user, generated at
app.propr.xyz/settings). Reads:

  GET /users/me                          sanity-check the key
  GET /book-account-issuances            funded accounts (checked first)
  GET /challenge-attempts                paper accounts during evaluation
  GET /challenge-attempts/{id}           attempt status + failureReason
  GET /book-account-issuances/{id}       issuance status + closureReason
  GET /accounts/{accountId}              balance / unrealized PnL / high-water mark
  GET /accounts/{accountId}/positions    open positions
  GET /accounts/{accountId}/trades       execution history (daily digest)
  GET /challenges                        challenge config (balance, loss limits)

Writes — used ONLY by the opt-in action features (/close, /flatten,
auto-flatten); never called otherwise:

  POST /accounts/{accountId}/orders      reduce-only market orders that close
                                         existing positions

Equity formula per the official SDK docs:
  equity = balance + totalUnrealizedPnl + isolatedPositionMargin
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import requests

from .snapshot import AccountSnapshot

DEFAULT_URL = "https://api.propr.xyz/v1"
DEFAULT_WS_URL = "wss://api.propr.xyz/ws"

# Statuses that mean the account's story is over, as reported by Propr itself.
_FAILED_STATUSES = {"failed", "closed"}
_PASSED_STATUSES = {"passed"}


class ProprClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_URL,
        builder_id: str | None = None,
        timeout: float = 10.0,
    ):
        if not api_key:
            raise ValueError("A Propr API key is required (starts with pk_live_ or pk_test_)")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["X-API-Key"] = api_key
        if builder_id:
            self.session.headers["X-Builder-ID"] = builder_id
        self.account_id: str | None = None
        self.record_id: str | None = None   # attemptId or issuanceId
        self.kind: str | None = None        # "funded" or "challenge"
        self.record: dict = {}
        self.accounts: list[dict] = []      # all active accounts found by discover()

    @property
    def label(self) -> str:
        if not self.account_id:
            return "Propr"
        return f"Propr {self.kind} {self.account_id[-8:]}"

    def _get(self, path: str, params: dict | None = None) -> Any:
        resp = self.session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        if resp.status_code == 401:
            raise ValueError(
                "Propr rejected the API key (401). Check PROPR_API_KEY — "
                "regenerate it at https://app.propr.xyz/settings if needed."
            )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _items(payload: Any) -> list[dict]:
        if isinstance(payload, dict):
            return payload.get("data", []) or []
        return payload or []

    def list_accounts(self) -> list[dict]:
        """All active accounts on this user: funded first, then challenges."""
        accounts = []
        for item in self._items(self._get("/book-account-issuances", params={"status": "active"})):
            accounts.append({
                "kind": "funded",
                "account_id": item["accountId"],
                "record_id": _first(item, "issuanceId", "id"),
                "record": item,
            })
        for item in self._items(self._get("/challenge-attempts", params={"status": "active"})):
            accounts.append({
                "kind": "challenge",
                "account_id": item["accountId"],
                "record_id": _first(item, "attemptId", "id"),
                "record": item,
            })
        self.accounts = accounts
        return accounts

    def discover(self, selector: str | None = None) -> None:
        """Pick the account to guard. With no selector, take the first active
        account (funded before challenge, same order as Propr's quickstart).
        A selector is either a 1-based number from --list-accounts or any
        unique part of the accountId."""
        accounts = self.list_accounts()
        if not accounts:
            raise SystemExit(
                "No active challenge or funded account found on this Propr account. "
                "Purchase a challenge at https://app.propr.xyz/dashboard first."
            )
        self.select(_select_account(accounts, selector))

    def select(self, account: dict) -> None:
        """Bind this client to one of the accounts from list_accounts()."""
        self.kind = account["kind"]
        self.record = account["record"]
        self.record_id = account["record_id"]
        self.account_id = account["account_id"]

    def fetch_open_positions(self) -> list[dict]:
        positions = self._items(
            self._get(f"/accounts/{self.account_id}/positions", params={"status": "open"})
        )
        return [p for p in positions if float(p.get("quantity") or 0) != 0]

    def fetch_trades(self, limit: int = 100) -> list[dict]:
        return self._items(
            self._get(f"/accounts/{self.account_id}/trades", params={"limit": limit})
        )

    def fetch_snapshot(self) -> AccountSnapshot:
        if not self.account_id:
            self.discover()

        account = self._get(f"/accounts/{self.account_id}")
        if isinstance(account, dict) and isinstance(account.get("data"), dict):
            account = account["data"]
        balance = float(account.get("balance") or 0.0)
        unrealized = float(account.get("totalUnrealizedPnl") or 0.0)
        isolated = float(account.get("isolatedPositionMargin") or 0.0)
        equity = balance + unrealized + isolated
        hwm = account.get("highWaterMark")

        positions = self.fetch_open_positions()

        status, reason = self._lifecycle()
        return AccountSnapshot(
            equity=equity,
            withdrawable=float(account.get("availableBalance") or 0.0),
            open_positions=positions,
            server_status=status,
            server_reason=reason,
            high_water_mark=float(hwm) if hwm is not None else None,
        )

    def _lifecycle(self) -> tuple[str | None, str | None]:
        """Re-read the attempt/issuance so server-declared pass/fail is caught
        even if our local math never saw the breach."""
        if not self.record_id:
            return None, None
        path = (
            f"/challenge-attempts/{self.record_id}"
            if self.kind == "challenge"
            else f"/book-account-issuances/{self.record_id}"
        )
        try:
            record = self._get(path)
        except requests.RequestException:
            return None, None  # lifecycle is best-effort; equity checks still ran
        if isinstance(record, dict) and isinstance(record.get("data"), dict):
            record = record["data"]
        status = record.get("status")
        reason = _first(record, "failureReason", "closureReason")
        return status, reason

    def _post(self, path: str, payload: dict) -> Any:
        resp = self.session.post(f"{self.base_url}{path}", json=payload, timeout=self.timeout)
        if resp.status_code not in (200, 201):  # Propr returns 201 on create
            raise RuntimeError(f"POST {path} failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    def close_position(self, position: dict) -> dict:
        """Close one position with a reduce-only IOC market order.

        Per the Propr docs: reduceOnly prevents accidentally opening an
        opposing position, closePosition closes the full size, and IOC stops
        the market order from resting on the book."""
        side = "sell" if (position.get("positionSide") or "").lower() == "long" else "buy"
        order = {
            "accountId": self.account_id,
            "intentId": new_ulid(),
            "exchange": position.get("exchange", "hyperliquid"),
            "type": "market",
            "side": side,
            "positionSide": position.get("positionSide"),
            "productType": position.get("productType", "perp"),
            "timeInForce": "IOC",
            "asset": position.get("asset") or position.get("base"),
            "base": position.get("base"),
            "quote": position.get("quote", "USDC"),
            "quantity": str(position.get("quantity")),
            "reduceOnly": True,
            "closePosition": True,
        }
        result = self._post(f"/accounts/{self.account_id}/orders", {"orders": [order]})
        return (self._items(result) or [result])[0]

    def flatten_positions(self, base: str | None = None) -> list[dict]:
        """Close all open positions (optionally only for one asset).
        Returns one result dict per position: {'position', 'ok', 'detail'}."""
        results = []
        for position in self.fetch_open_positions():
            if base and (position.get("base") or "").upper() != base.upper():
                continue
            desc = (f"{(position.get('positionSide') or '?').upper()} "
                    f"{position.get('quantity')} {position.get('base')}")
            try:
                order = self.close_position(position)
                results.append({"position": desc, "ok": True,
                                "detail": order.get("status", "submitted")})
            except Exception as exc:  # keep closing the rest even if one fails
                results.append({"position": desc, "ok": False, "detail": str(exc)})
        return results

    @staticmethod
    def is_terminal(status: str) -> str | None:
        """Map a server status to 'failed'/'passed' if it ends the account."""
        if status in _FAILED_STATUSES:
            return "failed"
        if status in _PASSED_STATUSES:
            return "passed"
        return None

    def detect_challenge_config(self) -> dict:
        """Best-effort auto-detection of starting balance and loss limits from
        the challenge definition. Field names aren't in the public docs, so we
        probe likely keys and return only what we find."""
        out: dict = {}
        challenge_id = _first(self.record, "challengeId")
        if not challenge_id:
            return out
        try:
            challenges = self._items(self._get("/challenges", params={"challengeId": challenge_id}))
        except requests.RequestException:
            return out
        if not challenges:
            return out
        c = challenges[0]
        balance = _first(c, "initialBalance", "startingBalance", "accountSize")
        daily = _first(c, "maxDailyLoss", "maxDailyLossPercent", "maxDailyLossPct", "dailyLossLimit")
        drawdown = _first(c, "maxDrawdown", "maxDrawdownPercent", "maxDrawdownPct", "drawdownLimit")
        if balance is not None:
            out["starting_balance"] = float(balance)
        if daily is not None:
            out["max_daily_loss_pct"] = _as_fraction(float(daily))
        if drawdown is not None:
            out["max_drawdown_pct"] = _as_fraction(float(drawdown))
        return out

    def probe(self) -> None:
        """Dump raw API responses (for debugging field mismatches)."""
        for name, path, params in (
            ("users/me", "/users/me", None),
            ("book-account-issuances", "/book-account-issuances", None),
            ("challenge-attempts", "/challenge-attempts", None),
        ):
            try:
                print(f"--- {name} ---\n{json.dumps(self._get(path, params), indent=2)[:3000]}",
                      flush=True)
            except Exception as exc:  # probe must show every endpoint regardless
                print(f"--- {name} --- ERROR: {exc}", flush=True)
        if self.account_id is None:
            try:
                self.discover()
            except SystemExit as exc:
                print(exc, flush=True)
                return
        for name, path in (
            (f"accounts/{self.account_id}", f"/accounts/{self.account_id}"),
            ("positions", f"/accounts/{self.account_id}/positions"),
        ):
            try:
                print(f"--- {name} ---\n{json.dumps(self._get(path), indent=2)[:3000]}", flush=True)
            except Exception as exc:
                print(f"--- {name} --- ERROR: {exc}", flush=True)


def format_accounts(accounts: list[dict]) -> str:
    lines = []
    for i, acc in enumerate(accounts, start=1):
        status = acc["record"].get("status", "?")
        lines.append(f"  {i}. [{acc['kind']}] {acc['account_id']} (status: {status})")
    return "\n".join(lines)


def _select_account(accounts: list[dict], selector: str | None) -> dict:
    if selector is None:
        return accounts[0]
    selector = selector.strip()
    if selector.isdigit():
        n = int(selector)
        if 1 <= n <= len(accounts):
            return accounts[n - 1]
        raise SystemExit(
            f"--account {n} is out of range; there are {len(accounts)} active "
            f"account(s):\n{format_accounts(accounts)}"
        )
    matches = [a for a in accounts if selector.lower() in a["account_id"].lower()]
    if len(matches) == 1:
        return matches[0]
    problem = "matches no" if not matches else "matches more than one"
    raise SystemExit(
        f"--account '{selector}' {problem} active account. "
        f"Active accounts:\n{format_accounts(accounts)}"
    )


_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """26-char ULID (48-bit ms timestamp + 80 random bits, Crockford base32).
    Propr uses the intentId for idempotency: a unique one per order."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD32[(value >> (5 * i)) & 31] for i in range(25, -1, -1))


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def _as_fraction(value: float) -> float:
    """Normalize '3' or '0.03' to 0.03 — the API may report percent or fraction."""
    return value / 100 if value > 1 else value
