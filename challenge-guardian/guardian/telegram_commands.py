"""Telegram command listener: lets the owner query the bot from the chat.

Read-only by design — commands report state, they never touch the account.
Only messages from the configured chat id are answered; everything else is
ignored silently. Runs as a daemon thread using Telegram long polling.

Note: Telegram allows only ONE getUpdates consumer per bot token. If you run
the guardian both locally and hosted with the same token, the listeners will
fight over updates (HTTP 409) — alerts still work, but commands should be
enabled on only one instance.
"""

from __future__ import annotations

import sys
import threading
import time

import requests

from .status import StatusBoard

HELP_TEXT = (
    "Challenge Guardian commands:\n"
    "/status — live equity, breach floors, and open positions per account\n"
    "/help — this message"
)

ACTIONS_HELP = (
    "\n/close <ASSET> — close that asset's position(s) on every account (e.g. /close BTC)\n"
    "/flatten — close ALL open positions on every account"
)

ACTIONS_DISABLED = (
    "Actions are disabled — I'm running read-only. Start the bot with "
    "--enable-actions (or GUARDIAN_ACTIONS=on) to allow /close and /flatten."
)


class TelegramCommandListener(threading.Thread):
    def __init__(self, token: str, chat_id: str, board: StatusBoard,
                 action_clients: dict | None = None, timeout: float = 35.0):
        super().__init__(daemon=True, name="telegram-commands")
        self.api = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.board = board
        self.action_clients = action_clients  # {label: ProprClient} or None
        self.timeout = timeout
        self._offset: int | None = None

    def handle_update(self, update: dict) -> str | None:
        """Return the reply text for an update, or None to stay silent."""
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("id")) != self.chat_id:
            return None  # not the owner: ignore, don't even reply
        text = (message.get("text") or "").strip().lower()
        if text.startswith("/status"):
            return self.board.render()
        if text.startswith("/start") or text.startswith("/help"):
            help_text = HELP_TEXT + (ACTIONS_HELP if self.action_clients else "")
            footer = ("\n\nActions are ENABLED — /close and /flatten place real "
                      "reduce-only orders." if self.action_clients
                      else "\n\nI'm read-only: I watch and warn, I never trade.")
            return help_text + footer
        if text.startswith("/flatten"):
            return self._run_action(base=None)
        if text.startswith("/close"):
            parts = text.split()
            if len(parts) < 2:
                return "Usage: /close <ASSET>, e.g. /close BTC"
            return self._run_action(base=parts[1].upper())
        if text.startswith("/"):
            return f"Unknown command {text.split()[0]}. Try /help."
        return None

    def _run_action(self, base: str | None) -> str:
        if not self.action_clients:
            return ACTIONS_DISABLED
        what = f"{base} position(s)" if base else "ALL positions"
        lines = [f"Closing {what}…"]
        closed_any = False
        for label, client in self.action_clients.items():
            try:
                results = client.flatten_positions(base=base)
            except Exception as exc:
                lines.append(f"{label}: FAILED — {exc}")
                continue
            if not results:
                lines.append(f"{label}: nothing to close")
                continue
            closed_any = True
            for r in results:
                mark = "✅" if r["ok"] else "❌"
                lines.append(f"{label}: {mark} {r['position']} → {r['detail']}")
        if not closed_any and base:
            lines.append(f"No open {base} positions found on any account.")
        return "\n".join(lines)

    def run(self) -> None:
        print("Telegram commands enabled: send /status to the bot.", flush=True)
        while True:
            try:
                resp = requests.get(
                    f"{self.api}/getUpdates",
                    params={"timeout": int(self.timeout - 5), "offset": self._offset},
                    timeout=self.timeout,
                )
                if resp.status_code == 409:
                    print("Telegram commands: another instance is already listening "
                          "on this bot token; retrying in 60s.", file=sys.stderr, flush=True)
                    time.sleep(60)
                    continue
                resp.raise_for_status()
                for update in resp.json().get("result", []):
                    self._offset = update["update_id"] + 1
                    reply = self.handle_update(update)
                    if reply:
                        self._send(reply)
            except requests.RequestException as exc:
                print(f"Telegram commands: {exc}", file=sys.stderr, flush=True)
                time.sleep(5)

    def _send(self, text: str) -> None:
        try:
            requests.post(
                f"{self.api}/sendMessage",
                json={"chat_id": self.chat_id, "text": text[:4096]},
                timeout=10,
            ).raise_for_status()
        except requests.RequestException as exc:
            print(f"Telegram command reply failed: {exc}", file=sys.stderr, flush=True)
