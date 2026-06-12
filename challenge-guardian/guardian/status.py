"""Shared live status that monitors write and the command listener reads."""

from __future__ import annotations

import threading
import time

from . import __version__


class StatusBoard:
    """Thread-safe snapshot of every guarded account's latest numbers."""

    def __init__(self):
        self._lock = threading.Lock()
        self._accounts: dict[str, dict] = {}

    def update(self, label: str, **fields) -> None:
        with self._lock:
            entry = self._accounts.setdefault(label, {})
            entry.update(fields)
            entry["updated_at"] = time.time()

    def render(self) -> str:
        with self._lock:
            accounts = {k: dict(v) for k, v in self._accounts.items()}
        header = f"Challenge Guardian v{__version__}"
        if not accounts:
            return (f"{header}\nNo accounts being guarded yet — "
                    "give me a few seconds after startup.")
        lines = [header]
        for label, e in accounts.items():
            age = time.time() - e.get("updated_at", 0)
            stale = " ⚠️ STALE" if age > 60 else ""
            lines.append(
                f"{label}{stale}\n"
                f"  equity: ${e.get('equity', 0):,.2f}\n"
                f"  daily floor: ${e.get('daily_floor', 0):,.2f} "
                f"(${e.get('equity', 0) - e.get('daily_floor', 0):,.2f} headroom)\n"
                f"  drawdown floor: ${e.get('dd_floor', 0):,.2f}\n"
                f"  peak: ${e.get('peak', 0):,.2f} | open positions: {e.get('positions', 0)}"
            )
        return "\n\n".join(lines)
