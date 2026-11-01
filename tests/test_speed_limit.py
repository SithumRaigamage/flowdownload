"""The app-wide speed cap, end to end through the manager and the API."""
from __future__ import annotations

import dataclasses
import time

import pytest
from pydantic import ValidationError

from app.api.schemas import MIN_SPEED_LIMIT_BPS, SettingsOut, SettingsPatch
from app.engine import downloader, segmented
from app.engine.models import DownloadStatus as S
from app.engine.settings import Settings
from tests.conftest import DATA, REQUESTS

CAP = 400_000  # bytes/s: DATA (400 KB) takes about a second


@pytest.fixture(autouse=True)
def small_thresholds(monkeypatch):
    # Split anything >= 100 KB, into pieces >= 50 KB (as in test_segmented).
    monkeypatch.setattr(segmented, "MIN_TOTAL", 100_000)
    monkeypatch.setattr(segmented, "MIN_PART", 50_000)
    monkeypatch.setattr(downloader, "compute_retry_delay", lambda attempt: None if attempt > 5 else 0.01)
    REQUESTS.clear()


def _timed_download(mgr, server, wait) -> float:
    started = time.monotonic()
    task = mgr.tasks[mgr.add_download(f"{server}/big?delay=0", "big.bin")]
    assert wait(lambda: task.status == S.COMPLETED)
    assert open(task.dest_path, "rb").read() == DATA
    return time.monotonic() - started


def test_split_download_shares_one_cap_across_its_connections(make_manager, server, wait):
    mgr = make_manager(connections_per_download=4, speed_limit_enabled=True, speed_limit_bps=CAP)
    elapsed = _timed_download(mgr, server, wait)
    ranges = {h.get("Range") for p, h in REQUESTS if p.startswith("/big")}
    # It really was split: the four pieces were all requested.
    assert {"bytes=0-99999", "bytes=100000-199999", "bytes=200000-299999", "bytes=300000-399999"} <= ranges
    # A per-connection cap would let 4 connections finish in about a quarter
    # of this. The first 64 KB chunk passes before the bucket is in debt.
    assert elapsed >= (len(DATA) - 64 * 1024) / CAP * 0.9


def test_single_stream_download_is_capped(make_manager, server, wait):
    mgr = make_manager(connections_per_download=1, speed_limit_enabled=True, speed_limit_bps=CAP)
    assert _timed_download(mgr, server, wait) >= (len(DATA) - 64 * 1024) / CAP * 0.9


def test_turning_the_limit_off_speeds_up_a_running_download(make_manager, server, wait):
    mgr = make_manager(connections_per_download=4, speed_limit_enabled=True, speed_limit_bps=MIN_SPEED_LIMIT_BPS)
    task = mgr.tasks[mgr.add_download(f"{server}/big?delay=0", "big.bin")]
    assert wait(lambda: task.downloaded_bytes > 0)  # ~25 s to finish at this cap
    mgr.apply_settings(dataclasses.replace(mgr.settings, speed_limit_enabled=False))
    assert wait(lambda: task.status == S.COMPLETED, timeout=5)
    assert open(task.dest_path, "rb").read() == DATA


def test_pausing_while_held_back_by_the_limit_is_prompt(make_manager, server, wait):
    mgr = make_manager(connections_per_download=4, speed_limit_enabled=True, speed_limit_bps=MIN_SPEED_LIMIT_BPS)
    task = mgr.tasks[mgr.add_download(f"{server}/big?delay=0", "big.bin")]
    assert wait(lambda: task.downloaded_bytes > 0)
    mgr.pause(task.id)
    assert wait(lambda: task.id not in mgr._workers, timeout=2)
    assert task.status == S.PAUSED


def test_limit_is_off_by_default_and_keeps_its_value_when_toggled():
    assert Settings().speed_limit == 0
    on = Settings(speed_limit_enabled=True, speed_limit_bps=CAP)
    assert on.speed_limit == CAP
    assert dataclasses.replace(on, speed_limit_enabled=False).speed_limit == 0
    out = SettingsOut.from_settings(on)
    assert (out.speed_limit_enabled, out.speed_limit_bps) == (True, CAP)


def test_settings_patch_rejects_a_limit_below_the_minimum():
    assert SettingsPatch(speed_limit_bps=MIN_SPEED_LIMIT_BPS).speed_limit_bps == MIN_SPEED_LIMIT_BPS
    for bad in (0, MIN_SPEED_LIMIT_BPS - 1):
        with pytest.raises(ValidationError):
            SettingsPatch(speed_limit_bps=bad)
