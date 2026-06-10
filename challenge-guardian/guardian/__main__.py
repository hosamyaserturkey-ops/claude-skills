"""CLI entry point: python -m guardian --address 0x... --balance 10000 --preset 1step"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .alerts import ConsoleAlerter, DiscordAlerter, TelegramAlerter
from .config import ChallengeConfig, make_preset
from .hyperliquid import MAINNET_URL, TESTNET_URL, HyperliquidInfoClient
from .monitor import run_monitor


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="guardian",
        description="Monitor a Propr challenge account on Hyperliquid and alert "
                    "before daily-loss or drawdown breaches.",
    )
    p.add_argument("--address", default=os.environ.get("GUARDIAN_ADDRESS"),
                   help="Wallet address of the challenge account (or env GUARDIAN_ADDRESS).")
    p.add_argument("--balance", type=float,
                   default=float(os.environ.get("GUARDIAN_BALANCE", 0) or 0),
                   help="Challenge starting balance in USDC (or env GUARDIAN_BALANCE).")
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
                   help="Path for persisted state (default: ./state/<address>.json).")
    p.add_argument("--testnet", action="store_true", help="Use Hyperliquid testnet.")
    p.add_argument("--once", action="store_true",
                   help="Do a single check and exit (useful for cron or smoke tests).")
    return p


def build_config(args: argparse.Namespace) -> ChallengeConfig:
    base = make_preset(args.preset, args.balance)
    overrides = {}
    if args.max_daily_loss is not None:
        overrides["max_daily_loss_pct"] = args.max_daily_loss
    if args.max_drawdown is not None:
        overrides["max_drawdown_pct"] = args.max_drawdown
    if args.profit_target is not None:
        overrides["profit_target_pct"] = args.profit_target
    if args.trailing_mode is not None:
        overrides["trailing_mode"] = args.trailing_mode
    if args.warn_levels is not None:
        overrides["warn_levels"] = tuple(sorted(args.warn_levels))
    if not overrides:
        return base
    from dataclasses import replace
    return replace(base, **overrides)


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
    if not args.address:
        print("--address (or GUARDIAN_ADDRESS) is required.", file=sys.stderr)
        return 1
    if args.balance <= 0:
        print("--balance (or GUARDIAN_BALANCE) must be a positive starting balance.",
              file=sys.stderr)
        return 1

    cfg = build_config(args)
    client = HyperliquidInfoClient(
        args.address,
        base_url=TESTNET_URL if args.testnet else MAINNET_URL,
    )
    state_path = Path(args.state_file) if args.state_file else (
        Path("state") / f"{args.address.lower()}.json"
    )
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
