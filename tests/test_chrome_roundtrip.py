"""Chrome hand-off end to end, minus Chrome itself:

extension (background.js under Node with a fake `chrome` API)
  -> Chrome native-messaging framing on stdin
  -> the real native host process
  -> a queued row in the database.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys

import pytest

from app.engine.instance_lock import InstanceLock
from app.engine.storage import Storage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is needed to run the extension's background.js")


def run_extension(*events) -> dict:
    out = subprocess.run(
        [NODE, os.path.join(ROOT, "tests", "chrome_harness.js"), json.dumps(list(events))],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(out.stdout)


def call_host(home, message: dict) -> dict:
    """Talk to the native host exactly as Chrome does: 4-byte little-endian
    length prefix + UTF-8 JSON, both ways."""
    payload = json.dumps(message).encode()
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "native_host", "flowdownload_host.py")],
        input=struct.pack("<I", len(payload)) + payload,
        capture_output=True, env={**os.environ, "HOME": str(home)}, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    (length,) = struct.unpack("<I", proc.stdout[:4])
    return json.loads(proc.stdout[4:4 + length])


@pytest.fixture
def home(tmp_path):
    # Holding the app's instance lock makes the host believe FlowDownload is
    # already running, so it queues the row without launching the real app.
    lock = InstanceLock(str(tmp_path / ".flowdownload" / "app.pid"))
    assert lock.acquire()
    yield tmp_path
    lock.release()


def test_extension_reroutes_chrome_downloads():
    out = run_extension(
        {"download": {"id": 7, "url": "https://cdn.example.com/a.mp4"}},
        {"download": {"id": 8, "url": "blob:https://example.com/1"}},  # page-generated: left to Chrome
    )
    assert out["cancelled"] == [7] and out["erased"] == [7]
    (sent,) = out["native"]
    assert sent["host"] == "com.flowdownload.host"
    assert sent["msg"]["url"] == "https://cdn.example.com/a.mp4"
    assert sent["msg"]["cookie"] == "sid=abc; t=1"
    assert sent["msg"]["profile_id"]


def test_chrome_keeps_the_download_until_the_host_accepts_it():
    out = run_extension({"download": {"id": 7, "url": "https://cdn.example.com/a.mp4"}})
    assert out["paused"] == [7] and out["resumed"] == []


@pytest.mark.parametrize("host", ["unreachable", "error"])
def test_failed_hand_off_resumes_the_chrome_download(host):
    # Erasing Chrome's copy before the host confirmed meant a broken
    # hand-off lost the download from both places.
    out = run_extension({"host": host}, {"download": {"id": 7, "url": "https://cdn.example.com/a.mp4"}})
    assert out["paused"] == [7] and out["resumed"] == [7]
    assert out["cancelled"] == [] and out["erased"] == []
    assert out["notifications"] == ["FlowDownload error"]


def test_interception_can_be_switched_off():
    out = run_extension(
        {"storage": {"flowdownload_intercept": {"enabled": False}}},
        {"download": {"id": 7, "url": "https://cdn.example.com/a.mp4"}},
        {"menu": {"linkUrl": "https://cdn.example.com/b.zip"}},  # explicit request: still sent
    )
    assert out["paused"] == [] and out["cancelled"] == []
    assert [s["msg"]["url"] for s in out["native"]] == ["https://cdn.example.com/b.zip"]


def test_excluded_sites_stay_in_chrome():
    excluded = {"flowdownload_intercept": {"enabled": True, "excluded": ["example.com", "https://www.other.test/page"]}}
    out = run_extension(
        {"storage": excluded},
        {"download": {"id": 1, "url": "https://example.com/a.zip"}},
        {"download": {"id": 2, "url": "https://cdn.example.com/b.zip"}},
        {"download": {"id": 3, "url": "https://other.test/c.zip"}},
        {"download": {"id": 4, "url": "https://notexample.com/d.zip"}},  # not a subdomain
    )
    assert out["paused"] == [4]
    assert [s["msg"]["url"] for s in out["native"]] == ["https://notexample.com/d.zip"]


def test_heartbeat_uses_an_alarm():
    # setInterval dies when Chrome suspends the idle service worker.
    assert run_extension()["alarms"] == [{"name": "flowdownload-ping", "periodInMinutes": 0.5}]


def test_download_reaches_the_database(home):
    (sent,) = run_extension({"download": {"id": 1, "url": "https://cdn.example.com/dir/movie.mp4"}})["native"]
    response = call_host(home, sent["msg"])
    assert response == {"ok": True, "filename": "movie.mp4"}
    (task,) = Storage(str(home / ".flowdownload" / "downloads.db")).load_all()
    assert task.url == "https://cdn.example.com/dir/movie.mp4"
    assert task.status.value == "queued"
    assert task.cookie_header == "sid=abc; t=1"
    assert task.dest_path == str(home / "Downloads" / "movie.mp4")


def test_context_menu_download_reaches_the_database(home):
    (sent,) = run_extension({"menu": {"linkUrl": "https://example.com/b.zip", "pageUrl": "https://site.test/p"}})["native"]
    assert call_host(home, sent["msg"])["ok"]
    (task,) = Storage(str(home / ".flowdownload" / "downloads.db")).load_all()
    assert task.filename == "b.zip"


def test_referrer_flows_from_chrome_to_the_database(home):
    out = run_extension(
        {"download": {"id": 1, "url": "https://cdn.example.com/a.mp4", "referrer": "https://site.test/watch/1"}},
        {"menu": {"linkUrl": "https://cdn.example.com/b.mp4", "pageUrl": "https://site.test/list"}},
    )
    for sent in out["native"]:
        assert call_host(home, sent["msg"])["ok"]
    rows = {t.filename: t.referrer for t in Storage(str(home / ".flowdownload" / "downloads.db")).load_all()}
    assert rows == {"a.mp4": "https://site.test/watch/1", "b.mp4": "https://site.test/list"}


def test_host_drops_non_http_referrer(home):
    assert call_host(home, {"url": "https://cdn.example.com/c.mp4", "referrer": "chrome://newtab/"})["ok"]
    (task,) = Storage(str(home / ".flowdownload" / "downloads.db")).load_all()
    assert task.referrer == ""


def test_host_rejects_unsafe_urls(home):
    assert call_host(home, {"url": "http://169.254.169.254/latest/meta-data"})["ok"] is False
