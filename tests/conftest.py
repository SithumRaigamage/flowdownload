"""Shared fixtures: a local HTTP file server and a throwaway DownloadManager.

Everything runs against 127.0.0.1 -- no test touches a real external server.
"""
from __future__ import annotations

import gc
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
# Concurrent connections being served right now, and the peak since reset.
CONNECTIONS = {"active": 0, "peak": 0}
_CONN_LOCK = threading.Lock()
# path -> ETag, set mid-test to simulate the file changing on the server.
ETAG_OVERRIDE: dict[str, str] = {}


class _Handler(BaseHTTPRequestHandler):
    """Serves DATA with Range/ETag support. Query params shape the behaviour:

    delay=<s>    sleep between 32 KB chunks (default 0.01)
    drop=<n>     close the connection after sending n bytes of the body
    status=<n>   reply with this status and an empty body instead
    etag=<s>     serve this ETag (default "fixture"); an If-Range that
                 doesn't match it gets the full body (200), as real servers do
    """

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        self.send_response(int(q.get("status", 200)))
        self.send_header("ETag", '"%s"' % q.get("etag", "fixture"))
        self.send_header("Content-Length", str(len(DATA)))
        self.end_headers()

    def do_GET(self):
        REQUESTS.append((self.path, dict(self.headers)))
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        if "status" in q:
            self.send_response(int(q["status"]))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        etag = '"%s"' % ETAG_OVERRIDE.get(urlparse(self.path).path, q.get("etag", "fixture"))
        rng = self.headers.get("Range")
        if_range = self.headers.get("If-Range")
        if rng and if_range and if_range != etag:
            rng = None  # file changed since the client's copy: send it all
        start, end = 0, len(DATA) - 1
        if rng:
            first, _, last = rng.split("=")[1].partition("-")
            start, end = int(first), (int(last) if last else len(DATA) - 1)
        body = DATA[start:end + 1]
        self.send_response(206 if rng else 200)
        self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(body)))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(DATA)}")
        self.end_headers()
        with _CONN_LOCK:
            CONNECTIONS["active"] += 1
            CONNECTIONS["peak"] = max(CONNECTIONS["peak"], CONNECTIONS["active"])
        try:
            self._send_body(body, q)
        finally:
            with _CONN_LOCK:
                CONNECTIONS["active"] -= 1

    def _send_body(self, body: bytes, q: dict) -> None:
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
            mgr.shutdown()  # also stops the manager's 2 s poll timer
    for _ in range(20):
        qapp.processEvents()
    # A manager's signal connections form reference cycles, so it isn't freed
    # when the test drops it -- Python's cycle collector frees it later, at an
    # arbitrary allocation. If that lands inside Qt's timer dispatch in a
    # later test, the manager's QTimer is destroyed mid-iteration and the
    # process segfaults in QTimerInfoList::activateTimers. Collect here, at a
    # known-safe point, instead. (The app has one manager for its lifetime.)
    made.clear()
    gc.collect()
