from __future__ import annotations

import queue
from collections.abc import Callable
from typing import TypeVar

from PySide6.QtCore import QObject, Signal, Slot

T = TypeVar("T")

# Generous: real calls take milliseconds. It only bounds how long an API
# thread can hang when the main thread is stuck or no longer running.
CALL_TIMEOUT = 15.0


class MainThreadBusy(Exception):
    """The Qt main thread didn't run the call within CALL_TIMEOUT."""


class MainThreadExecutor(QObject):
    """Runs a callable on the Qt GUI thread and blocks the caller for the result.

    DownloadManager (and the Storage/QThread workers it owns) is only safe to
    touch from the thread it lives on -- the Qt main thread. uvicorn serves
    API requests on its own thread, so every route handler that calls into
    the manager routes through here instead of calling it directly, avoiding
    a race between an HTTP request mutating a DownloadTask and a worker's
    progress signal doing the same thing at the same time.

    Must be constructed on the main thread: the signal/slot connection below
    is what makes the emit() from another thread hop onto the receiver's
    thread (Qt does this automatically for cross-thread connections).
    """

    _request = Signal(object, object)  # fn, result_queue

    def __init__(self):
        super().__init__()
        self._request.connect(self._run)

    @Slot(object, object)
    def _run(self, fn: Callable[[], T], result_queue: queue.Queue) -> None:
        try:
            result_queue.put(("ok", fn()))
        except Exception as exc:  # noqa: BLE001 -- re-raised on the caller's thread below
            result_queue.put(("error", exc))

    def call(self, fn: Callable[[], T], timeout: float | None = None) -> T:
        result_queue: queue.Queue = queue.Queue(maxsize=1)
        self._request.emit(fn, result_queue)
        # Without a timeout, a main thread that was blocked (or had stopped
        # its event loop, e.g. mid-quit) left every API thread waiting
        # forever. If fn does run later, its result just lands in a queue
        # nobody reads any more.
        try:
            status, value = result_queue.get(timeout=CALL_TIMEOUT if timeout is None else timeout)
        except queue.Empty:
            raise MainThreadBusy("The app is busy; try again") from None
        if status == "error":
            raise value
        return value
