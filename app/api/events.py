from __future__ import annotations

import asyncio

# Per-client backlog. Each progress tick is one event, so a client that
# stops reading (or a stalled socket) used to grow its queue without end.
QUEUE_LIMIT = 2000
# Put on a client's queue in place of the event that didn't fit: the
# WebSocket handler closes that connection, and the UI's reconnect re-reads
# the full list (see connectWS in index.html) instead of missing events.
OVERFLOW = object()


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

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_LIMIT + 1)  # +1: room for OVERFLOW
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    @staticmethod
    def _deliver(q: asyncio.Queue, message: dict) -> None:
        # Runs on the event loop. Once OVERFLOW is queued the client is
        # being disconnected, so later events for it are simply dropped.
        if q.qsize() < QUEUE_LIMIT:
            q.put_nowait(message)
        elif q.qsize() == QUEUE_LIMIT:
            q.put_nowait(OVERFLOW)

    def publish(self, message: dict) -> None:
        # Take a snapshot under the loop's lock semantics: call_soon_threadsafe
        # from a different thread races with the loop being closed on shutdown.
        # If the loop is gone or any single subscriber queue misbehaves, drop
        # the message for that subscriber rather than crash the process.
        try:
            subscribers = list(self._subscribers)
        except Exception:
            return
        for q in subscribers:
            try:
                self._loop.call_soon_threadsafe(self._deliver, q, message)
            except RuntimeError:
                # Loop closed -- drop the message for this subscriber. The UI
                # is best-effort; losing one event is better than aborting
                # the whole process.
                continue
            except Exception:
                continue
