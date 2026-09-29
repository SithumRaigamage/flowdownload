"""DownloadManager scheduling and lifecycle, end to end against the local server."""
from __future__ import annotations

import os
import time

from app.engine import manager as manager_mod
from app.engine.manager import MAX_PER_DOMAIN
from app.engine.paths import domain_key, strip_dedupe_suffix
from app.engine.models import DownloadStatus as S
from app.engine.models import DownloadTask
from tests.conftest import DATA

SLOW = "delay=0.05"  # ~0.6 s per file: slow enough to pause/cancel mid-download


def test_domain_key_groups_subdomains():
    assert domain_key("https://c7.trafficdeposit.com/x") == "trafficdeposit.com"
    assert domain_key("https://C10.TrafficDeposit.com/y") == "trafficdeposit.com"
    assert domain_key("http://192.168.1.5:8080/a") == "192.168.1.5"
    assert domain_key("http://localhost/a") == "localhost"


def test_strip_dedupe_suffix():
    assert strip_dedupe_suffix("movie (2).mp4") == "movie.mp4"
    assert strip_dedupe_suffix("archive (10)") == "archive"
    assert strip_dedupe_suffix("movie.mp4") == "movie.mp4"
    assert strip_dedupe_suffix("a (b).mp4") == "a (b).mp4"


def test_dispatch_respects_per_domain_limit(make_manager):
    mgr = make_manager(max_concurrent_downloads=40)
    spawned = []

    def fake_spawn(task_id):
        mgr.tasks[task_id].status = S.DOWNLOADING
        spawned.append(task_id)

    mgr._spawn = fake_spawn
    urls = [f"https://a{i % 2}.example.com/f{i}" for i in range(MAX_PER_DOMAIN + 4)] + ["https://other.test/1", "https://other.test/2"]
    for i, url in enumerate(urls):
        task = DownloadTask(url=url, dest_path=f"/tmp/fd-test-{i}")
        task.created_at = time.time() + i
        mgr.tasks[task.id] = task
    mgr._dispatch_queue()

    def active(domain):
        return sum(1 for t in mgr.tasks.values() if t.status == S.DOWNLOADING and domain_key(t.url) == domain)

    assert active("example.com") == MAX_PER_DOMAIN
    assert active("other.test") == 2
    mgr.tasks[spawned[0]].status = S.COMPLETED
    mgr._dispatch_queue()
    assert active("example.com") == MAX_PER_DOMAIN
    assert len(spawned) == MAX_PER_DOMAIN + 2 + 1


def test_manual_start_queues_when_domain_full(make_manager):
    mgr = make_manager()
    mgr._spawn = lambda task_id: None
    for i in range(MAX_PER_DOMAIN):
        task = DownloadTask(url=f"https://x.example.com/{i}", dest_path=f"/tmp/fd-a{i}", status=S.DOWNLOADING)
        mgr.tasks[task.id] = task
    extra = DownloadTask(url="https://y.example.com/z", dest_path="/tmp/fd-b", status=S.PAUSED)
    mgr.tasks[extra.id] = extra
    mgr.start(extra.id)
    assert extra.status == S.QUEUED


def test_auto_folder_by_type(make_manager, server, wait, tmp_path):
    mgr = make_manager(auto_folder_by_type=True)
    task = mgr.tasks[mgr.add_download(f"{server}/movie.mp4", "movie.mp4")]
    assert task.dest_path == str(tmp_path / "dl" / "Video" / "movie.mp4")
    assert wait(lambda: task.status == S.COMPLETED)
    assert open(task.dest_path, "rb").read() == DATA


def test_resume_right_after_pause_is_not_ignored(make_manager, server, wait):
    mgr = make_manager()
    spawns = []
    real_spawn = mgr._spawn
    mgr._spawn = lambda task_id: (spawns.append(task_id), real_spawn(task_id))
    task = mgr.tasks[mgr.add_download(f"{server}/f?{SLOW}", "f.bin")]
    assert wait(lambda: task.downloaded_bytes > 0)
    mgr.pause(task.id)
    mgr.start(task.id)  # old worker is still unwinding at this point
    assert task.status == S.QUEUED
    assert wait(lambda: task.status == S.COMPLETED)
    assert len(spawns) == 2  # original + resumed, never overlapping
    assert open(task.dest_path, "rb").read() == DATA


