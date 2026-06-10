"""Tests for the Telegram command handler and the shared status board."""

from __future__ import annotations

from guardian.status import StatusBoard
from guardian.telegram_commands import TelegramCommandListener


def make_listener(board: StatusBoard | None = None) -> TelegramCommandListener:
    return TelegramCommandListener("tok", "12345", board or StatusBoard())


def update(chat_id, text):
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}


def test_status_command_renders_board():
    board = StatusBoard()
    board.update("Propr 1-Step (acc1)", equity=4992.59, daily_floor=4842.59,
                 dd_floor=4700.0, peak=5000.0, positions=1)
    reply = make_listener(board).handle_update(update(12345, "/status"))
    assert "Propr 1-Step (acc1)" in reply
    assert "$4,992.59" in reply
    assert "open positions: 1" in reply
    assert "$150.00 headroom" in reply


def test_status_before_first_poll():
    reply = make_listener().handle_update(update(12345, "/status"))
    assert "few seconds" in reply


def test_help_and_unknown_commands():
    listener = make_listener()
    assert "/status" in listener.handle_update(update(12345, "/help"))
    assert "/status" in listener.handle_update(update(12345, "/start"))
    assert "Unknown command" in listener.handle_update(update(12345, "/flatten"))
    # Plain chatter (no slash) gets no reply.
    assert listener.handle_update(update(12345, "hello bot")) is None


def test_other_chats_are_ignored_silently():
    listener = make_listener()
    assert listener.handle_update(update(99999, "/status")) is None
    assert listener.handle_update(update(99999, "/help")) is None
    assert listener.handle_update({"update_id": 2}) is None  # no message at all


def test_stale_account_is_flagged():
    board = StatusBoard()
    board.update("acct", equity=1.0, daily_floor=0, dd_floor=0, peak=1.0, positions=0)
    board._accounts["acct"]["updated_at"] -= 120  # simulate a stalled monitor
    assert "STALE" in board.render()
