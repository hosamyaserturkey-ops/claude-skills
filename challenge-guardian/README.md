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

Instead of `export` lines you can copy `.env.example` to `.env` in the
`challenge-guardian` folder and fill in your values — the bot reads it
automatically. Never commit `.env`.

## Multiple accounts

Guard everything at once with `--all` (one process, one monitor per account):

```bash
python -m guardian --all
```

Or pick one account per terminal window:

```bash
python -m guardian --list-accounts
python -m guardian --preset 1step --account 1
python -m guardian --preset 2step-1 --account 2   # in a second window
```

`--account` takes the number from `--list-accounts` or any unique part of the
account id. The bot warns at startup if it sees active accounts it isn't
guarding. Note: `--all` applies the same `--preset` to every account (the
loss limits are auto-detected per account where the API provides them).

## Hosting it 24/7

The bot only protects you while it's running, so for real use deploy it to a
small always-on host. A `Dockerfile` is included; the container defaults to
`python -m guardian --all`.

**Railway / Render (no server admin needed):**
1. Push this repo to GitHub (already done if you're reading this there).
2. Create a new service from the repo; set the root directory to
   `challenge-guardian` (both platforms auto-detect the Dockerfile).
3. Add the environment variables from `.env.example` (`PROPR_API_KEY`,
   `GUARDIAN_TELEGRAM_TOKEN`, `GUARDIAN_TELEGRAM_CHAT_ID`,
   `GUARDIAN_DISCORD_WEBHOOK`, `GUARDIAN_ALL=true`, `GUARDIAN_PRESET=...`).
4. Deploy. The logs show the same status lines as running locally.

**Any VPS / Docker host:**

```bash
docker build -t challenge-guardian .
docker run -d --restart unless-stopped --env-file .env \
  -v guardian-state:/app/state challenge-guardian
```

Note on restarts: local floor state lives in `state/`. On hosts with an
ephemeral filesystem the bot reconstructs the important part (the trailing
peak) from Propr's server-side `highWaterMark`, but mount a volume for
`/app/state` where you can.

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
- a **trade notification** whenever a position opens or closes, with current
  equity and the remaining headroom to today's loss floor (disable with
  `--no-trade-alerts`)
- a **watchdog alert** if the API becomes unreachable, so you know when you
  are flying blind

Warnings de-duplicate (no spam every poll) and re-arm after equity recovers
or the trading day rolls over.

## Telegram commands

When Telegram alerts are configured, the bot also listens for commands —
only from your own chat ID; everyone else is ignored:

- `/status` — live equity, breach floors, headroom, and open positions for
  every guarded account
- `/help` — command list

Commands are read-only: the bot watches and warns, it never trades. Disable
with `--no-telegram-commands` or `GUARDIAN_TELEGRAM_COMMANDS=off`. Telegram
allows one command listener per bot token, so when running locally and hosted
at the same time, leave commands enabled on only one of them (alerts are
unaffected). Equity follows the official SDK formula
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
--list-accounts            # show your active accounts and exit
--account 2                # guard a specific account (number or id fragment)
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
