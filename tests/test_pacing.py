"""Holding back downloads whose signed links would expire before they finish."""
from __future__ import annotations

from app.engine.models import DownloadStatus as S
from app.engine.models import DownloadTask
from app.engine.pacing import EXPIRY_MARGIN, can_start

NOW = 1_793_386_590
MB = 1024 * 1024


def link(expires_in: float) -> str:
    return f"https://c10.trafficdeposit.com/vidi/sig/tok/{int(NOW + expires_in)}/68e2/x.vid"


def running(expires_in, total, done, speed):
    return DownloadTask(url=link(expires_in), dest_path="/x", status=S.DOWNLOADING,
                        total_bytes=total, downloaded_bytes=done, speed_bps=speed)


def queued(expires_in, total=0):
    return DownloadTask(url=link(expires_in), dest_path="/y", total_bytes=total)


def test_links_without_expiry_are_never_held():
    plain = DownloadTask(url="https://cdn.test/x.mp4", dest_path="/y")
    assert can_start(plain, [running(60, 900 * MB, 0, 1)], NOW)


def test_first_download_from_a_site_always_starts():
    assert can_start(queued(3000), [], NOW)


def test_waits_while_the_site_speed_is_unknown():
    assert not can_start(queued(3000), [running(3000, 300 * MB, 0, 0.0)], NOW)


def test_starts_when_everything_still_fits_before_expiry():
    # 300 MB left + 300 MB new at 2 MB/s = 300 s, well inside 3000 s.
    assert can_start(queued(3000, 300 * MB), [running(3000, 300 * MB, 0, 2 * MB)], NOW)


def test_held_when_it_would_push_a_running_download_past_its_expiry():
    # Alone, the running one needs 400 s of its 600; adding 800 MB more needs 800 s.
    assert not can_start(queued(3000, 800 * MB), [running(600, 800 * MB, 0, 2 * MB)], NOW)


def test_unknown_size_is_estimated_from_the_running_downloads():
    # Each running file is ~1000 MB, so the newcomer is assumed to be too.
    fleet = [running(1000, 1000 * MB, 500 * MB, 1 * MB)]
    assert not can_start(queued(1000), fleet, NOW)  # 500 + 1000 MB at 1 MB/s = 1500 s > 1000 s
    assert can_start(queued(3000), [running(3000, 1000 * MB, 500 * MB, 1 * MB)], NOW)  # 1620 s < 3000 s


def test_margin_is_kept_before_expiry():
    just_fits = running(100 + EXPIRY_MARGIN, 100 * MB, 0, 2 * MB)  # 100 MB alone takes 50 s
    assert can_start(queued(10_000, 100 * MB), [just_fits], NOW)
    assert not can_start(queued(10_000, 200 * MB), [just_fits], NOW)


def test_already_expired_link_starts_so_it_fails_with_a_clear_message():
    assert can_start(queued(-10), [running(60, 900 * MB, 0, 1)], NOW)
