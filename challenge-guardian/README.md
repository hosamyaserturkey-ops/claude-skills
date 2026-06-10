# Challenge Guardian

A risk-compliance bot for [Propr](https://www.propr.xyz/) challenge accounts.
It watches your account equity in real time on Hyperliquid and alerts you on
Discord/Telegram **before** you breach a challenge rule — because on Propr,
breaches include floating PnL and are permanent.

## What it monitors

Rules from the [official Propr rulebook](https://www.propr.xyz/rules), built
in as presets (every number can be overridden via flags):

| Preset    | Max daily loss | Max drawdown   | Profit target |
|-----------|----------------|----------------|---------------|
| `1step`   | 3%             | 6% static      | 10%           |
| `2step-1` | 5%             | 8% trailing    | 5%            |
| `2step-2` | 5%             | 8% trailing    | 10%           |
| `funded`  | 3%             | 6% static      | —             |

For each poll it computes the exact breach floors and fires:

- **WARNING** at 70% of a loss budget consumed
- **CRITICAL** at 90%
- **BREACH** when equity touches a floor (the monitor then stops — Propr
  breaches are permanent)
- **PASSED** when equity reaches the profit target
- a **watchdog alert** if the API becomes unreachable, so you know when you
  are flying blind

Warnings de-duplicate (no spam every poll) and re-arm after equity recovers
or the trading day rolls over.

## How it works

Propr runs on Hyperliquid, so account equity (including unrealized PnL) is
readable for any wallet address through the public, unauthenticated
[Info API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint).
No API keys, no signing — the bot is **read-only and cannot touch your
positions**.

## Quick start

```bash
cd challenge-guardian
pip install -r requirements.txt

# Optional alert channels:
export GUARDIAN_DISCORD_WEBHOOK="https://discord.com/api/webhooks/..."
export GUARDIAN_TELEGRAM_TOKEN="123456:ABC..."
export GUARDIAN_TELEGRAM_CHAT_ID="123456789"

python -m guardian --address 0xYourChallengeAccount --balance 10000 --preset 1step
```

Useful flags:

```
--preset {1step,2step-1,2step-2,funded}
--max-daily-loss 0.03      # override any preset number
--max-drawdown 0.06
--profit-target 0.10
--warn-levels 0.5 0.75 0.9
--poll-interval 5          # seconds between checks (default 10)
--once                     # single check, for cron
--testnet
```

State (peak equity, daily anchor, sent alerts) persists to
`state/<address>.json` so restarts don't loosen a trailing floor.

## Run the tests

```bash
python -m pytest tests/ -v
```

## Caveats

- **Verify the numbers.** Presets mirror the public rulebook as of June 2026;
  Propr can change rules, and the exact trailing-drawdown formula and the
  daily reset time (assumed 00:00 UTC here) are not spelled out publicly.
  Treat this as an early-warning system, not as the source of truth — Propr's
  own risk engine is the only thing that decides a breach.
- Polling (default 10s) can miss a fast wick between checks. Lower the
  interval for volatile sessions.
- Alerts-only by design. Auto-flattening positions before a breach would need
  authenticated exchange access (agent wallet) and is left as future work.
