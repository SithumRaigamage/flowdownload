"""Named queues: their schedules, their own limits, and how the manager runs them."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.engine import downloader
from app.engine.models import DownloadStatus as S
from app.engine.queues import Queue, is_open, opens_at, validate_queue
from app.engine.storage import Storage
from tests.conftest import DATA

MON = datetime(2026, 11, 2)  # a Monday
NIGHT = Queue(name="Night", scheduled=True, start_minute=22 * 60, stop_minute=6 * 60, days=(0,))


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(downloader, "compute_retry_delay", lambda attempt: None if attempt > 5 else 0.01)


class _Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def test_a_daytime_window_is_open_only_between_its_times_on_its_days():
    q = Queue(name="Work", scheduled=True, start_minute=9 * 60, stop_minute=17 * 60, days=(0, 1))
    assert not is_open(q, MON.replace(hour=8, minute=59))
    assert is_open(q, MON.replace(hour=9))
    assert not is_open(q, MON.replace(hour=17))
    assert not is_open(q, MON.replace(day=4, hour=12))  # Wednesday
    assert is_open(Queue(name="Any"), MON)  # no schedule: always open


def test_an_overnight_window_belongs_to_the_day_it_started():
    assert is_open(NIGHT, MON.replace(hour=23))
    assert is_open(NIGHT, MON.replace(day=3, hour=5, minute=59))  # Tuesday morning, Monday's window
    assert not is_open(NIGHT, MON.replace(hour=5))  # Monday morning belongs to Sunday, not scheduled
    assert not is_open(NIGHT, MON.replace(day=3, hour=6))


def test_opens_at_skips_the_days_a_queue_doesnt_run():
    assert opens_at(NIGHT, MON.replace(day=3, hour=12)) == datetime(2026, 11, 9, 22, 0)
    assert opens_at(NIGHT, MON.replace(hour=21)) == MON.replace(hour=22)
    assert opens_at(NIGHT, MON.replace(hour=23)) is None


def test_validation_rejects_taken_names_and_empty_windows():
    with pytest.raises(ValueError, match="already a queue"):
        validate_queue(Queue(name=" night "), ["Night"])
    with pytest.raises(ValueError, match="already a queue"):
        validate_queue(Queue(name="main"), [])
    with pytest.raises(ValueError, match="same"):
        validate_queue(Queue(name="X", scheduled=True, start_minute=60, stop_minute=60), [])
    with pytest.raises(ValueError, match="at least one day"):
        validate_queue(Queue(name="X", scheduled=True, start_minute=60, stop_minute=120, days=()), [])
    assert validate_queue(Queue(name=" Night ", days=(3, 1, 1)), []).name == "Night"


def test_a_queue_runs_no_more_than_its_own_limit(make_manager, server, wait):
    mgr = make_manager(max_concurrent_downloads=5, connections_per_download=1)
    queue = mgr.add_queue(Queue(name="One at a time", max_concurrent=1))
    tasks = [mgr.tasks[mgr.add_download(f"{server}/q{i}?delay=0.02", f"q{i}.bin", queue_id=queue.id)]
             for i in range(3)]
    peak = 0

    def _done() -> bool:
        nonlocal peak
        peak = max(peak, sum(t.status == S.DOWNLOADING for t in tasks))
        return all(t.status == S.COMPLETED for t in tasks)

    assert wait(_done, timeout=30)
    assert peak == 1


def test_a_closed_queue_holds_its_downloads_until_its_window_opens(make_manager, server, wait):
    clock = _Clock(MON.replace(hour=21))
    mgr = make_manager(clock=clock)
    queue = mgr.add_queue(NIGHT)
    task = mgr.tasks[mgr.add_download(f"{server}/night", "night.bin", queue_id=queue.id)]
    mgr.start(task.id)  # Resume follows the queue's hours too
    assert not wait(lambda: task.status != S.QUEUED, timeout=0.5)
    assert mgr.queue_opens_at(queue.id) == MON.replace(hour=22)

    clock.now = MON.replace(hour=22)
    mgr._poll_external_changes()
    assert wait(lambda: task.status == S.COMPLETED)
    assert open(task.dest_path, "rb").read() == DATA


def test_downloads_wait_again_when_their_queue_closes_and_resume_when_it_reopens(make_manager, server, wait):
    clock = _Clock(MON.replace(hour=23))
    mgr = make_manager(clock=clock, connections_per_download=1)
    queue = mgr.add_queue(NIGHT)
    task = mgr.tasks[mgr.add_download(f"{server}/slow?delay=0.1", "slow.bin", queue_id=queue.id)]
    assert wait(lambda: task.downloaded_bytes > 0)

    clock.now = MON.replace(day=3, hour=6)
    mgr._poll_external_changes()
    assert wait(lambda: task.id not in mgr._workers)
    assert task.status == S.QUEUED  # waiting for the next window, not paused
    assert 0 < task.downloaded_bytes < len(DATA)

    clock.now = MON.replace(day=9, hour=22)
    mgr._poll_external_changes()
    assert wait(lambda: task.status == S.COMPLETED)
    assert open(task.dest_path, "rb").read() == DATA


def test_finishing_a_queues_last_download_announces_the_queue(make_manager, server, wait):
    mgr = make_manager()
    announced: list[str] = []
    mgr.queue_finished.connect(announced.append)
    queue = mgr.add_queue(Queue(name="Batch", max_concurrent=2))
    for i in range(2):
        mgr.add_download(f"{server}/b{i}", f"b{i}.bin", queue_id=queue.id)
    mgr.add_download(f"{server}/main", "main.bin")  # Main isn't announced
    assert wait(lambda: all(t.status == S.COMPLETED for t in mgr.tasks.values()))
    assert announced == ["Batch"]


def test_deleting_a_queue_moves_its_downloads_to_main(make_manager, server, wait, tmp_path):
    clock = _Clock(MON.replace(hour=12))
    mgr = make_manager(clock=clock)
    queue = mgr.add_queue(NIGHT)
    task = mgr.tasks[mgr.add_download(f"{server}/moved", "moved.bin", queue_id=queue.id)]
    mgr.delete_queue(queue.id)
    assert task.queue_id == ""
    assert wait(lambda: task.status == S.COMPLETED)  # Main has no hours
    storage = Storage(str(tmp_path / "t.db"))
    assert storage.load_queues() == []
    assert storage.load_all()[0].queue_id == ""


def test_queues_and_their_downloads_survive_a_relaunch(make_manager):
    clock = _Clock(MON.replace(hour=12))
    mgr = make_manager(clock=clock)
    queue = mgr.add_queue(NIGHT)
    task_id = mgr.add_download("https://example.test/f.bin", "f.bin", queue_id=queue.id)
    mgr.shutdown()

    again = make_manager(clock=clock)
    assert again.queues == {queue.id: queue}
    assert again.tasks[task_id].queue_id == queue.id
    assert again.tasks[task_id].status == S.QUEUED  # still outside its hours


def test_adding_to_an_unknown_queue_is_refused(make_manager):
    mgr = make_manager()
    with pytest.raises(ValueError, match="no longer exists"):
        mgr.add_download("https://example.test/f.bin", "f.bin", queue_id="gone")
    with pytest.raises(ValueError, match="already a queue"):
        mgr.add_queue(Queue(name="Main"))


def test_queues_are_managed_through_the_api(make_manager, server, tmp_path, wait):
    import threading

    import requests

    from app.api.runner import ApiServer

    mgr = make_manager(clock=_Clock(MON.replace(hour=12)))
    api = ApiServer(mgr, str(tmp_path / "settings.json"), port=0, app_data_dir=str(tmp_path / "appdata"))
    api.start()
    base = f"http://127.0.0.1:{api.port}/api"
    night = {"name": "Night", "max_concurrent": 2, "scheduled": True,
             "start_minute": 22 * 60, "stop_minute": 6 * 60, "days": [0]}
    seen = {}

    def client():  # off the main thread: the API calls back into it
        s = requests.Session()
        s.headers["Authorization"] = f"Bearer {api.token}"
        created = s.post(f"{base}/queues", json=night)
        seen["created"] = created.status_code, created.json()
        qid = created.json()["id"]
        seen["taken"] = s.post(f"{base}/queues", json={**night, "name": "night"}).json()["detail"]
        seen["empty"] = s.post(f"{base}/queues", json={**night, "name": "X", "stop_minute": 22 * 60}).status_code
        added = s.post(f"{base}/downloads", json={"url": f"{server}/api", "filename": "api.bin", "queue_id": qid})
        seen["added"] = added.json()["queue_id"] == qid
        seen["bad_move"] = s.patch(f"{base}/downloads/{added.json()['id']}", json={"queue_id": "gone"}).status_code
        seen["renamed"] = s.patch(f"{base}/queues/{qid}", json={"name": "Overnight"}).json()["name"]
        seen["missing"] = s.patch(f"{base}/queues/gone", json={"name": "Y"}).status_code
        seen["deleted"] = s.delete(f"{base}/queues/{qid}").status_code
        seen["left"] = s.get(f"{base}/queues").json()

    thread = threading.Thread(target=client)
    try:
        thread.start()
        assert wait(lambda: not thread.is_alive(), timeout=30)  # pumps Qt events meanwhile
    finally:
        api.stop()
    status, body = seen.pop("created")
    assert status == 201
    assert body["is_open"] is False and body["opens_at"] == MON.replace(hour=22).timestamp()
    assert seen == {"taken": "There's already a queue called night", "empty": 400, "added": True,
                    "bad_move": 400, "renamed": "Overnight", "missing": 404, "deleted": 200, "left": []}
    assert wait(lambda: all(t.status == S.COMPLETED for t in mgr.tasks.values()))  # moved to Main, so it ran
