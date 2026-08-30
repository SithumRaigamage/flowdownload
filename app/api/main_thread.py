from __future__ import annotations

import queue
from typing import Callable, TypeVar

from PySide6.QtCore import QObject, Signal, Slot

T = TypeVar("T")


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
    def _run(self, fn: Callable[[], T], result_queue: "queue.Queue") -> None:
        try:
            result_queue.put(("ok", fn()))
        except Exception as exc:  # noqa: BLE001 -- re-raised on the caller's thread below
            result_queue.put(("error", exc))

    def call(self, fn: Callable[[], T]) -> T:
        result_queue: "queue.Queue" = queue.Queue(maxsize=1)
        self._request.emit(fn, result_queue)
        status, value = result_queue.get()
        if status == "error":
            raise value
        return value
