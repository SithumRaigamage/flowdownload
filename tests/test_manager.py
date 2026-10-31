"""DownloadManager scheduling and lifecycle, end to end against the local server."""
from __future__ import annotations

import os
import time

from app.engine import manager as manager_mod
from app.engine.models import DownloadStatus as S
from app.engine.models import DownloadTask
from app.engine.paths import domain_key, strip_dedupe_suffix
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


LIMIT = 10


def test_site_limit_never_queues(make_manager):
    # A site past its connection budget still starts every task that
    # "Downloads at once" allows -- the extras just get 1 connection each.
    mgr = make_manager(max_concurrent_downloads=40, site_connection_limit=LIMIT)
    started = []
    mgr._spawn = lambda task_id: (setattr(mgr.tasks[task_id], "status", S.DOWNLOADING), started.append(task_id))
    for i in range(LIMIT + 4):
        task = DownloadTask(url=f"https://a{i % 2}.example.com/f{i}", dest_path=f"/tmp/fd-test-{i}")
        task.created_at = time.time() + i
        mgr.tasks[task.id] = task
    mgr._dispatch_queue()
    assert len(started) == LIMIT + 4


def test_manual_start_runs_when_domain_full(make_manager):
    mgr = make_manager(site_connection_limit=LIMIT, max_concurrent_downloads=LIMIT + 1)
    started = []
    mgr._spawn = started.append
    for i in range(LIMIT):
        task = DownloadTask(url=f"https://x.example.com/{i}", dest_path=f"/tmp/fd-a{i}", status=S.DOWNLOADING)
        mgr.tasks[task.id] = task
    extra = DownloadTask(url="https://y.example.com/z", dest_path="/tmp/fd-b", status=S.PAUSED)
    mgr.tasks[extra.id] = extra
    mgr.start(extra.id)
    assert started == [extra.id]


def test_downloads_at_once_alone_decides_by_default(make_manager):
    # One site (v1..v10.cdnde.com) and 30 slots: all 30 run, no hidden cap.
    mgr = make_manager(max_concurrent_downloads=30)
    mgr._spawn = lambda task_id: setattr(mgr.tasks[task_id], "status", S.DOWNLOADING)
    for i in range(35):
        task = DownloadTask(url=f"https://v{i % 10 + 1}.cdnde.com/f{i}", dest_path=f"/tmp/fd-all-{i}")
        task.created_at = time.time() + i
        mgr.tasks[task.id] = task
    mgr._dispatch_queue()
    assert sum(1 for t in mgr.tasks.values() if t.status == S.DOWNLOADING) == 30


def test_expiring_links_are_not_held_in_the_queue(make_manager):
    # Signed links (expiry in the path) once waited for the site's measured
    # speed, so a batch trickled out one at a time despite free slots.
    mgr = make_manager(max_concurrent_downloads=30, site_connection_limit=15)
    mgr._spawn = lambda task_id: setattr(mgr.tasks[task_id], "status", S.DOWNLOADING)
    expiry = int(time.time()) + 3000
    for i in range(5):
        task = DownloadTask(url=f"https://s{i}.trafficdeposit.com/vidi/sig/tok/{expiry}/{i}/x.vid",
                            dest_path=f"/tmp/fd-exp-{i}")
        task.created_at = time.time() + i
        mgr.tasks[task.id] = task
    mgr._dispatch_queue()
    assert all(t.status == S.DOWNLOADING for t in mgr.tasks.values())


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


def test_pause_sticks_for_queued_tasks(make_manager):
    mgr = make_manager(max_concurrent_downloads=1)
    mgr._spawn = lambda task_id: setattr(mgr.tasks[task_id], "status", S.DOWNLOADING)
    running = DownloadTask(url="https://e.test/a", dest_path="/tmp/fd-pq-a", status=S.DOWNLOADING)
    queued = DownloadTask(url="https://e.test/b", dest_path="/tmp/fd-pq-b", status=S.QUEUED)
    for task in (running, queued):
        mgr.tasks[task.id] = task
    mgr.pause(running.id)
    mgr.pause(queued.id)
    mgr._dispatch_queue()  # what the freed slot triggers once the worker exits
    assert running.status == S.PAUSED
    assert queued.status == S.PAUSED


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


