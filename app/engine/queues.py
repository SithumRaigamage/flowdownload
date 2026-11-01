"""Named download queues and their schedules -- pure values and helpers.

Every download belongs to a queue. The built-in "Main" queue (queue_id "")
isn't stored anywhere: it has no schedule, and only "Downloads at once"
limits it. Named queues add their own limit and an optional daily window.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

MAIN_QUEUE_ID = ""
MAIN_QUEUE_NAME = "Main"
MAX_NAME_LENGTH = 40
# Same bound as "Downloads at once" (SettingsPatch.max_concurrent_downloads).
MAX_QUEUE_SLOTS = 50
MINUTES_PER_DAY = 24 * 60
ALL_DAYS = tuple(range(7))  # datetime.weekday(): 0 = Monday


@dataclass(frozen=True)
class Queue:
    name: str
    max_concurrent: int = 1
    # Off: always open. On: open from start_minute to stop_minute (minutes
    # past local midnight) on `days`. A stop before the start runs past
    # midnight, and belongs to the day it started on.
    scheduled: bool = False
    start_minute: int = 0
    stop_minute: int = 0
    days: tuple[int, ...] = ALL_DAYS
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)


def validate_queue(queue: Queue, taken_names: Iterable[str]) -> Queue:
    """Return `queue` with its name trimmed and days sorted, or raise
    ValueError with a message the Queues editor can show as is."""
    name = queue.name.strip()
    if not name:
        raise ValueError("Give the queue a name")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"Queue names can be at most {MAX_NAME_LENGTH} characters")
    if name.casefold() in {n.casefold() for n in (*taken_names, MAIN_QUEUE_NAME)}:
        raise ValueError(f"There's already a queue called {name}")
    if not 1 <= queue.max_concurrent <= MAX_QUEUE_SLOTS:
        raise ValueError(f"A queue can run between 1 and {MAX_QUEUE_SLOTS} downloads at once")
    days = tuple(sorted(set(queue.days)))
    if any(d not in ALL_DAYS for d in days):
        raise ValueError("Days must be numbers from 0 (Monday) to 6 (Sunday)")
    if queue.scheduled:
        if not all(0 <= m < MINUTES_PER_DAY for m in (queue.start_minute, queue.stop_minute)):
            raise ValueError("Start and stop times must be within the day")
        if queue.start_minute == queue.stop_minute:
            raise ValueError("The start and stop times are the same. Pick a stop time after the start.")
        if not days:
            raise ValueError("Pick at least one day for the schedule")
    return Queue(name=name, max_concurrent=queue.max_concurrent, scheduled=queue.scheduled,
                 start_minute=queue.start_minute, stop_minute=queue.stop_minute, days=days,
                 id=queue.id, created_at=queue.created_at)


def is_open(queue: Queue, now: datetime) -> bool:
    """Whether the queue may start downloads at local time `now`."""
    if not queue.scheduled:
        return True
    minute = now.hour * 60 + now.minute
    start, stop = queue.start_minute, queue.stop_minute
    if start < stop:
        return now.weekday() in queue.days and start <= minute < stop
    # Overnight: either today's window has started, or yesterday's is still running.
    if minute >= start:
        return now.weekday() in queue.days
    return minute < stop and (now.weekday() - 1) % 7 in queue.days


def opens_at(queue: Queue, now: datetime) -> datetime | None:
    """When a closed queue next opens; None while it's open."""
    if is_open(queue, now) or not queue.days:
        return None
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for day in range(8):
        start = midnight + timedelta(days=day, minutes=queue.start_minute)
        if start > now and start.weekday() in queue.days:
            return start
    return None


def days_to_mask(days: Iterable[int]) -> int:
    return sum(1 << d for d in set(days))


def mask_to_days(mask: int) -> tuple[int, ...]:
    return tuple(d for d in ALL_DAYS if mask & (1 << d))