def test_resume_all_skips_cancelled(make_manager):
    mgr = make_manager(max_concurrent_downloads=1)
    mgr._spawn = lambda task_id: None
    tasks = {}
    for status in (S.PAUSED, S.FAILED, S.CANCELLED):
        task = DownloadTask(url=f"https://e.test/{status.value}", dest_path=f"/tmp/fd-{status.value}", status=status)
        mgr.tasks[task.id] = task
        tasks[status] = task
    mgr.resume_all()
    assert tasks[S.PAUSED].status == S.QUEUED
    assert tasks[S.FAILED].status == S.QUEUED
    assert tasks[S.CANCELLED].status == S.CANCELLED


def test_cancel_deletes_partial_file(make_manager, server, wait):
    mgr = make_manager()
    task = mgr.tasks[mgr.add_download(f"{server}/f?{SLOW}", "f.bin")]
    assert wait(lambda: task.downloaded_bytes > 0)
    mgr.cancel(task.id)
    assert wait(lambda: task.status == S.CANCELLED)
    assert not os.path.exists(task.part_path)
    assert task.downloaded_bytes == 0


def test_quit_keeps_partial_and_relaunch_resumes(make_manager, server, wait):
    mgr = make_manager()
    running = mgr.tasks[mgr.add_download(f"{server}/run?{SLOW}", "run.bin")]
    paused = mgr.tasks[mgr.add_download(f"{server}/usr?{SLOW}", "usr.bin")]
    assert wait(lambda: running.downloaded_bytes > 0 and paused.downloaded_bytes > 0)
    mgr.pause(paused.id)
    assert wait(lambda: paused.id not in mgr._workers)
    mgr.shutdown()
    wait(lambda: False, timeout=0.3)  # let late worker signals arrive, as a real quit might
    assert os.path.exists(running.part_path)

    relaunched = make_manager()  # same DB file
    again = relaunched.tasks[running.id]
    assert relaunched.tasks[paused.id].status == S.PAUSED  # user pause is respected
    assert wait(lambda: again.status == S.COMPLETED)
    assert open(again.dest_path, "rb").read() == DATA


def test_overwrite_keeps_old_file_until_new_download_succeeds(make_manager, server, wait, tmp_path):
    mgr = make_manager(duplicate_policy="overwrite")
    old = DownloadTask(url=f"{server}/o?status=404", dest_path=str(tmp_path / "dl" / "o.bin"), status=S.COMPLETED)
    os.makedirs(os.path.dirname(old.dest_path))
    with open(old.dest_path, "wb") as f:
        f.write(b"OLD")
    mgr.storage.save(old)
    mgr.tasks[old.id] = old

    failed = mgr.tasks[mgr.add_download(old.url, "o.bin")]
    assert failed.dest_path == old.dest_path
    assert wait(lambda: failed.status == S.FAILED)
    assert open(old.dest_path, "rb").read() == b"OLD"

    failed.status, failed.url = S.COMPLETED, f"{server}/o"
    mgr.storage.save(failed)
    ok = mgr.tasks[mgr.add_download(failed.url, "o.bin")]
    assert ok.dest_path == old.dest_path
    assert wait(lambda: ok.status == S.COMPLETED)
    assert open(old.dest_path, "rb").read() == DATA


def test_progress_db_writes_are_throttled(make_manager, server, wait, monkeypatch):
    mgr = make_manager()
    saves = []
    real_save = mgr.storage.save
    monkeypatch.setattr(mgr.storage, "save", lambda task: (saves.append(task.status), real_save(task)))
    ticks = []
    mgr.task_updated.connect(ticks.append)
    task = mgr.tasks[mgr.add_download(f"{server}/f?delay=0.1", "f.bin")]  # ~1.3 s
    assert wait(lambda: task.status == S.COMPLETED)
    progress_ticks = len(ticks)
    assert len(saves) < progress_ticks
    assert mgr.storage.load_all()[0].downloaded_bytes == len(DATA)
    assert manager_mod.PROGRESS_SAVE_INTERVAL >= 1.0
