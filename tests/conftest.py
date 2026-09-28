"""Shared fixtures: a local HTTP file server and a throwaway DownloadManager.

Everything runs against 127.0.0.1 -- no test touches a real external server.
"""
from __future__ import annotations

import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

# QtWebEngine (used by tests/ui) must be imported before the QApplication
# exists, and renders fine offscreen -- so no window ever appears.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6 import QtWebEngineWidgets  # noqa: E402,F401
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.engine.manager import DownloadManager  # noqa: E402
from app.engine.settings import Settings  # noqa: E402
from app.engine.storage import Storage  # noqa: E402

DATA = os.urandom(400_000)
CHUNK = 32 * 1024
# (path, headers) of every request the fixture server received, for tests
# that check what was actually sent over the wire.
REQUESTS: list[tuple[str, dict]] = []


class _Handler(BaseHTTPRequestHandler):
    """Serves DATA with Range/ETag support. Query params shape the behaviour:

    delay=<s>    sleep between 32 KB chunks (default 0.01)
    drop=<n>     close the connection after sending n bytes of the body
    status=<n>   reply with this status and an empty body instead
    """

    def log_message(self, *args):
        pass

    def do_GET(self):
        REQUESTS.append((self.path, dict(self.headers)))
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        if "status" in q:
            self.send_response(int(q["status"]))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        rng = self.headers.get("Range")
        start = int(rng.split("=")[1].split("-")[0]) if rng else 0
        body = DATA[start:]
        self.send_response(206 if rng else 200)
        self.send_header("ETag", '"fixture"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        limit = int(q["drop"]) if "drop" in q else len(body)
        delay = float(q.get("delay", 0.01))
        try:
            sent = 0
            while sent < min(limit, len(body)):
                piece = body[sent:min(sent + CHUNK, limit)]
                self.wfile.write(piece)
                self.wfile.flush()
                sent += len(piece)
                time.sleep(delay)
            if limit < len(body):
                self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # client went away (pause/cancel) -- expected


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="session")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def wait(qapp):
    """wait(condition, timeout) -> bool, pumping the Qt event loop meanwhile
    so worker signals get delivered to the manager."""
    def _wait(condition, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            qapp.processEvents()
            if condition():
                return True
            time.sleep(0.01)
        return False
    return _wait


@pytest.fixture
def make_manager(qapp, tmp_path):
    """Build DownloadManagers on a temp DB/download dir; shut all down after.

    Shutdown matters: a QThread garbage-collected while running aborts the
    whole process, so every worker is stopped before the test returns.
    """
    made = []

    def _make(db_name="t.db", **settings):
        settings.setdefault("download_dir", str(tmp_path / "dl"))
        mgr = DownloadManager(Storage(str(tmp_path / db_name)), Settings(**settings))
        made.append(mgr)
        return mgr

    yield _make
    for mgr in made:
        if not mgr._shutting_down:
            mgr.shutdown()
    for _ in range(20):
        qapp.processEvents()
