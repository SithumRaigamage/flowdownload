"""Holding back downloads whose links expire.

Signed CDN links stop working a while after they're captured (often under
an hour). Every download from a site shares the same internet connection,
so starting a whole batch at once makes each one slow -- and they all run
past their expiry together and fail with 403. Instead, a queued download
with an expiring link only starts when the site's measured speed says
everything already running there, plus it, still finishes in time.
"Downloads at once" stays the upper limit; this only ever starts fewer.
"""
from __future__ import annotations

from collections.abc import Sequence

from app.engine.models import DownloadTask
from app.engine.paths import link_expiry

# Slack before a link's expiry: the estimate is only as steady as the
# measured speed, and the last bytes must arrive before the link dies.
EXPIRY_MARGIN = 120.0


def can_start(candidate: DownloadTask, running: Sequence[DownloadTask], now: float) -> bool:
    """Whether to start `candidate` now, given the active downloads on its site.

    Deliberately pessimistic: with the bandwidth shared, the estimate is
    that everyone finishes only when all the remaining bytes are in, and
    that must land before the earliest expiry among them.
    """
    expiry = link_expiry(candidate.url)
    if expiry is None or expiry <= now:
        return True  # nothing to protect, or already expired: let it fail fast with a clear message
    if not running:
        return True  # nothing to measure against: the first one always goes
    site_bps = sum(t.speed_bps for t in running)
    if site_bps <= 0:
        return False  # still connecting or measuring; the next dispatch looks again

    known = [t.total_bytes for t in running if t.total_bytes > 0]
    typical = sum(known) / len(known) if known else 0  # stand-in until a size is known
    remaining = sum(_remaining(t, typical) for t in [*running, candidate])
    deadlines = [e for e in (link_expiry(t.url) for t in running) if e is not None] + [expiry]
    return now + remaining / site_bps + EXPIRY_MARGIN <= min(deadlines)


def _remaining(task: DownloadTask, typical: float) -> float:
    total = task.total_bytes if task.total_bytes > 0 else typical
    return max(0.0, total - task.downloaded_bytes)
