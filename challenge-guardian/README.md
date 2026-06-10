# Challenge Guardian

A risk-compliance bot for [Propr](https://www.propr.xyz/) challenge accounts.
It watches your account equity in real time through the official
[Propr API](https://www.propr.xyz/developers) and alerts you on
Discord/Telegram **before** you breach a challenge rule — because on Propr,
breaches include floating PnL and are permanent.

The bot is **read-only**: it never places, modifies, or cancels orders.

## Quick start

1. Get your API key: log in at [app.propr.xyz](https://app.propr.xyz/) →
   **Settings** → generate an API key (starts with `pk_live_`). Keep it secret.
2. Install and run:

```bash
cd challenge-guardian
pip install -r requirements.txt

# Optional alert channels:
export GUARDIAN_DISCORD_WEBHOOK="https://discord.com/api/webhooks/..."
export GUARDIAN_TELEGRAM_TOKEN="123456:ABC..."
export GUARDIAN_TELEGRAM_CHAT_ID="123456789"

export PROPR_API_KEY="pk_live_..."
python -m guardian --preset 1step
```

(On Windows PowerShell use `$env:PROPR_API_KEY="pk_live_..."` instead of `export`.)

The bot finds your active account automatically — funded accounts first, then
challenge attempts — and auto-detects the starting balance and loss limits
from the challenge definition where possible. If detection fails, pass
`--balance 10000` (your challenge size).

## What it monitors

Rules from the [official Propr rulebook](https://www.propr.xyz/rules), built
in as presets (every number can be overridden via flags, and limits reported
by the Propr API take precedence over preset defaults):

| Preset    | Max daily loss | Max drawdown   | Profit target |
|-----------|----------------|----------------|---------------|
| `1step`   | 3%             | 6% static      | 10%           |
| `2step-1` | 5%             | 8% trailing    | 5%            |
| `2step-2` | 5%             | 8% trailing    | 10%           |
| `funded`  | 3%             | 6% static      | —             |

For each poll it computes the exact breach floors and fires:

- **WARNING** at 70% of a loss budget consumed
- **CRITICAL** at 90%
- **BREACH** when equity touches a floor — or when Propr's own server marks
  the attempt `failed` (the server verdict always wins)
- **PASSED** when the profit target is reached or Propr marks the challenge passed
- a **watchdog alert** if the API becomes unreachable, so you know when you
  are flying blind

Warnings de-duplicate (no spam every poll) and re-arm after equity recovers
or the trading day rolls over. Equity follows the official SDK formula
(`balance + totalUnrealizedPnl + isolatedPositionMargin`), and the server's
`highWaterMark` is merged into the trailing-drawdown peak so the floor can
only tighten, never loosen.

## Useful flags

```
--preset {1step,2step-1,2step-2,funded}
--balance 10000            # only needed if auto-detection fails
--max-daily-loss 0.03      # override any preset/detected number
--max-drawdown 0.06
--profit-target 0.10
--warn-levels 0.5 0.75 0.9
--poll-interval 5          # seconds between checks (default 10)
--once                     # single check, for cron
--probe                    # dump raw Propr API responses for debugging
--address 0x...            # alternative mode: watch a wallet directly on
                           # Hyperliquid's public API instead of Propr
```

State (peak equity, daily anchor, sent alerts) persists to
`state/<account>.json` so restarts don't loosen a trailing floor. If you start
a brand-new challenge, delete the old state file first.

## Run the tests

```bash
python -m pytest tests/ -v
```

## Caveats

- **Verify the numbers.** Presets mirror the public rulebook as of June 2026,
  and limits are auto-detected from the API where possible — but Propr's own
  risk engine is the only thing that decides a breach. The daily reset time is
  assumed to be 00:00 UTC; the exact trailing-drawdown formula is configurable
  via `--trailing-mode` (defaults conservative). Treat this as an early-warning
  system, not as the source of truth.
- Polling (default 10s) can miss a fast wick between checks. Lower the
  interval for volatile sessions (the API allows 1,200 requests/min).
- Alerts-only by design. Auto-flattening positions before a breach is possible
  through the Propr order API and is left as future work.
