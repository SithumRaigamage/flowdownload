"""Turning a URL into a queued download row -- shared by the app's
DownloadManager and the Chrome native host (a separate process that never
loads Qt), so both pick destination paths by exactly the same rules."""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable

from app.engine.models import DownloadStatus, DownloadTask
from app.engine.paths import apply_auto_folder_for_path, dedupe_path
from app.engine.settings import Settings
from app.engine.storage import Storage

FREE_NAME_ATTEMPTS = 20


def find_completed(tasks: Iterable[DownloadTask], url: str) -> DownloadTask | None:
    """The completed download of exactly this URL, if any (the duplicate check)."""
    return next((t for t in tasks if t.status == DownloadStatus.COMPLETED and t.url == url), None)


def queue_download(storage: Storage, settings: Settings, url: str, filename: str, *,
                   cookie: str = "", referrer: str = "", replace: DownloadTask | None = None) -> DownloadTask:
    """Save a QUEUED row for `url` and return it.

    `filename` must already be safe (paths.safe_filename); the folder is
    always settings.download_dir (plus its type subfolder when auto-folder
    is on), never caller-supplied. `replace` is a completed download being
    overwritten: its row is deleted and its path reused. Its file stays on
    disk until the new download completes -- the worker swaps it in with
    os.replace at the end, so a failed re-download never costs the user the
    copy they already had. Raises RuntimeError if no free filename is found.
    """
    if settings.auto_folder_by_type:
        dest_path = apply_auto_folder_for_path(filename, settings.download_dir)
    else:
        dest_path = os.path.join(settings.download_dir, filename)

    # From storage, not anyone's in-memory list: rows the other process (app
    # or native host) just inserted haven't written a byte yet, so they have
    # no .part for dedupe_path's filesystem check to see -- without this, two
    # downloads queued back-to-back with the same name would share a file.
    reserved = storage.dest_paths()
    if replace is not None:
        storage.delete(replace.id)
        reserved.discard(replace.dest_path)
        dest_path = replace.dest_path
    else:
        dest_path = dedupe_path(dest_path, reserved)

    task = DownloadTask(url=url, dest_path=dest_path, filename=os.path.basename(dest_path),
                        status=DownloadStatus.QUEUED, cookie_header=cookie, referrer=referrer)
    # The reserved-set read above and this insert aren't atomic across the
    # two processes. storage's UNIQUE index on dest_path turns a same-instant
    # collision into an IntegrityError instead of two tasks silently sharing
    # a file; retry against a freshly renamed path, as dedupe_path would
    # have picked had it seen the other row in time.
    for _ in range(FREE_NAME_ATTEMPTS):
        try:
            storage.save(task)
            return task
        except sqlite3.IntegrityError:
            reserved.add(task.dest_path)
            task.dest_path = dedupe_path(dest_path, reserved)
            task.filename = os.path.basename(task.dest_path)
    raise RuntimeError(f"Could not find a free filename after {FREE_NAME_ATTEMPTS} attempts")
