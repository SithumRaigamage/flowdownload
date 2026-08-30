from __future__ import annotations

import asyncio


class EventBroadcaster:
    """Fans out events from the Qt main thread to WebSocket clients on the
    asyncio event loop uvicorn runs.

    publish() is called from the Qt main thread (directly from a signal
    handler, or from the periodic tick QTimer); call_soon_threadsafe is what
    makes handing the message to the asyncio loop from a foreign thread safe.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> "asyncio.Queue":
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue") -> None:
        self._subscribers.discard(q)

    def publish(self, message: dict) -> None:
        for q in list(self._subscribers):
            self._loop.call_soon_threadsafe(q.put_nowait, message)
