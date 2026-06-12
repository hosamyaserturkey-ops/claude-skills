"""WebSocket nudger: wake the monitors the instant Propr pushes an event.

Propr streams account.updated / position.updated / order.filled etc. over
wss://api.propr.xyz/ws (authenticated with the same X-API-Key header). We
don't trust event payloads for breach math — the monitors immediately re-poll
the REST API instead, so polling remains the single source of truth and the
WebSocket only removes the up-to-10s reaction delay. If the connection drops
or the websockets package is missing, the bot silently stays on polling.
"""

from __future__ import annotations

import sys
import threading


class WebSocketNudger(threading.Thread):
    def __init__(self, api_key: str, url: str, nudges: list[threading.Event]):
        super().__init__(daemon=True, name="ws-nudger")
        self.api_key = api_key
        self.url = url
        self.nudges = nudges

    def nudge_all(self) -> None:
        for event in self.nudges:
            event.set()

    def run(self) -> None:
        try:
            import asyncio

            import websockets
        except ImportError:
            print("websockets package not installed — staying on 10s polling. "
                  "pip install websockets to enable real-time reactions.",
                  file=sys.stderr, flush=True)
            return

        async def loop() -> None:
            backoff = 1.0
            while True:
                try:
                    async with websockets.connect(
                        self.url,
                        additional_headers={"X-API-Key": self.api_key},
                        ping_interval=20,
                        ping_timeout=10,
                    ) as ws:
                        print("WebSocket connected: reacting to events in real time.",
                              flush=True)
                        backoff = 1.0
                        async for _ in ws:
                            # Any event on this user's account is a reason to
                            # re-check equity right now.
                            self.nudge_all()
                except Exception as exc:
                    print(f"WebSocket dropped ({exc}); polling continues, "
                          f"reconnecting in {backoff:.0f}s.", file=sys.stderr, flush=True)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)

        import asyncio
        asyncio.run(loop())
