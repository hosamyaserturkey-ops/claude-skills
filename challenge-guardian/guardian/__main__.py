"""CLI entry point.

Propr mode (recommended):   python -m guardian --preset 1step
                            (reads PROPR_API_KEY from the environment;
                            balance is auto-detected when possible)
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
from .monitor import run_monitor
from .propr import DEFAULT_URL as PROPR_URL, ProprClient, format_accounts


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="guardian",
        description="Monitor a Propr challenge account and alert before "
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
                        "from --list-accounts (1, 2, ...) or part of the account id. "
                        "Run one guardian per account, each with its own --account.")
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    detected: dict = {}
    if args.api_key:
        client = ProprClient(args.api_key, base_url=args.propr_url,
                             builder_id=args.builder_id)
        if args.probe:
            client.probe()
            return 0
        if args.list_accounts:
            accounts = client.list_accounts()
            if not accounts:
                print("No active accounts found.", flush=True)
            else:
                print(f"Active accounts ({len(accounts)}):\n{format_accounts(accounts)}\n"
                      "Guard one with: python -m guardian --account <number>", flush=True)
            return 0
        client.discover(args.account)
        print(f"Found {client.kind} account: {client.account_id}", flush=True)
        if len(client.accounts) > 1 and not args.account:
            others = len(client.accounts) - 1
            print(f"⚠️  You have {others} other active account(s) NOT being guarded:\n"
                  f"{format_accounts(client.accounts)}\n"
                  "Open another window and run the guardian with --account <number> "
                  "for each one.", flush=True)
        detected = client.detect_challenge_config()
        balance = args.balance or detected.get("starting_balance", 0)
        if balance <= 0:
            print("Could not auto-detect the starting balance — pass it with "
                  "--balance, e.g. --balance 10000.", file=sys.stderr)
            return 1
        state_key = re.sub(r"[^A-Za-z0-9._-]", "_", client.account_id)
    elif args.address:
        if args.balance <= 0:
            print("--balance (or GUARDIAN_BALANCE) must be a positive starting "
                  "balance in wallet mode.", file=sys.stderr)
            return 1
        client = HyperliquidInfoClient(
            args.address,
            base_url=TESTNET_URL if args.testnet else MAINNET_URL,
        )
        balance = args.balance
        state_key = args.address.lower()
    else:
        print("Provide a Propr API key (--api-key or env PROPR_API_KEY) — or, for "
              "accounts trading directly on Hyperliquid, a wallet --address.",
              file=sys.stderr)
        return 1

    cfg = build_config(args, balance, detected)
    state_path = Path(args.state_file) if args.state_file else Path("state") / f"{state_key}.json"
    return run_monitor(
        cfg,
        client,
        build_alerters(),
        state_path,
        poll_interval=args.poll_interval,
        max_iterations=1 if args.once else None,
    )


if __name__ == "__main__":
    sys.exit(main())
