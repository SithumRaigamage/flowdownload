"""Drive the real web UI (app/web/index.html) in an offscreen QtWebEngine page,
talking to a real ApiServer + DownloadManager on a temp DB and a free port."""
from __future__ import annotations

import json
import os
import time

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtWebEngineCore import QWebEnginePage

from app.api.runner import ApiServer
from app.engine.models import DownloadStatus as S
from app.version import APP_VERSION

pytestmark = pytest.mark.ui

SLOW = "delay=0.2"  # ~2.6 s per file: stays active long enough to inspect


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
        server = ApiServer(mgr, str(tmp_path / "settings.json"), app_data_dir=str(tmp_path / "appdata"))
        server.start()
        opened.append(server)
        return mgr, Page(qapp, wait, server.page_url)

    opened = []
    yield _open
    for server in opened:
        server.stop()


def test_version_in_header_and_about_tab(ui):
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


def _open_menu(page, task_id):
    page.js(f"document.querySelector('.row[data-id=\"{task_id}\"]')"
            ".dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, clientX: 20, clientY: 20}))")


def test_removing_an_unfinished_download_asks_before_discarding(ui, server, wait):
    mgr, page = ui()
    task = mgr.tasks[mgr.add_download(f"{server}/f?delay=0.05", "f.bin")]
    assert wait(lambda: task.downloaded_bytes > 100_000)
    mgr.pause(task.id)
    assert page.wait_js(page.row(task.id, ".badge") + "?.textContent === 'Paused'")
    _open_menu(page, task.id)
    page.js("document.querySelector('#ctxmenu [data-m=\"remove\"]').click()")
    assert page.wait_js("!!document.getElementById('remGo')")
    assert page.js("document.activeElement.textContent") == "Keep"  # the safe default
    page.js("document.querySelector('[data-close]').click()")
    assert page.wait_js("!document.getElementById('remGo')")
    assert task.id in mgr.tasks  # "Keep" left it alone
    _open_menu(page, task.id)
    page.js("document.querySelector('#ctxmenu [data-m=\"remove\"]').click()")
    assert page.wait_js("!!document.getElementById('remGo')")
    page.js("document.getElementById('remGo').click()")
    assert wait(lambda: task.id not in mgr.tasks)


def test_cancel_from_the_menu(ui, server, wait):
    mgr, page = ui()
    task = mgr.tasks[mgr.add_download(f"{server}/f?{SLOW}", "f.bin")]
    assert page.wait_js(page.row(task.id, ".badge") + "?.textContent === 'Downloading'")
    _open_menu(page, task.id)
    page.js("document.querySelector('#ctxmenu [data-m=\"cancel\"]').click()")
    assert wait(lambda: task.status == S.CANCELLED)
    assert page.wait_js(page.row(task.id, ".badge") + "?.textContent === 'Cancelled'")


def test_refresh_link_dialog_resumes_download(ui, server, wait):
    mgr, page = ui()
    task = mgr.tasks[mgr.add_download(f"{server}/old?delay=0.05", "movie.mp4")]
    assert wait(lambda: task.downloaded_bytes > 100_000)
    mgr.pause(task.id)
    assert page.wait_js(page.row(task.id, ".badge") + "?.textContent === 'Paused'")
    page.js(f"document.querySelector('.row[data-id=\"{task.id}\"]')"
            ".dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, clientX: 20, clientY: 20}))")
    page.js("document.querySelector('#ctxmenu [data-m=\"refresh\"]').click()")
    assert page.wait_js("!!document.getElementById('refUrl')")
    page.js(f"document.getElementById('refUrl').value = '{server}/new'")
    page.js("document.getElementById('refGo').click()")
    assert wait(lambda: task.url == f"{server}/new")
    assert wait(lambda: task.status == S.COMPLETED)
    assert page.wait_js("!document.getElementById('refUrl')")  # dialog closed


def test_expired_link_hint_on_403(ui, server, wait):
    mgr, page = ui()
    task = mgr.tasks[mgr.add_download(f"{server}/gone?status=403", "gone.bin")]
    assert wait(lambda: task.status == S.FAILED)
    assert page.wait_js(page.row(task.id, ".rbot") + "?.textContent.includes('Link may have expired')")


def test_connections_setting_saves(ui):
    mgr, page = ui()
    page.js("document.getElementById('settingsBtn').click()")
    assert page.wait_js("!!document.querySelector('[data-st=\"Downloads\"]')")
    page.js("document.querySelector('[data-st=\"Downloads\"]').click()")
    assert page.wait_js("!!document.getElementById('setConnections')")
    assert page.js("document.getElementById('setConnections').value") == "4"
    page.js("document.getElementById('setConnections').value = '2'")
    page.js("document.getElementById('setConnSave').click()")
    assert page.wait_js("settingsCache.connections_per_download === 2")
    assert mgr.settings.connections_per_download == 2


def test_reconnect_resyncs_events_missed_while_disconnected(ui, server, wait):
    mgr, page = ui()
    # Drop the socket and add a download before it reconnects (~1 s later):
    # its "added" event goes nowhere, so only a re-sync can show it.
    page.js("(ws.onclose = () => { ui.wsConnected = false; setTimeout(connectWS, 1000); }, ws.close(), true)")
    assert page.wait_js("ui.wsConnected === false")
    task = mgr.tasks[mgr.add_download(f"{server}/missed?{SLOW}", "missed.bin")]
    assert page.wait_js("ui.wsConnected === true")
    assert page.wait_js(f"!!document.querySelector('.row[data-id=\"{task.id}\"]')")


def test_closing_the_player_stops_its_polling(ui, wait, monkeypatch, tmp_path):
    # The poll used to outlive the modal and start playback on the detached
    # <video> once the transcode finished.
    from app.engine.models import DownloadTask
    from app.media import transcode

    monkeypatch.setattr(transcode, "probe_media", lambda path: (True, 60.0))
    monkeypatch.setattr(transcode, "get_or_build", lambda *a, **k: None)  # stays "pending"
    mgr, page = ui()
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 64)
    task = DownloadTask(url="http://127.0.0.1:1/clip.mp4", dest_path=str(clip), filename="clip.mp4",
                        status=S.COMPLETED)
    mgr.storage.save(task)
    mgr._poll_external_changes()
    assert page.wait_js(f"downloads.some(d => d.id === '{task.id}')")
    page.js("(window._polls = 0, window._fetch = window.fetch, window.fetch = (u, o) =>"
            " (String(u).includes('transcode-status') && window._polls++, window._fetch(u, o)), true)")
    page.js(f"(setView('library'), openLibPlayer('{task.id}'), true)")
    assert page.wait_js("window._polls >= 2", timeout=10)
    page.js("(closeLibPlayer(), true)")
    after_close = page.js("window._polls")
    wait(lambda: False, timeout=5)
    assert page.js("window._polls") <= after_close + 1  # at most one already in flight
