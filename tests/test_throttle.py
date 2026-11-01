"""RateLimiter maths, on a fake clock so no test really waits."""
from __future__ import annotations

import pytest

from app.engine.throttle import BURST_SECONDS, RateLimiter


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


def never() -> bool:
    return False


def test_unlimited_never_waits(clock):
    limiter = RateLimiter(0, clock=clock, sleep=clock.sleep)
    for _ in range(100):
        assert limiter.consume(64 * 1024, never)
    assert clock.sleeps == []


def test_average_rate_matches_the_cap(clock):
    limiter = RateLimiter(100_000, clock=clock, sleep=clock.sleep)
    for _ in range(10):
        assert limiter.consume(10_000, never)
    # The first chunk passes at once (the bucket goes into debt); the other
    # 90 KB wait to be paid for at 100 KB/s.
    assert clock.now == pytest.approx(0.9, abs=0.01)


def test_a_chunk_bigger_than_the_bucket_still_passes(clock):
    limiter = RateLimiter(16 * 1024, clock=clock, sleep=clock.sleep)
    assert limiter.consume(64 * 1024, never)
    assert limiter.consume(64 * 1024, never)  # waits off the first chunk's debt
    assert clock.now == pytest.approx(4.0, abs=0.06)


def test_waits_are_sliced(clock):
    limiter = RateLimiter(1_000, clock=clock, sleep=clock.sleep)
    limiter.consume(1_000, never)
    limiter.consume(1, never)
    assert clock.sleeps and max(clock.sleeps) <= 0.05


def test_stopping_while_waiting_takes_nothing(clock):
    limiter = RateLimiter(1_000, clock=clock, sleep=clock.sleep)
    limiter.consume(5_000, never)
    debt = limiter._tokens
    assert limiter.consume(5_000, lambda: True) is False
    assert limiter._tokens == debt
    assert clock.sleeps == []


def test_turning_the_limit_off_releases_waiters(clock):
    limiter = RateLimiter(1_000, clock=clock, sleep=clock.sleep)
    limiter.consume(100_000, never)  # 100 s of debt at this rate
    limiter.set_rate(0)
    assert limiter.consume(100_000, never)
    assert clock.now == 0.0


def test_raising_the_limit_shortens_the_wait(clock):
    limiter = RateLimiter(1_000, clock=clock, sleep=clock.sleep)
    limiter.consume(100_000, never)
    limiter.set_rate(1_000_000)
    assert limiter.consume(1, never)
    assert clock.now == pytest.approx(0.1, abs=0.06)


def test_idle_allowance_is_capped(clock):
    limiter = RateLimiter(100_000, clock=clock, sleep=clock.sleep)
    clock.now += 60  # a minute with nothing downloading
    limiter.consume(1, never)
    assert limiter._tokens == pytest.approx(100_000 * BURST_SECONDS - 1)
