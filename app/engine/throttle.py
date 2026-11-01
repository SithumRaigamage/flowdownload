"""The app-wide speed cap: one token bucket that every connection draws from.

A split download runs one thread per connection, so a cap enforced per
thread would let four connections use four times the limit. Instead the
manager owns a single RateLimiter and hands it to every worker; each chunk
loop takes its bytes from it before writing them.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

# Longest a waiting connection sleeps before checking again, so a pause or
# cancel, or a change to the limit, takes effect within about this long.
WAIT_SLICE = 0.05
# Unused allowance a quiet bucket may save up, in seconds at the current
# rate. Enough to cover the wake-up slack of a WAIT_SLICE sleep; small
# enough that a download starting up only briefly goes over the cap.
BURST_SECONDS = 0.25


class RateLimiter:
    """Thread-safe token bucket. A rate of 0 means unlimited.

    A connection may take a chunk whenever the bucket isn't in debt, even a
    chunk bigger than what's in it; the bucket then goes negative and every
    connection waits until the debt is paid back. That keeps the average at
    the cap even when one chunk (CHUNK_SIZE, 64 KB) is more than a second's
    worth at a low limit.
    """

    def __init__(self, rate_bps: int = 0, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._lock = threading.Lock()
        self._clock = clock
        self._sleep = sleep
        self._rate = 0.0
        self._tokens = 0.0
        self._stamp = clock()
        self.set_rate(rate_bps)

    @property
    def rate(self) -> int:
        return int(self._rate)

    def set_rate(self, rate_bps: int) -> None:
        """Change the cap (0 = unlimited). Connections already waiting pick
        up the new rate on their next check."""
        with self._lock:
            self._refill()
            self._rate = float(max(0, rate_bps))
            self._tokens = min(self._tokens, self._capacity())

    def consume(self, n: int, stopped: Callable[[], bool]) -> bool:
        """Take `n` bytes of allowance, waiting while the bucket is in debt.

        Returns False, without taking anything, if `stopped()` becomes true
        while waiting, so the caller can unwind for a pause or cancel.
        """
        while True:
            if not self._rate:  # unlimited: the common case stays lock-free
                return True
            with self._lock:
                if not self._rate:
                    return True
                self._refill()
                # "Paid back" means less than a byte still owed: sleeping for
                # exactly the computed wait can leave float dust like -1e-12,
                # whose wait would be too small to move the clock at all.
                if self._tokens > -1:
                    self._tokens -= n
                    return True
                wait = min(WAIT_SLICE, -self._tokens / self._rate)
            if stopped():
                return False
            self._sleep(wait)

    def _capacity(self) -> float:
        return self._rate * BURST_SECONDS

    def _refill(self) -> None:
        now = self._clock()
        if self._rate:
            self._tokens = min(self._capacity(), self._tokens + (now - self._stamp) * self._rate)
        self._stamp = now
