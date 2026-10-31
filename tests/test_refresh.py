"""Refresh link: resume an unfinished download from a new URL."""
from __future__ import annotations

import os

import pytest

from app.engine.models import DownloadStatus as S
from tests.conftest import DATA, REQUESTS

SLOW = "delay=0.05"


def paused_partial(mgr, server, wait, path="old"):
    """A download that got partway, then stopped (as an expired link would)."""
    task = mgr.tasks[mgr.add_download(f"{server}/{path}?{SLOW}", "movie.mp4")]
    assert wait(lambda: task.downloaded_bytes > 100_000)
    mgr.pause(task.id)
    assert wait(lambda: task.id not in mgr._workers)
    return task, os.path.getsize(task.part_path)


def ranges_sent(path):
    return [headers.get("Range") for p, headers in REQUESTS if p.startswith(path)]


def test_refresh_resumes_from_partial_data(make_manager, server, wait):
    mgr = make_manager()
    task, partial = paused_partial(mgr, server, wait)
    REQUESTS.clear()
    mgr.refresh_url(task.id, f"{server}/new")
    assert wait(lambda: task.status == S.COMPLETED)
    # Resumed, not restarted (after a probe, as the file might be split).
    assert ranges_sent("/new") == ["bytes=0-", f"bytes={partial}-"]
    assert open(task.dest_path, "rb").read() == DATA
    assert task.url == f"{server}/new"


def test_refresh_to_a_changed_file_restarts_cleanly(make_manager, server, wait):
    mgr = make_manager()
    task, _ = paused_partial(mgr, server, wait)
    mgr.refresh_url(task.id, f"{server}/new?etag=different")  # If-Range won't match
    assert wait(lambda: task.status == S.COMPLETED)
    assert open(task.dest_path, "rb").read() == DATA


def test_refresh_while_downloading_switches_url(make_manager, server, wait):
    mgr = make_manager()
    task = mgr.tasks[mgr.add_download(f"{server}/old?{SLOW}", "movie.mp4")]
    assert wait(lambda: task.downloaded_bytes > 0)
    REQUESTS.clear()
    mgr.refresh_url(task.id, f"{server}/new")
    assert wait(lambda: task.status == S.COMPLETED)
    assert ranges_sent("/new")  # the rest came from the new URL
    assert open(task.dest_path, "rb").read() == DATA


def test_refresh_rejects_completed_and_unsafe_urls(make_manager, server, wait):
    mgr = make_manager()
    task = mgr.tasks[mgr.add_download(f"{server}/done", "done.bin")]
    assert wait(lambda: task.status == S.COMPLETED)
    with pytest.raises(ValueError):
        mgr.refresh_url(task.id, f"{server}/other")
    paused, _ = paused_partial(mgr, server, wait)
    with pytest.raises(ValueError):
        mgr.refresh_url(paused.id, "file:///etc/passwd")


def test_pending_refresh_from_chrome_is_applied(make_manager, server, wait):
    mgr = make_manager()
    task, _ = paused_partial(mgr, server, wait)
    mgr.storage.add_refresh(task.id, f"{server}/fresh", "sid=1", "https://page.test/")
    mgr._poll_external_changes()  # what the 2 s timer does
    assert task.url == f"{server}/fresh"
    assert task.cookie_header == "sid=1" and task.referrer == "https://page.test/"
    assert wait(lambda: task.status == S.COMPLETED)
    assert mgr.storage.pop_refreshes() == []
