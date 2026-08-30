from __future__ import annotations

import random

BASE_DELAY = 1.0
MAX_DELAY = 60.0


def compute_retry_delay(attempt: int) -> float | None:
    """Decide how long to wait before retry number `attempt` (1, 2, 3, ...).

    Return the number of seconds to wait, or None to give up and mark the
    download as failed.

    Capped exponential backoff, retried forever: doubles each attempt up to
    MAX_DELAY, with jitter so several downloads retrying at once don't all
    hit the server in the same instant. No attempt limit, since a flaky home
    connection reconnects on its own and the file is still there waiting.
    """
    delay = min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)
    return delay * random.uniform(0.8, 1.2)