def test_compressing_server_still_yields_the_exact_file(make_manager, server, wait):
    # Content-Length and byte ranges count the encoded body; asking for
    # gzip would hand us decoded bytes that never match either.
    mgr = make_manager(connections_per_download=1)
    task = mgr.tasks[mgr.add_download(f"{server}/f?gzip=1&delay=0", "f.bin")]
    assert wait(lambda: task.status in (S.COMPLETED, S.FAILED))
    assert task.status == S.COMPLETED, task.error_message
    assert open(task.dest_path, "rb").read() == DATA


def test_removing_a_running_download_does_not_block(make_manager, server, wait):
    # remove() runs on the Qt main thread; waiting there for a worker stuck
    # in a slow read froze the UI (and every API call) for seconds per row.
    mgr = make_manager(connections_per_download=1)
    task = mgr.tasks[mgr.add_download(f"{server}/f?delay=3", "f.bin")]
    assert wait(lambda: os.path.exists(task.part_path) and task.downloaded_bytes > 0)
    started = time.monotonic()
    mgr.remove(task.id)
    assert time.monotonic() - started < 0.5
    assert task.id not in mgr.tasks and not mgr.storage.load_all()
    assert wait(lambda: not os.path.exists(task.part_path))  # cleaned up once the worker exits


def test_completed_downloads_drop_their_cookies(make_manager, server, wait):
    mgr = make_manager()
    task = DownloadTask(url=f"{server}/f?delay=0", dest_path=os.path.join(mgr.settings.download_dir, "c.bin"),
                        filename="c.bin", cookie_header="sid=secret")
    mgr.storage.save(task)  # as the Chrome native host queues it
    mgr._poll_external_changes()
    task = mgr.tasks[task.id]
    assert wait(lambda: task.status == S.COMPLETED)
    assert task.cookie_header == ""
    assert mgr.storage.load_all()[0].cookie_header == ""


def test_filenames_cannot_name_the_folder_or_its_parent(make_manager):
    from app.engine.paths import safe_filename

    assert safe_filename("..") == "download"
    assert safe_filename(".") == "download"
    assert safe_filename("../../x/.bashrc") == "bashrc"
    assert safe_filename("a\x00b\nc.txt") == "abc.txt"
    mgr = make_manager()
    task = mgr.tasks[mgr.add_download("http://127.0.0.1:1/x", "..")]
    assert os.path.dirname(task.dest_path) == mgr.settings.download_dir
    assert task.filename == "download"


def test_resumable_means_the_server_supports_ranges(make_manager, server, wait):
    from app.api.schemas import DownloadOut

    mgr = make_manager(connections_per_download=1)
    yes = mgr.tasks[mgr.add_download(f"{server}/r?delay=0", "r.bin")]
    no = mgr.tasks[mgr.add_download(f"{server}/n?delay=0&noranges=1", "n.bin")]  # ETag, but no ranges
    assert wait(lambda: yes.status == S.COMPLETED and no.status == S.COMPLETED)
    assert DownloadOut.from_task(yes).accept_ranges is True
    assert DownloadOut.from_task(no).accept_ranges is False
    assert {t.id: t.accept_ranges for t in mgr.storage.load_all()} == {yes.id: True, no.id: False}


def test_browser_cookies_survive_redirects_but_server_cookies_stay_per_task(make_manager, server, wait):
    from tests.conftest import REQUESTS

    REQUESTS.clear()
    mgr = make_manager(connections_per_download=1)
    first = DownloadTask(url=f"{server}/start?redirect=/landed%3Fsetcookie%3D1%26delay%3D0",
                         dest_path=os.path.join(mgr.settings.download_dir, "a.bin"), filename="a.bin",
                         cookie_header="sid=abc")
    mgr.storage.save(first)
    mgr._poll_external_changes()
    assert wait(lambda: mgr.tasks[first.id].status == S.COMPLETED)
    landed = [h for p, h in REQUESTS if p.startswith("/landed")]
    assert landed and "sid=abc" in landed[0].get("Cookie", "")  # kept across the redirect

    second = mgr.tasks[mgr.add_download(f"{server}/other?delay=0", "b.bin")]
    assert wait(lambda: second.status == S.COMPLETED)
    other = [h for p, h in REQUESTS if p.startswith("/other")]
    assert other and "leak" not in other[0].get("Cookie", "")  # first task's server cookie
