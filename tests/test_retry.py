"""DownloadWorker retry behaviour against a server that drops connections."""
from __future__ import annotations

import pytest
import requests

from app.engine import downloader, retry_policy
from app.engine.downloader import DownloadWorker
from tests.conftest import DATA, REQUESTS


@pytest.fixture
def attempts(monkeypatch):
    """Record the attempt number of every retry, cap at 3, and skip the
    real backoff sleep so the tests stay fast."""
    seen: list[int] = []

    def fake_delay(attempt):
        seen.append(attempt)
        return None if attempt > 3 else 0.01

    monkeypatch.setattr(downloader, "compute_retry_delay", fake_delay)
    return seen


# Workers are kept alive for the whole session. A parentless QThread is owned
# by its Python wrapper, so letting it be garbage-collected frees the C++
# object while its last queued signals may still be in flight -- a segfault
# the next time the event loop runs. (The app avoids this by only dropping a
# worker from its own `finished` handler; see DownloadManager._on_worker_finished.)
_WORKERS: list[DownloadWorker] = []


def run_worker(url, tmp_path, wait, referrer=""):
    worker = DownloadWorker("t", url, str(tmp_path / "out.bin"), "", "", "", requests.Session(), referrer=referrer)
    _WORKERS.append(worker)
    statuses: list[str] = []
    worker.status_changed.connect(lambda _tid, status, _msg: statuses.append(status))
    worker.start()
    assert wait(lambda: worker.isFinished(), timeout=30)
    worker.wait()
    wait(lambda: False, timeout=0.1)  # deliver the last queued signals
    return statuses


def test_status_returns_to_downloading_after_reconnect(server, tmp_path, wait, attempts):
    statuses = run_worker(f"{server}/f?drop=100000", tmp_path, wait)
    assert statuses[0] == "retrying"
    assert "downloading" in statuses[1:]
    assert statuses[-1] == "completed"


def test_progress_resets_retry_counter(server, tmp_path, wait, attempts):
    # Drops after 64 KB on *every* connection: far more drops than the cap
    # of 3 allows, but each one made progress, so it must still complete.
    statuses = run_worker(f"{server}/f?drop=65536", tmp_path, wait)
    assert statuses[-1] == "completed"
    assert len(attempts) > 3
    assert set(attempts) == {1}
    assert (tmp_path / "out.bin").read_bytes() == DATA


def test_no_progress_gives_up_after_cap(server, tmp_path, wait, attempts):
    statuses = run_worker(f"{server}/f?drop=0", tmp_path, wait)
    assert statuses[-1] == "failed"
    assert attempts == [1, 2, 3, 4]


def test_permanent_http_error_fails_without_retry(server, tmp_path, wait, attempts):
    statuses = run_worker(f"{server}/f?status=404", tmp_path, wait)
    assert statuses == ["failed"]
    assert attempts == []


def test_referer_is_sent_on_every_connection(server, tmp_path, wait, attempts):
    REQUESTS.clear()
    statuses = run_worker(f"{server}/ref?drop=100000", tmp_path, wait, referrer="https://page.test/watch")
    assert statuses[-1] == "completed"
    sent = [headers.get("Referer") for path, headers in REQUESTS if path.startswith("/ref")]
    assert len(sent) >= 2  # first connection + at least one reconnect
    assert set(sent) == {"https://page.test/watch"}


def test_no_referer_header_when_unknown(server, tmp_path, wait, attempts):
    REQUESTS.clear()
    run_worker(f"{server}/noref", tmp_path, wait)
    assert all("Referer" not in headers for path, headers in REQUESTS if path.startswith("/noref"))


def test_backoff_is_capped_and_gives_up():
    assert retry_policy.compute_retry_delay(retry_policy.MAX_ATTEMPTS + 1) is None
    for attempt in (1, 5, retry_policy.MAX_ATTEMPTS):
        delay = retry_policy.compute_retry_delay(attempt)
        assert 0 < delay <= retry_policy.MAX_DELAY * 1.2
