"""Split-file (multi-connection) downloads and the per-site connection budget."""
from __future__ import annotations

import json
import os

import pytest

from app.engine import downloader, segmented
from app.engine.models import DownloadStatus as S
from tests.conftest import CONNECTIONS, DATA, ETAG_OVERRIDE, REQUESTS

SLOW = "delay=0.3"  # ~1.2 s per 100 KB segment: slow enough to pause mid-way


@pytest.fixture(autouse=True)
def small_thresholds(monkeypatch):
    # DATA is 400 KB: split anything >= 100 KB, into pieces >= 50 KB.
    monkeypatch.setattr(segmented, "MIN_TOTAL", 100_000)
    monkeypatch.setattr(segmented, "MIN_PART", 50_000)
    monkeypatch.setattr(downloader, "compute_retry_delay", lambda attempt: None if attempt > 5 else 0.01)
    REQUESTS.clear()
    ETAG_OVERRIDE.clear()
    CONNECTIONS["peak"] = 0


def ranges(path):
    return [h.get("Range") for p, h in REQUESTS if p.startswith(path)]


def test_large_file_downloads_over_parallel_connections(make_manager, server, wait):
    mgr = make_manager(connections_per_download=4)
    task = mgr.tasks[mgr.add_download(f"{server}/big?delay=0.02", "big.bin")]
    assert wait(lambda: task.connections == 4)
    assert wait(lambda: task.status == S.COMPLETED)
    assert open(task.dest_path, "rb").read() == DATA
    assert CONNECTIONS["peak"] >= 3
    bounded = {r for r in ranges("/big") if r and not r.endswith("-")}
    # The initial four-way split; on a slow machine an idle connection may
    # also steal half of a lagging piece, adding an extra range.
    assert {"bytes=0-99999", "bytes=100000-199999", "bytes=200000-299999", "bytes=300000-399999"} <= bounded
    assert not os.path.exists(task.segments_path)
    assert task.connections == 0  # budget released once finished


def test_pause_and_relaunch_resume_from_the_sidecar(make_manager, server, wait):
    mgr = make_manager(connections_per_download=4)
    task = mgr.tasks[mgr.add_download(f"{server}/big?{SLOW}", "big.bin")]
    assert wait(lambda: task.downloaded_bytes > 100_000)
    mgr.pause(task.id)
    assert wait(lambda: task.id not in mgr._workers)
    assert os.path.getsize(task.part_path) == len(DATA)  # preallocated
    saved = sum(done for _, _, done in json.load(open(task.segments_path))["segments"])
    assert 0 < saved < len(DATA)
    mgr.shutdown()

    relaunched = make_manager(connections_per_download=4)
    again = relaunched.tasks[task.id]
    assert again.downloaded_bytes == saved  # from the sidecar, not the full-size .part
    REQUESTS.clear()
    relaunched.start(again.id)
    assert wait(lambda: again.status == S.COMPLETED)
    assert open(again.dest_path, "rb").read() == DATA
    resumed = ranges("/big")
    assert "bytes=0-" not in resumed  # no fresh probe: it picked up the saved segments
    assert all(not r.startswith("bytes=0-") for r in resumed)


def test_segments_recover_from_dropped_connections(make_manager, server, wait):
    mgr = make_manager(connections_per_download=4)
    # Drops after 70 KB: more than one 64 KB read, so every connection
    # delivers something (a drop mid-read loses that partial read).
    task = mgr.tasks[mgr.add_download(f"{server}/flaky?drop=70000", "flaky.bin")]
    assert wait(lambda: task.status == S.COMPLETED, timeout=30)
    assert open(task.dest_path, "rb").read() == DATA
    assert len(ranges("/flaky")) > 5  # every 100 KB segment needed a reconnect


def test_file_changing_mid_download_falls_back_to_single_stream(make_manager, server, wait):
    mgr = make_manager(connections_per_download=4)
    task = mgr.tasks[mgr.add_download(f"{server}/chg?drop=70000&delay=0.1", "chg.bin")]
    assert wait(lambda: task.downloaded_bytes > 50_000)
    ETAG_OVERRIDE["/chg"] = "v2"  # every If-Range now misses -> 200 -> restart
    assert wait(lambda: task.status == S.COMPLETED, timeout=30)
    assert open(task.dest_path, "rb").read() == DATA
    assert not os.path.exists(task.segments_path)
    assert None in ranges("/chg")  # the single-stream restart: a plain GET from byte 0
    assert task.etag == '"v2"'


