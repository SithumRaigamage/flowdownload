"""The Chrome native-messaging host's download hand-off (no Chrome involved)."""
from __future__ import annotations

import json
import os
import sys

import pytest

from app.engine.models import DownloadStatus as S
from app.engine.models import DownloadTask
from app.engine.storage import Storage
from tests.conftest import DATA

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "native_host"))
import flowdownload_host as host  # noqa: E402


@pytest.fixture
def host_env(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "APP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(host, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(host, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(host, "launch_app_if_needed", lambda: None)
    monkeypatch.setattr(host, "_write_extension_state", lambda *a: None)

    def configure(**settings):
        settings.setdefault("download_dir", str(tmp_path / "dl"))
        (tmp_path / "settings.json").write_text(json.dumps(settings))
        return tmp_path

    return configure


def test_auto_folder_path_has_filename_once(host_env):
    root = host_env(auto_folder_by_type=True)
    assert host.handle_message({"url": "https://example.com/a/movie.mp4", "filename": "movie.mp4"})["ok"]
    (task,) = Storage(host.DB_PATH).load_all()
    assert task.dest_path == str(root / "dl" / "Video" / "movie.mp4")


def test_overwrite_keeps_old_file_and_reuses_path(host_env):
    host_env(duplicate_policy="overwrite")
    msg = {"url": "https://example.com/a/movie.mp4", "filename": "movie.mp4"}
    host.handle_message(msg)
    storage = Storage(host.DB_PATH)
    (first,) = storage.load_all()
    os.makedirs(os.path.dirname(first.dest_path))
    with open(first.dest_path, "w") as f:
        f.write("OLD")
    first.status = S.COMPLETED
    storage.save(first)

    assert host.handle_message(msg)["ok"]
    (second,) = Storage(host.DB_PATH).load_all()
    assert second.id != first.id
    assert second.dest_path == first.dest_path
    assert open(first.dest_path).read() == "OLD"


def test_rejects_non_http_urls(host_env):
    host_env()
    assert host.handle_message({"url": "file:///etc/passwd"})["ok"] is False


# ---- auto-match: a fresh link for an unfinished download -------------------

def stalled(storage, tmp_path, url, filename, etag="", total=0, status=S.FAILED):
    """An unfinished download row with partial data on disk."""
    task = DownloadTask(url=url, dest_path=str(tmp_path / "dl" / filename), filename=filename,
                        status=status, etag=etag, total_bytes=total)
    os.makedirs(os.path.dirname(task.dest_path), exist_ok=True)
    with open(task.part_path, "wb") as f:
        f.write(b"x" * 1000)
    storage.save(task)
    return task


def test_fresh_link_with_same_filename_refreshes_instead_of_duplicating(host_env):
    root = host_env()
    storage = Storage(host.DB_PATH)
    old = stalled(storage, root, "https://c7.cdn.test/tok1/movie.mp4", "movie.mp4")
    response = host.handle_message({"url": "https://c9.cdn.test/tok2/movie.mp4", "cookie": "s=1",
                                    "referrer": "https://page.test/"})
    assert response == {"ok": True, "filename": "movie.mp4", "refreshed": True}
    assert [t.id for t in Storage(host.DB_PATH).load_all()] == [old.id]  # no new task
    assert storage.pop_refreshes() == [(old.id, "https://c9.cdn.test/tok2/movie.mp4", "s=1", "https://page.test/")]


def test_dedupe_suffix_is_ignored_when_matching(host_env):
    root = host_env()
    old = stalled(Storage(host.DB_PATH), root, "https://cdn.test/a/movie.mp4", "movie (1).mp4")
    assert host.handle_message({"url": "https://cdn.test/b/movie.mp4"}).get("refreshed")
    assert Storage(host.DB_PATH).pop_refreshes()[0][0] == old.id


def test_renamed_link_is_matched_by_etag(host_env, server):
    root = host_env()
    storage = Storage(host.DB_PATH)
    stalled(storage, root, "http://127.0.0.1:1/old/a.bin", "a.bin", etag='"other"')
    target = stalled(storage, root, "http://127.0.0.1:1/old/b.bin", "b.bin", etag='"v7"')
    # Different path (so a different filename) but the server reports ETag "v7".
    response = host.handle_message({"url": f"{server}/signed/xyz?etag=v7"})
    assert response.get("refreshed") and response["filename"] == "b.bin"
    assert storage.pop_refreshes()[0][0] == target.id


def test_renamed_link_is_matched_by_size_when_etags_differ(host_env, server):
    root = host_env()
    storage = Storage(host.DB_PATH)
    stalled(storage, root, "http://127.0.0.1:1/old/a.bin", "a.bin", total=123)
    target = stalled(storage, root, "http://127.0.0.1:1/old/b.bin", "b.bin", total=len(DATA))
    assert host.handle_message({"url": f"{server}/signed/xyz"}).get("refreshed")
    assert storage.pop_refreshes()[0][0] == target.id


def test_ambiguous_match_queues_a_new_download(host_env):
    root = host_env()
    storage = Storage(host.DB_PATH)
    stalled(storage, root, "https://cdn.test/a/movie.mp4", "movie.mp4")
    stalled(storage, root, "https://cdn.test/b/movie.mp4", "movie (1).mp4")
    response = host.handle_message({"url": "https://cdn.test/c/movie.mp4"})
    assert "refreshed" not in response
    assert len(Storage(host.DB_PATH).load_all()) == 3
    assert storage.pop_refreshes() == []


def test_other_sites_and_finished_downloads_never_match(host_env):
    root = host_env()
    storage = Storage(host.DB_PATH)
    stalled(storage, root, "https://other.test/a/movie.mp4", "movie.mp4")
    stalled(storage, root, "https://cdn.test/a/clip.mp4", "clip.mp4", status=S.COMPLETED)
    assert "refreshed" not in host.handle_message({"url": "https://cdn.test/b/movie.mp4"})
    assert "refreshed" not in host.handle_message({"url": "https://cdn.test/b/clip.mp4"})


def test_same_name_but_different_size_is_a_new_download(host_env, server):
    # Sites reuse generic names; a name match alone used to hijack an
    # unrelated stalled download instead of queueing this one.
    root = host_env()
    storage = Storage(host.DB_PATH)
    stalled(storage, root, "http://127.0.0.1:1/old/video.mp4", "video.mp4", total=123)
    response = host.handle_message({"url": f"{server}/new/video.mp4"})
    assert "refreshed" not in response
    assert len(Storage(host.DB_PATH).load_all()) == 2
    assert storage.pop_refreshes() == []


def test_setup_registers_a_launcher_that_runs_the_host(tmp_path, monkeypatch):
    # The host's shebang used to hard-code one machine's venv path.
    import importlib.util
    import struct
    import subprocess

    spec = importlib.util.spec_from_file_location(
        "setup_chrome", os.path.join(os.path.dirname(__file__), "..", "scripts", "setup_chrome_integration.py"))
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    monkeypatch.setattr(setup, "KEY_DIR", str(tmp_path))
    monkeypatch.setattr(setup, "SOURCE_LAUNCHER", str(tmp_path / "flowdownload-host"))
    monkeypatch.setattr(setup, "APP_BUNDLE_CANDIDATES", (str(tmp_path / "missing.app"),))
    launcher = setup.host_executable(None)
    assert launcher == str(tmp_path / "flowdownload-host")
    ping = json.dumps({"action": "ping"}).encode()
    proc = subprocess.run([launcher, "chrome-extension://x/"], input=struct.pack("<I", len(ping)) + ping,
                          capture_output=True, env={**os.environ, "HOME": str(tmp_path)}, timeout=30)
    assert proc.stdout[4:] == b'{"ok": true}', proc.stderr.decode()
