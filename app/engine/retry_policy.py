from __future__ import annotations

import random

BASE_DELAY = 1.0
MAX_DELAY = 60.0
# Cap total retries so a permanently-broken download can't pin a worker slot
# forever; user can re-trigger via the Retry button which resets the attempt
# counter.
MAX_ATTEMPTS = 30


def compute_retry_delay(attempt: int) -> float | None:
    """Decide how long to wait before retry number `attempt` (1, 2, 3, ...).

    Return the number of seconds to wait, or None to give up and mark the
    download as failed.

    Capped exponential backoff: doubles each attempt up to MAX_DELAY, with
    jitter so several downloads retrying at once don't all hit the server in
    the same instant. Gives up after MAX_ATTEMPTS retries so a permanently-
    broken URL can't pin a worker slot indefinitely.
    """
    if attempt > MAX_ATTEMPTS:
        return None
    delay = min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)
    return delay * random.uniform(0.8, 1.2)