def test_connection_budget_is_shared_per_site(make_manager, server, wait):
    mgr = make_manager(connections_per_download=4, site_connection_limit=6)
    a = mgr.tasks[mgr.add_download(f"{server}/a?delay=0.2", "a.bin")]
    b = mgr.tasks[mgr.add_download(f"{server}/b?delay=0.2", "b.bin")]
    c = mgr.tasks[mgr.add_download(f"{server}/c?delay=0.2", "c.bin")]
    # 4 + 2 = the site's 6; c still starts, on a single connection
    assert wait(lambda: a.connections == 4 and b.connections == 2 and c.connections == 1)
    assert c.status == S.DOWNLOADING
    for t in (a, b, c):
        mgr.pause(t.id)


def test_unsplittable_download_releases_its_budget(make_manager, server, wait, monkeypatch):
    monkeypatch.setattr(segmented, "MIN_TOTAL", 10_000_000)  # nothing here is big enough to split
    mgr = make_manager(connections_per_download=4, max_concurrent_downloads=10)
    tasks = [mgr.tasks[mgr.add_download(f"{server}/{n}?delay=0.2", f"{n}.bin")] for n in "abcdef"]
    # Each is granted up to 4, finds the file too small, and drops to 1 --
    # so all six fit in the site's budget of 6 at once.
    assert wait(lambda: all(t.status == S.DOWNLOADING and t.connections == 1 for t in tasks))
    for t in tasks:
        mgr.pause(t.id)


def test_cancel_removes_part_and_sidecar(make_manager, server, wait):
    mgr = make_manager(connections_per_download=4)
    task = mgr.tasks[mgr.add_download(f"{server}/big?{SLOW}", "big.bin")]
    assert wait(lambda: os.path.exists(task.segments_path) and task.downloaded_bytes > 0)
    mgr.cancel(task.id)
    assert wait(lambda: task.status == S.CANCELLED)
    assert not os.path.exists(task.part_path) and not os.path.exists(task.segments_path)


def test_one_connection_setting_never_splits(make_manager, server, wait):
    mgr = make_manager(connections_per_download=1)
    task = mgr.tasks[mgr.add_download(f"{server}/one", "one.bin")]
    assert wait(lambda: task.status == S.COMPLETED)
    assert ranges("/one") == [None]  # a single plain GET, no probe


def test_misplaced_ranges_fall_back_to_a_clean_single_stream(make_manager, server, wait):
    # A 206 for the wrong offset was written at the requested one: a
    # silently scrambled file that still passed the size check.
    mgr = make_manager(connections_per_download=4)
    task = mgr.tasks[mgr.add_download(f"{server}/bad?badrange=1&delay=0", "bad.bin")]
    assert wait(lambda: task.status in (S.COMPLETED, S.FAILED))
    assert open(task.dest_path, "rb").read() == DATA


def test_idle_connections_split_the_remaining_work(make_manager, server, wait, monkeypatch):
    # Resuming with a single unfinished piece used to download it over one
    # connection, however many were granted.
    monkeypatch.setattr(segmented, "_split", lambda total, n: [segmented._Segment(0, total - 1)])
    mgr = make_manager(connections_per_download=4)
    task = mgr.tasks[mgr.add_download(f"{server}/one?delay=0.05", "one.bin")]
    assert wait(lambda: task.status == S.COMPLETED)
    assert open(task.dest_path, "rb").read() == DATA
    assert CONNECTIONS["peak"] >= 3
    assert len([r for r in ranges("/one") if r and not r.endswith("-")]) >= 3  # stolen tails


def test_split_download_reports_its_file_map(make_manager, server, wait):
    from app.api.schemas import DownloadOut

    mgr = make_manager(connections_per_download=4)
    task = mgr.tasks[mgr.add_download(f"{server}/map?{SLOW}", "map.bin")]
    assert wait(lambda: len(task.segments) == 4 and any(done for _, done, _ in task.segments))
    out = DownloadOut.from_task(task)
    assert [s[0] for s in out.segments] == [0, 100_000, 200_000, 300_000]  # sorted by offset
    assert all(0 <= done <= end - start + 1 for start, done, end in out.segments)
    mgr.pause(task.id)
    assert wait(lambda: task.status == S.PAUSED)
    relaunched = make_manager(db_name="t.db")  # same DB: the map comes back from the sidecar
    assert len(relaunched.tasks[task.id].segments) == 4
    assert wait(lambda: not mgr._workers)
