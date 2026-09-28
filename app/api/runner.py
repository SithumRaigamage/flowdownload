from __future__ import annotations

import asyncio
import os
import secrets
import threading
import time

import uvicorn

from app.api.events import EventBroadcaster
from app.api.main_thread import MainThreadExecutor
from app.api.schemas import DownloadOut
from app.api.server import create_app
from app.engine.manager import DownloadManager

API_PORT = 8756
APP_DATA_DIR = os.path.join(os.path.expanduser("~"), ".flowdownload")


class ApiServer:
    """Owns the local HTTP/WebSocket API: a FastAPI app served by uvicorn on
    its own thread, bridged back to the Qt-thread-bound DownloadManager via
    MainThreadExecutor, with live updates pushed out over EventBroadcaster.

    Each DownloadWorker already throttles its progress signal to 4/s
    (PROGRESS_EMIT_INTERVAL in downloader.py), and DownloadManager forwards
    each one as a task_updated signal -- so simply relaying those signals
    here already gives the frontend batched, bounded-rate updates without a
    second timer duplicating that throttling.

    Must be constructed on the Qt main thread: MainThreadExecutor's
    signal/slot bridge needs main-thread affinity to work.
    """

    def __init__(self, manager: DownloadManager, settings_path: str,
                 port: int = API_PORT, app_data_dir: str = APP_DATA_DIR):
        # port/app_data_dir are overridable so tests can run a second server
        # beside a running app without fighting it for 8756 or ~/.flowdownload.
        self.manager = manager
        self.token = secrets.token_urlsafe(32)
        self.port = port
        self.executor = MainThreadExecutor()
        # Optional callback registered by the UI (WebMainWindow) to open a
        # completed file in the native OS media player. Lives on the Qt main
        # thread -- the API endpoint routes through self.executor.call() so
        # invocations land there.
        self._native_player_opener = None

        self._loop = asyncio.new_event_loop()
        self.broadcaster = EventBroadcaster(self._loop)
        app = create_app(
            manager, self.executor, self.broadcaster, settings_path, self.token, app_data_dir,
            get_native_player_opener=lambda: self._native_player_opener,
        )
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, loop="none", log_level="warning")
        self._server = uvicorn.Server(config)

        manager.task_added.connect(self._on_task_added)
        manager.task_updated.connect(self._on_task_changed)
        manager.task_removed.connect(self._on_task_removed)

        self._thread = threading.Thread(target=self._run_loop, daemon=True)

    def start(self) -> None:
        self._thread.start()
        # start() spawning the thread doesn't mean the socket is bound yet --
        # WebMainWindow navigates to this server's URL right after this
        # returns, and a connection attempt that lands before uvicorn has
        # bound the port gets connection-refused, which QtWebEngine renders
        # as a permanent Chromium error page instead of retrying. uvicorn
        # sets `Server.started` the instant the listening socket is up
        # (see Server.startup()), so block briefly on that instead of racing.
        deadline = time.monotonic() + 5
        while not self._server.started and time.monotonic() < deadline:
            time.sleep(0.005)

    def set_native_player_opener(self, opener) -> None:
        """Register a callback that opens a completed file in the native player.

        The callback must be safe to invoke on the Qt main thread (it is
        routed through the executor). WebMainWindow registers its own
        _open_native_player method here so the API endpoint can hand off
        playback without needing a custom URL scheme.
        """
        self._native_player_opener = opener

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(setattr, self._server, "should_exit", True)
        self._thread.join(timeout=2)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._server.serve())

    def _on_task_added(self, task_id: str) -> None:
        try:
            task = self.manager.tasks.get(task_id)
            if task is not None:
                self.broadcaster.publish({"type": "added", "download": DownloadOut.from_task(task).model_dump()})
        except Exception:
            # Slot must never raise: an exception escaping a Qt slot crosses
            # into C++ qt_metacall which has no place to put it, and Qt's
            # policy is qFatal() -> abort(). Swallow + log instead.
            import traceback
            print("FlowDownload: _on_task_added swallowed exception:", traceback.format_exc(), flush=True)

    def _on_task_changed(self, task_id: str) -> None:
        try:
            task = self.manager.tasks.get(task_id)
            if task is not None:
                self.broadcaster.publish({"type": "updated", "download": DownloadOut.from_task(task).model_dump()})
        except Exception:
            import traceback
            print("FlowDownload: _on_task_changed swallowed exception:", traceback.format_exc(), flush=True)

    def _on_task_removed(self, task_id: str) -> None:
        try:
            self.broadcaster.publish({"type": "removed", "id": task_id})
        except Exception:
            import traceback
            print("FlowDownload: _on_task_removed swallowed exception:", traceback.format_exc(), flush=True)
