"""The Chrome native-messaging host's download hand-off (no Chrome involved)."""
from __future__ import annotations

import json
import os
import sys

import pytest

from app.engine.models import DownloadStatus as S
from app.engine.storage import Storage

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
