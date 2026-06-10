"""Alert sinks: console, Discord webhook, Telegram bot.

Each sink swallows its own delivery errors (logged to stderr) so one broken
channel never stops the monitoring loop or the other channels.
"""

from __future__ import annotations

import sys

import requests

from .rules import RuleEvent

_EMOJI = {
    "INFO": "ℹ️",
    "WARNING": "⚠️",
    "CRITICAL": "🚨",
    "BREACH": "💀",
    "PASSED": "🏆",
}


def format_event(event: RuleEvent, account_label: str) -> str:
    emoji = _EMOJI.get(event.severity, "")
    return f"{emoji} [{event.severity}] {account_label} — {event.message}"


class ConsoleAlerter:
    def send(self, text: str) -> None:
        print(text, flush=True)


class DiscordAlerter:
    def __init__(self, webhook_url: str, timeout: float = 10.0):
        self.webhook_url = webhook_url
        self.timeout = timeout

    def send(self, text: str) -> None:
        try:
            resp = requests.post(self.webhook_url, json={"content": text[:2000]}, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"Discord alert failed: {exc}", file=sys.stderr, flush=True)


class TelegramAlerter:
    def __init__(self, bot_token: str, chat_id: str, timeout: float = 10.0):
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id
        self.timeout = timeout

    def send(self, text: str) -> None:
        try:
            resp = requests.post(
                self.url,
                json={"chat_id": self.chat_id, "text": text[:4096]},
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"Telegram alert failed: {exc}", file=sys.stderr, flush=True)


def dispatch(alerters: list, event: RuleEvent, account_label: str) -> None:
    text = format_event(event, account_label)
    # Console always gets everything; remote channels skip nothing either —
    # every event the rule engine emits is worth a push.
    for alerter in alerters:
        alerter.send(text)
