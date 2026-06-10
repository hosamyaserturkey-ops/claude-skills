"""CLI entry point.

Propr mode (recommended):   python -m guardian --all
                            (reads PROPR_API_KEY from the environment or a
                            .env file; guards every active account)
Single account:             python -m guardian --preset 1step --account 2
Hyperliquid wallet mode:    python -m guardian --address 0x... --balance 10000
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import replace
from pathlib import Path

from .alerts import ConsoleAlerter, DiscordAlerter, TelegramAlerter
from .config import ChallengeConfig, make_preset
from .hyperliquid import MAINNET_URL, TESTNET_URL, HyperliquidInfoClient
from .monitor import run_monitor, run_parallel
from .propr import DEFAULT_URL as PROPR_URL, ProprClient, format_accounts


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load KEY=VALUE lines from a .env file without overriding real env vars."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="guardian",
        description="Monitor Propr challenge accounts and alert before "
                    "daily-loss or drawdown breaches.",
    )
    p.add_argument("--api-key", default=os.environ.get("PROPR_API_KEY"),
                   help="Propr API key from app.propr.xyz/settings "
                        "(or env PROPR_API_KEY). Enables Propr mode.")
    p.add_argument("--builder-id", default=os.environ.get("PROPR_BUILDER_ID"),
                   help="Optional Propr builder code (or env PROPR_BUILDER_ID).")
    p.add_argument("--propr-url", default=os.environ.get("PROPR_API_URL", PROPR_URL),
                   help="Propr API base URL (or env PROPR_API_URL). "
                        "Point at the beta environment to test.")
    p.add_argument("--address", default=os.environ.get("GUARDIAN_ADDRESS"),
                   help="Hyperliquid wallet address — only for accounts trading "
                        "directly on Hyperliquid (or env GUARDIAN_ADDRESS).")
    p.add_argument("--balance", type=float,
                   default=float(os.environ.get("GUARDIAN_BALANCE", 0) or 0),
                   help="Challenge starting balance in USDC. In Propr mode this "
                        "is auto-detected when possible.")
    p.add_argument("--preset", default=os.environ.get("GUARDIAN_PRESET", "1step"),
                   choices=["1step", "2step-1", "2step-2", "funded"],
                   help="Propr challenge type (default: 1step).")
    p.add_argument("--max-daily-loss", type=float, default=None,
                   help="Override max daily loss as a fraction, e.g. 0.03.")
    p.add_argument("--max-drawdown", type=float, default=None,
                   help="Override max drawdown as a fraction, e.g. 0.06.")
    p.add_argument("--profit-target", type=float, default=None,
                   help="Override profit target as a fraction, e.g. 0.10.")
    p.add_argument("--trailing-mode", default=None, choices=["relative", "absolute"],
                   help="Trailing drawdown formula (see guardian/config.py).")
    p.add_argument("--warn-levels", type=float, nargs="+", default=None,
                   help="Loss-budget fractions that trigger warnings (default: 0.7 0.9).")
    p.add_argument("--poll-interval", type=float,
                   default=float(os.environ.get("GUARDIAN_POLL_INTERVAL", 10)),
                   help="Seconds between equity checks (default: 10).")
    p.add_argument("--state-file", default=None,
                   help="Path for persisted state (default: ./state/<account>.json).")
    p.add_argument("--testnet", action="store_true",
                   help="Hyperliquid mode only: use the testnet.")
    p.add_argument("--once", action="store_true",
                   help="Do a single check and exit (useful for cron or smoke tests).")
    p.add_argument("--probe", action="store_true",
                   help="Propr mode only: dump raw API responses for debugging and exit.")
    p.add_argument("--list-accounts", action="store_true",
                   help="Propr mode only: list your active accounts and exit.")
    p.add_argument("--account", default=os.environ.get("GUARDIAN_ACCOUNT"),
                   help="Which account to guard when you have several: a number "
                        "from --list-accounts (1, 2, ...) or part of the account id.")
    p.add_argument("--no-trade-alerts", action="store_true",
                   help="Don't send a notification when a position opens or closes.")
    p.add_argument("--all", action="store_true",
                   default=os.environ.get("GUARDIAN_ALL", "").lower() in ("1", "true", "yes"),
                   help="Propr mode only: guard every active account in one process "
                        "(or env GUARDIAN_ALL=true). Ideal for hosted deployments.")
    return p


def build_config(args: argparse.Namespace, balance: float, detected: dict) -> ChallengeConfig:
    cfg = make_preset(args.preset, balance)
    # Precedence: CLI flag > value detected from the Propr API > preset default.
    detected_overrides = {
        k: v for k, v in detected.items()
        if k in ("max_daily_loss_pct", "max_drawdown_pct")
    }
    cli_overrides = {}
    if args.max_daily_loss is not None:
        cli_overrides["max_daily_loss_pct"] = args.max_daily_loss
    if args.max_drawdown is not None:
        cli_overrides["max_drawdown_pct"] = args.max_drawdown
    if args.profit_target is not None:
        cli_overrides["profit_target_pct"] = args.profit_target
    if args.trailing_mode is not None:
        cli_overrides["trailing_mode"] = args.trailing_mode
    if args.warn_levels is not None:
        cli_overrides["warn_levels"] = tuple(sorted(args.warn_levels))
    overrides = {**detected_overrides, **cli_overrides}
    if detected_overrides:
        print(f"Using limits from the Propr API: {detected_overrides}", flush=True)
    return replace(cfg, **overrides) if overrides else cfg


def build_alerters() -> list:
    alerters: list = [ConsoleAlerter()]
    discord_url = os.environ.get("GUARDIAN_DISCORD_WEBHOOK")
    if discord_url:
        alerters.append(DiscordAlerter(discord_url))
        print("Discord alerts enabled.", flush=True)
    tg_token = os.environ.get("GUARDIAN_TELEGRAM_TOKEN")
    tg_chat = os.environ.get("GUARDIAN_TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        alerters.append(TelegramAlerter(tg_token, tg_chat))
        print("Telegram alerts enabled.", flush=True)
    return alerters


def _state_path(args: argparse.Namespace, key: str) -> Path:
    if args.state_file:
        return Path(args.state_file)
    return Path("state") / f"{re.sub(r'[^A-Za-z0-9._-]', '_', key)}.json"


def _propr_job(args: argparse.Namespace, account: dict, alerters: list):
    """Build a no-arg monitor job for one Propr account."""
    client = ProprClient(args.api_key, base_url=args.propr_url,
                         builder_id=args.builder_id)
    client.select(account)
    detected = client.detect_challenge_config()
    balance = args.balance or detected.get("starting_balance", 0)
    if balance <= 0:
        raise SystemExit(
            f"Could not auto-detect the starting balance for {client.account_id} — "
            "pass it with --balance, e.g. --balance 10000."
        )
    cfg = build_config(args, balance, detected)
    state_path = _state_path(args, client.account_id)
    return lambda: run_monitor(
        cfg, client, alerters, state_path,
        poll_interval=args.poll_interval,
        max_iterations=1 if args.once else None,
        trade_alerts=not args.no_trade_alerts,
    )


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)

    if args.api_key:
        client = ProprClient(args.api_key, base_url=args.propr_url,
                             builder_id=args.builder_id)
        if args.probe:
            client.probe()
            return 0
        accounts = client.list_accounts()
        if args.list_accounts:
            if not accounts:
                print("No active accounts found.", flush=True)
            else:
                print(f"Active accounts ({len(accounts)}):\n{format_accounts(accounts)}\n"
                      "Guard one with --account <number>, or all with --all.", flush=True)
            return 0
        if not accounts:
            print("No active challenge or funded account found on this Propr "
                  "account. Purchase a challenge at https://app.propr.xyz/dashboard "
                  "first.", file=sys.stderr)
            return 1

        alerters = build_alerters()
        if args.all:
            print(f"Guarding all {len(accounts)} active account(s):\n"
                  f"{format_accounts(accounts)}", flush=True)
            jobs = [_propr_job(args, acc, alerters) for acc in accounts]
            return run_parallel(jobs)

        client.discover(args.account)
        print(f"Found {client.kind} account: {client.account_id}", flush=True)
        if len(accounts) > 1 and not args.account:
            print(f"⚠️  You have {len(accounts) - 1} other active account(s) NOT "
                  f"being guarded:\n{format_accounts(accounts)}\n"
                  "Use --all to guard everything, or --account <number> per window.",
                  flush=True)
        job = _propr_job(args, {
            "kind": client.kind, "account_id": client.account_id,
            "record_id": client.record_id, "record": client.record,
        }, alerters)
        return job()

    if args.address:
        if args.balance <= 0:
            print("--balance (or GUARDIAN_BALANCE) must be a positive starting "
                  "balance in wallet mode.", file=sys.stderr)
            return 1
        client = HyperliquidInfoClient(
            args.address,
            base_url=TESTNET_URL if args.testnet else MAINNET_URL,
        )
        cfg = build_config(args, args.balance, {})
        return run_monitor(
            cfg, client, build_alerters(), _state_path(args, args.address.lower()),
            poll_interval=args.poll_interval,
            max_iterations=1 if args.once else None,
            trade_alerts=not args.no_trade_alerts,
        )

    print("Provide a Propr API key (--api-key or env PROPR_API_KEY) — or, for "
          "accounts trading directly on Hyperliquid, a wallet --address.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
