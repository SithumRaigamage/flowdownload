"""Drive the real web UI (app/web/index.html) in an offscreen QtWebEngine page,
talking to a real ApiServer + DownloadManager on a temp DB and a free port."""
from __future__ import annotations

import json
import os
import socket
import time

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtWebEngineCore import QWebEnginePage

from app.api.runner import ApiServer
from app.engine.models import DownloadStatus as S
from app.version import APP_VERSION

pytestmark = pytest.mark.ui

SLOW = "delay=0.2"  # ~2.6 s per file: stays active long enough to inspect


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Page:
    """A loaded FlowDownload page plus helpers to evaluate JS in it."""

    def __init__(self, qapp, wait, url):
        self._qapp, self._wait, self._url = qapp, wait, url
        self.page = QWebEnginePage()
        self.load()

    def load(self):
        loaded = {}
        self.page.loadFinished.connect(lambda ok: loaded.setdefault("ok", ok))
        self.page.load(QUrl(self._url))
        assert self._wait(lambda: "ok" in loaded, timeout=30) and loaded["ok"]
        # boot() fetches the initial state and opens the WebSocket.
        assert self.wait_js("ui.wsConnected === true")

    def js(self, expr):
        box = {}
        script = ("(() => { try { const v = (" + expr + "); return JSON.stringify({v: v === undefined ? null : v}); }"
                  " catch (e) { return JSON.stringify({error: String(e)}); } })()")
        self.page.runJavaScript(script, 0, lambda r: box.setdefault("r", r))
        assert self._wait(lambda: "r" in box, timeout=10), f"no result for: {expr}"
        out = json.loads(box["r"])
        assert "error" not in out, f"JS error in {expr!r}: {out.get('error')}"
        return out["v"]

    def wait_js(self, expr, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.js(expr):
                return True
            self._wait(lambda: False, timeout=0.1)
        return False

    def row(self, task_id, selector):
        return f"document.querySelector('.row[data-id=\"{task_id}\"] {selector}')"


@pytest.fixture
def ui(qapp, wait, make_manager, tmp_path):
    def _open(**settings):
        mgr = make_manager(**settings)
        server = ApiServer(mgr, str(tmp_path / "settings.json"), port=_free_port(),
                           app_data_dir=str(tmp_path / "appdata"))
        server.start()
        opened.append(server)
        return mgr, Page(qapp, wait, f"http://127.0.0.1:{server.port}/")

    opened = []
    yield _open
    for server in opened:
        server.stop()


def test_version_in_sidebar_and_about_tab(ui):
    _, page = ui()
    assert page.js("document.getElementById('appVersion').textContent") == f"FlowDownload v{APP_VERSION}"
    page.js("document.getElementById('settingsBtn').click()")
    assert page.wait_js("!!document.querySelector('[data-st=\"About\"]')")
    page.js("document.querySelector('[data-st=\"About\"]').click()")
    assert f"v{APP_VERSION}" in page.js("document.getElementById('setPane').textContent")


def test_theme_survives_reload(ui):
    _, page = ui()
    assert page.js("document.documentElement.classList.contains('dark')") is False
    page.js("document.getElementById('themeBtn').click()")
    page.load()
    assert page.js("document.documentElement.classList.contains('dark')") is True


def test_row_badge_follows_state_changes(ui, server, wait):
    mgr, page = ui()
    task = mgr.tasks[mgr.add_download(f"{server}/f?{SLOW}", "f.bin")]
    badge = page.row(task.id, ".badge") + "?.textContent"
    assert page.wait_js(f"{badge} === 'Downloading'")
    mgr.pause(task.id)
    assert page.wait_js(f"{badge} === 'Paused'")


def test_queued_row_explains_why(ui, server):
    mgr, page = ui(max_concurrent_downloads=1)
    mgr.add_download(f"{server}/a?{SLOW}", "a.bin")
    queued = mgr.tasks[mgr.add_download(f"{server}/b?{SLOW}", "b.bin")]
    assert queued.status == S.QUEUED
    assert page.wait_js(page.row(queued.id, ".rbot") + "?.textContent.includes('Waiting for a free slot')")


def test_retry_reason_survives_progress_updates(ui, server, wait):
    mgr, page = ui()
    task = mgr.tasks[mgr.add_download(f"{server}/f?{SLOW}", "f.bin")]
    assert wait(lambda: task.downloaded_bytes > 0)
    mgr._on_status_changed(task.id, "retrying", "ConnectionError — retrying in 1s")
    reason = page.row(task.id, ".rbot") + "?.textContent.includes('ConnectionError')"
    assert page.wait_js(reason)
    before = task.downloaded_bytes
    assert wait(lambda: task.downloaded_bytes > before)  # live progress events keep arriving
    wait(lambda: False, timeout=0.5)
    assert page.js(reason) is True


def test_delete_file_dialog(ui, server, wait):
    mgr, page = ui()
    task = mgr.tasks[mgr.add_download(f"{server}/done", "done.bin")]
    assert wait(lambda: task.status == S.COMPLETED)
    assert os.path.exists(task.dest_path)
    assert page.wait_js(page.row(task.id, ".badge") + "?.textContent === 'Completed'")
    page.js(f"document.querySelector('.row[data-id=\"{task.id}\"]')"
            ".dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, clientX: 20, clientY: 20}))")
    page.js("document.querySelector('#ctxmenu [data-m=\"deletefile\"]').click()")
    assert page.wait_js("!!document.getElementById('delGo')")
    assert page.js("document.activeElement.textContent") == "Cancel"
    page.js("document.getElementById('delGo').click()")
    assert page.wait_js(f"!document.querySelector('.row[data-id=\"{task.id}\"]')")
    assert wait(lambda: not os.path.exists(task.dest_path))
    assert task.id not in mgr.tasks
