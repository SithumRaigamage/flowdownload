from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

import requests
from PySide6.QtCore import QObject, QTimer, Signal

from app.engine.downloader import DownloadWorker
from app.engine.models import DownloadStatus, DownloadTask
from app.engine import segmented
from app.engine.paths import dedupe_path, domain_key
from app.engine.probe import validate_download_url
from app.engine.settings import Settings
from app.engine.storage import Storage

ACTIVE_STATUSES = (DownloadStatus.DOWNLOADING, DownloadStatus.RETRYING)
EXTERNAL_POLL_INTERVAL_MS = 2000
MAX_CONNECTIONS_PER_DOWNLOAD = 8
# Progress ticks arrive ~4x/s per download; persisting each one meant dozens
# of SQLite commits per second on the main thread. The UI still gets every
# tick via task_updated -- only the DB write is throttled. Nothing is lost by
# skipping writes: status changes save the full task, and _load_existing
# re-reads downloaded_bytes from the .part file on the next launch.
PROGRESS_SAVE_INTERVAL = 1.0
HISTORY_LIMIT = 50  # per task; in-memory only, so a restart clears it -- fine for a debugging log

# Module-level reference to workers that didn't exit cleanly on shutdown.
# When the manager is torn down, the worker's QThread would be destroyed while
# still running -- Qt treats that as a fatal error and abort()s the process.
# Keeping them alive at module scope (which outlives the manager) prevents that
# until the OS reaps the whole process anyway. See DownloadManager.shutdown().
_STUCK_WORKERS: list[DownloadWorker] = []

# File-type extensions that go into subfolders when auto-folder is enabled.
AUTO_FOLDER_VIDEO = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".vid", ".m4v"}
AUTO_FOLDER_AUDIO = {".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg"}
AUTO_FOLDER_DOCUMENT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
AUTO_FOLDER_ARCHIVE = {".zip", ".tar", ".gz", ".7z", ".rar", ".dmg", ".pkg", ".iso"}


@dataclass
class DedupeResult:
    """Result of a dedupe check: whether an existing completed file matches."""
    is_duplicate: bool
    existing_task_id: Optional[str] = None
    existing_path: Optional[str] = None
    existing_size: int = 0
    existing_digest: Optional[str] = None


class DownloadManager(QObject):
    task_added = Signal(str)
    task_updated = Signal(str)
    task_removed = Signal(str)
    task_finished = Signal(str, str)  # task_id, status_value ("completed" or "failed")

    def __init__(self, storage: Storage, settings: Settings, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.settings = settings
        self.tasks: dict[str, DownloadTask] = {}
        self._workers: dict[str, DownloadWorker] = {}
        # Tasks the user resumed while their previous worker was still
        # unwinding from a pause. They're held QUEUED (the worker's final
        # PAUSED signal is ignored for them) and dispatched once it exits.
        self._resume_pending: set[str] = set()
        self._last_progress_save: dict[str, float] = {}
        self._shutting_down = False
        # Per-task event log for the UI's detail view -- in-memory only
        # (not persisted), since it's a debugging aid, not state the app
        # needs to survive a restart.
        self.history: dict[str, list[dict]] = {}
        self._session = requests.Session()
        # Some servers block or alter behavior for the default
        # "python-requests/X" UA; a realistic browser UA avoids that.
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        })
        self._load_existing()

        # Picks up rows inserted by another process while we're running --
        # currently only the Chrome native messaging host does this, adding
        # a queued download without going through this DownloadManager.
        self._external_poll_timer = QTimer(self)
        self._external_poll_timer.timeout.connect(self._poll_external_changes)
        self._external_poll_timer.start(EXTERNAL_POLL_INTERVAL_MS)

    def _poll_external_changes(self) -> None:
        for task in self.storage.load_all():
            if task.id not in self.tasks:
                self.tasks[task.id] = task
                self.task_added.emit(task.id)
        self._apply_pending_refreshes()
        self._dispatch_queue()

    def _apply_pending_refreshes(self) -> None:
        """Apply fresh links the Chrome native host matched to our downloads."""
        for task_id, url, cookie, referrer in self.storage.pop_refreshes():
            if task_id not in self.tasks:
                continue  # removed since the host matched it
            try:
                self.refresh_url(task_id, url, cookie=cookie, referrer=referrer, source="Chrome")
            except ValueError as exc:
                self._log(task_id, "warn", f"Ignored a fresh link from Chrome: {exc}")

    def refresh_url(self, task_id: str, url: str, cookie: str | None = None,
                    referrer: str | None = None, source: str = "") -> None:
        """Point an unfinished download at a new URL (e.g. a re-signed CDN link)
        and resume it.

        The .part file and the ETag/Last-Modified validators are kept, so the
        next request is a conditional (If-Range) resume: the server continues
        from our partial data if it's the same file, or sends the whole thing
        again if it isn't -- the worker already restarts from 0 in that case.
        Raises ValueError for an unsafe URL or a completed download.
        """
        validate_download_url(url)
        task = self.tasks[task_id]
        if task.status == DownloadStatus.COMPLETED:
            raise ValueError("This download has already completed")
        task.url = url
        if cookie is not None:
            task.cookie_header = cookie
        if referrer is not None:
            task.referrer = referrer
        task.error_message = ""
        task.updated_at = time.time()
        self.storage.save(task)
        self._log(task_id, "info", "Link refreshed" + (f" from {source}" if source else ""))
        if task.status in ACTIVE_STATUSES:
            self.pause(task_id)  # the old worker is still on the old URL
        self.start(task_id)

    def _load_existing(self) -> None:
        for task in self.storage.load_all():
            # Anything mid-flight when the app last closed was interrupted by
            # the quit, not paused by the user (shutdown() keeps its pauses
            # out of the DB), so queue it to carry on automatically. Progress
            # is re-synced from the .part file, the real source of truth.
            # Tasks that were only QUEUED stay queued; both get picked up by
            # _dispatch_queue() below, same as if they'd just been added.
            if task.status in ACTIVE_STATUSES:
                task.status = DownloadStatus.QUEUED
                self._log(task.id, "info", "Interrupted by app restart — resuming")
            # A split-file download's .part is preallocated to full size, so
            # its real progress comes from the segment sidecar instead.
            split_progress = segmented.load_progress(task.part_path)
            if split_progress is not None:
                task.downloaded_bytes = split_progress
            elif os.path.exists(task.part_path):
                task.downloaded_bytes = os.path.getsize(task.part_path)
            task.updated_at = time.time()
            self.storage.save(task)
            self.tasks[task.id] = task
        self._apply_pending_refreshes()
        self._dispatch_queue()

    def _log(self, task_id: str, level: str, message: str) -> None:
        entries = self.history.setdefault(task_id, [])
        entries.append({"time": time.time(), "level": level, "message": message})
        del entries[:-HISTORY_LIMIT]

    def get_history(self, task_id: str) -> list[dict]:
        return list(self.history.get(task_id, []))

    def add_download(self, url: str, filename: str) -> str:
        validate_download_url(url)

        # Only the filename is trusted from the caller -- the destination
        # directory is always settings.download_dir, never client-supplied,
        # so a crafted filename (e.g. "../../Library/LaunchAgents/x.plist")
        # can't escape the download folder. Mirrors native_host.py, which
        # applies the same os.path.basename() boundary for the Chrome
        # extension hand-off.
        filename = os.path.basename(filename) or "download"
        dest_path = self.apply_auto_folder(filename)

        # Read from storage rather than self.tasks: a download the Chrome
        # extension just queued via the native host lands in storage
        # immediately but isn't polled into self.tasks for up to
        # EXTERNAL_POLL_INTERVAL_MS, so self.tasks alone could still miss it.
        reserved = {t.dest_path for t in self.storage.load_all()}

        overwrite = False
        dup = self.check_dedupe(url)
        if dup.is_duplicate:
            if self.settings.duplicate_policy == "skip":
                self._log(dup.existing_task_id, "info", "Re-add skipped: URL already completed")
                return dup.existing_task_id
            if self.settings.duplicate_policy == "overwrite":
                old = self.tasks.pop(dup.existing_task_id, None)
                if old is not None:
                    # The old file is left in place: the new download writes
                    # to <dest>.part and _finalize's os.replace swaps it in
                    # only once it completes, so a failed re-download never
                    # costs the user the copy they already had.
                    self.storage.delete(old.id)
                    self.history.pop(old.id, None)
                    reserved.discard(old.dest_path)
                    self.task_removed.emit(old.id)
                    dest_path = old.dest_path
                    overwrite = True
            # "rename" (default) falls through unchanged -- dedupe_path below
            # already picks a fresh filename on any collision.

        if not overwrite:
            dest_path = dedupe_path(dest_path, reserved)
        task = DownloadTask(
            url=url, dest_path=dest_path, filename=os.path.basename(dest_path),
            status=DownloadStatus.QUEUED,
        )
        # The reserved-set check above and this save aren't atomic across
        # processes -- the native host (a separate process) could insert a
        # row claiming the same dest_path in between. storage's UNIQUE
        # index on dest_path turns that into an IntegrityError here rather
        # than two tasks silently sharing a file; retry against a freshly
        # renamed path the same way dedupe_path would have if it had seen
        # the other process's row in time.
        for _ in range(20):
            try:
                self.storage.save(task)
                break
            except sqlite3.IntegrityError:
                reserved.add(task.dest_path)
                task.dest_path = dedupe_path(dest_path, reserved)
                task.filename = os.path.basename(task.dest_path)
        else:
            raise RuntimeError("Could not find a free filename after 20 attempts")
        self.tasks[task.id] = task
        self._log(task.id, "info", "Added to queue")
        self.task_added.emit(task.id)
        self._dispatch_queue()
        return task.id

    def start(self, task_id: str) -> None:
        """User explicitly wants this task running now (Resume / Retry click)."""
        task = self.tasks[task_id]
        existing = self._workers.get(task_id)
        if existing is not None and existing.isRunning():
            # Still unwinding from a pause -- starting now would put two
            # workers on one .part file. Queue it; _on_worker_finished
            # dispatches it as soon as the old worker is gone.
            self._resume_pending.add(task_id)
            task.status = DownloadStatus.QUEUED
            task.error_message = ""
            task.updated_at = time.time()
            self.storage.save(task)
            self.task_updated.emit(task_id)
            return
        if (self._active_count() >= self.settings.max_concurrent_downloads
                or self._connections_used(domain_key(task.url)) >= self._site_limit()):
            task.status = DownloadStatus.QUEUED
            task.error_message = ""
            task.updated_at = time.time()
            self.storage.save(task)
            self.task_updated.emit(task_id)
            return
        self._spawn(task_id)

    def retry(self, task_id: str) -> None:
        self.start(task_id)

    def pause(self, task_id: str) -> None:
        try:
            self._do_pause(task_id)
        except Exception as exc:
            # Never let a pause call crash the Qt event loop. If something
            # goes wrong here, log it and let the worker's eventual exit
            # (whenever the next chunk boundary arrives) drive the state
            # transition through _on_status_changed instead.
            import traceback
            print(f"FlowDownload: pause({task_id}) swallowed exception: {exc}\n{traceback.format_exc()}", flush=True)

    def _do_pause(self, task_id: str) -> None:
        worker = self._workers.get(task_id)
        if worker is not None:
            worker.pause()
        # Flip the task status *now*, before the worker exits. Otherwise
        # _dispatch_queue() (called from _on_worker_finished when the worker
        # finishes the current chunk) sees status==DOWNLOADING and immediately
        # spawns a replacement, so the pause never sticks.
        task = self.tasks.get(task_id)
        if task is not None and task.status in ACTIVE_STATUSES:
            task.status = DownloadStatus.PAUSED
            task.error_message = ""
            task.updated_at = time.time()
            try:
                self.storage.save(task)
            except Exception as exc:
                # Log but don't re-raise: the in-memory state is already
                # correct, and the worker's eventual _on_status_changed
                # will re-save when the worker finally unwinds.
                import traceback
                print(f"FlowDownload: storage.save in pause({task_id}) failed: {exc}\n{traceback.format_exc()}", flush=True)
            self.task_updated.emit(task_id)

    def resume_all(self) -> None:
        """Resume all paused, queued, and failed tasks.

        Cancelled tasks are left alone -- the user stopped those on purpose.
        """
        for task in self.tasks.values():
            if task.status in (DownloadStatus.PAUSED, DownloadStatus.QUEUED,
                               DownloadStatus.FAILED):
                if task.id in self._workers:
                    self._resume_pending.add(task.id)
                task.status = DownloadStatus.QUEUED
                task.error_message = ""
                task.updated_at = time.time()
                self.storage.save(task)
                self.task_updated.emit(task.id)
        self._dispatch_queue()

    def cancel(self, task_id: str) -> None:
        self._resume_pending.discard(task_id)
        worker = self._workers.get(task_id)
        if worker is not None:
            # The worker's CANCELLED signal discards the partial file once
            # it has closed it (see _do_status_changed).
            worker.cancel()
        else:
            task = self.tasks.get(task_id)
            if task is not None:
                task.status = DownloadStatus.CANCELLED
                self._discard_partial(task)
                task.updated_at = time.time()
                self.storage.save(task)
                self.task_updated.emit(task_id)

    @staticmethod
    def _discard_partial(task: DownloadTask) -> None:
        """Delete a cancelled task's partial data -- nothing will ever resume it."""
        for path in (task.part_path, task.segments_path):
            try:
                os.remove(path)
            except OSError:
                pass  # never started writing, or already gone
        task.downloaded_bytes = 0

    def remove(self, task_id: str) -> None:
        self._resume_pending.discard(task_id)
        worker = self._workers.pop(task_id, None)
        if worker is not None:
            worker.cancel()
            # Wait for the thread to stop before dropping the reference. If it
            # cannot join in time, keep it alive at module scope so nothing
            # garbage-collects a running QThread -- which would trigger Qt's
            # fatal "destroyed while thread is still running" abort.
            if not worker.wait(2000) and worker.isRunning():
                _STUCK_WORKERS.append(worker)

        task = self.tasks.pop(task_id, None)
        if task is not None:
            self._discard_partial(task)

        self.storage.delete(task_id)
        self.history.pop(task_id, None)
        self._last_progress_save.pop(task_id, None)
        self.task_removed.emit(task_id)

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        self._dispatch_queue()

    def shutdown(self) -> None:
        """Stop every running download and wait for threads to exit cleanly.

        Uses pause, not cancel: both make workers exit at the next chunk
        boundary, but a cancel discards the .part file, and quitting the
        app must keep partial downloads resumable. Closes the shared
        requests session to interrupt any worker blocked in a network syscall.
        Workers that are still alive after the wait window are kept alive in
        the module-level _STUCK_WORKERS list so they are not garbage-collected
        while running -- which would trigger QThread's "destroyed while still
        running" abort. The process is exiting anyway, so leaking them is fine.
        """
        # Their PAUSED signals are ignored from here on (see
        # _do_status_changed) so the DB keeps them as downloading, which
        # _load_existing resumes on the next launch -- unlike a user pause.
        self._shutting_down = True
        self._external_poll_timer.stop()
        # Pause all workers so they exit at the next chunk boundary.
        for worker in list(self._workers.values()):
            worker.pause()
        # Close the shared session to unblock any worker stuck in a syscall.
        try:
            self._session.close()
        except Exception:
            pass
        # Wait for workers to finish. Workers stuck in network I/O will exit
        # once the socket times out (READ_TIMEOUT=10s) -- the RequestException
        # path sees the pause event and returns without retrying.
        for worker in list(self._workers.values()):
            if not worker.wait(5000):
                # Still alive (stuck in network I/O). Move to module-level
                # list so the manager's GC doesn't destroy a running QThread.
                _STUCK_WORKERS.append(worker)

    def _active_count(self) -> int:
        return sum(1 for t in self.tasks.values() if t.status in ACTIVE_STATUSES)

    def _site_limit(self) -> int:
        """Per-site connection budget; guards against a hand-edited settings.json."""
        return max(1, int(self.settings.max_connections_per_site))

    def _connections_used(self, domain: str, exclude: str | None = None) -> int:
        """Connections active downloads hold on this site's budget."""
        return sum(max(1, t.connections) for t in self.tasks.values()
                   if t.status in ACTIVE_STATUSES and t.id != exclude and domain_key(t.url) == domain)

    def _dispatch_queue(self) -> None:
        if self._shutting_down:
            return
        free_slots = self.settings.max_concurrent_downloads - self._active_count()
        if free_slots <= 0:
            return
        site_limit = self._site_limit()
        per_domain: dict[str, int] = {}  # connections in use per site
        for t in self.tasks.values():
            if t.status in ACTIVE_STATUSES:
                key = domain_key(t.url)
                per_domain[key] = per_domain.get(key, 0) + max(1, t.connections)
        # Only auto-start QUEUED tasks. PAUSED tasks must wait for an explicit
        # Resume click — otherwise the dispatcher re-spawns them immediately
        # after the user clicks Pause, which made Pause All look broken.
        candidates = sorted(
            (t for t in self.tasks.values()
             if t.status == DownloadStatus.QUEUED),
            key=lambda t: t.created_at,
        )
        for task in candidates:
            if free_slots <= 0:
                break
            if task.id in self._workers:
                continue  # previous worker still unwinding; never run two on one .part
            # A domain at its cap is skipped, not a stopping point -- a queued
            # task from another site can still take the free slot.
            key = domain_key(task.url)
            if per_domain.get(key, 0) >= site_limit:
                continue
            self._spawn(task.id)
            per_domain[key] = per_domain.get(key, 0) + max(1, task.connections)
            free_slots -= 1

    def _spawn(self, task_id: str) -> None:
        task = self.tasks[task_id]
        # Grant what's left of the site's connection budget, up to the
        # per-download setting. The worker reports back (connections_resolved)
        # if it ends up using fewer, which frees the rest for other downloads.
        wanted = max(1, min(int(self.settings.connections_per_download), MAX_CONNECTIONS_PER_DOWNLOAD))
        free = self._site_limit() - self._connections_used(domain_key(task.url), exclude=task_id)
        task.connections = max(1, min(wanted, free))
        task.status = DownloadStatus.DOWNLOADING
        task.error_message = ""
        task.updated_at = time.time()
        self.storage.save(task)
        if task.downloaded_bytes > 0:
            self._log(task_id, "info", f"Resuming from byte {task.downloaded_bytes:,}")
        else:
            self._log(task_id, "info", "Connecting")
        self.task_updated.emit(task_id)

        worker = DownloadWorker(
            task.id, task.url, task.dest_path, task.etag, task.last_modified,
            task.cookie_header, self._session, referrer=task.referrer,
            max_connections=task.connections,
        )
        worker.progress.connect(self._on_progress)
        worker.status_changed.connect(self._on_status_changed)
        worker.metadata_resolved.connect(self._on_metadata_resolved)
        worker.digest_computed.connect(self._on_digest_computed)
        worker.size_unverified.connect(self._on_size_unverified)
        worker.connections_resolved.connect(self._on_connections_resolved)
        worker.finished.connect(lambda tid=task_id: self._on_worker_finished(tid))
        self._workers[task_id] = worker
        worker.start()

    def _on_worker_finished(self, task_id: str) -> None:
        self._resume_pending.discard(task_id)
        worker = self._workers.pop(task_id, None)
        if worker is not None:
            # Wait for the thread to actually stop before the Python wrapper can
            # be garbage-collected. Without this, Python's GC can call the
            # Shiboken deallocator while the C++ thread is still running, which
            # triggers QThread's "destroyed while thread is still running" abort.
            # The finished signal fires when run() returns, but the native
            # thread teardown can lag, so don't delete the wrapper until the
            # join is confirmed. If it can't join in time (e.g. blocked in a
            # syscall), stash it at module scope so nothing GC's a running
            # QThread.
            if worker.wait(1000):
                worker.deleteLater()
            else:
                _STUCK_WORKERS.append(worker)
        self._dispatch_queue()

    def _on_progress(self, task_id: str, downloaded: int, total: int, speed: float) -> None:
        try:
            self._do_progress(task_id, downloaded, total, speed)
        except Exception as exc:
            self._log(task_id, "error", f"progress handler error: {exc}")

    def _do_progress(self, task_id: str, downloaded: int, total: int, speed: float) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return
        task.downloaded_bytes = downloaded
        task.total_bytes = total
        task.speed_bps = speed
        task.updated_at = time.time()
        if task.updated_at - self._last_progress_save.get(task_id, 0.0) >= PROGRESS_SAVE_INTERVAL:
            self.storage.save(task)
            self._last_progress_save[task_id] = task.updated_at
        self.task_updated.emit(task_id)

    def _on_metadata_resolved(self, task_id: str, total_bytes: int, etag: str, last_modified: str) -> None:
        try:
            self._do_metadata_resolved(task_id, total_bytes, etag, last_modified)
        except Exception as exc:
            self._log(task_id, "error", f"metadata handler error: {exc}")

    def _do_metadata_resolved(self, task_id: str, total_bytes: int, etag: str, last_modified: str) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return
        task.total_bytes = total_bytes
        task.etag = etag
        task.last_modified = last_modified
        task.updated_at = time.time()
        self.storage.save(task)
        if etag or last_modified:
            self._log(task_id, "ok", "Server accepts byte ranges — resumable")
        else:
            self._log(task_id, "warn", "No ETag/Last-Modified from server — resume not guaranteed")
        self.task_updated.emit(task_id)

    _STATUS_LOG_LEVEL = {
        DownloadStatus.RETRYING: "warn",
        DownloadStatus.FAILED: "error",
        DownloadStatus.COMPLETED: "ok",
        DownloadStatus.PAUSED: "info",
        DownloadStatus.CANCELLED: "info",
    }

    def _on_status_changed(self, task_id: str, status_value: str, error_message: str) -> None:
        try:
            self._do_status_changed(task_id, status_value, error_message)
        except Exception as exc:
            self._log(task_id, "error", f"status_changed handler error: {exc}")

    def _do_status_changed(self, task_id: str, status_value: str, error_message: str) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return
        status = DownloadStatus(status_value)
        if status == DownloadStatus.PAUSED and self._shutting_down:
            return  # quit, not a user pause -- keep it resumable on relaunch
        if status == DownloadStatus.PAUSED and task_id in self._resume_pending:
            # Resumed while this worker was still pausing: stay queued.
            status = DownloadStatus.QUEUED
        if status == DownloadStatus.CANCELLED:
            self._discard_partial(task)
        if status not in ACTIVE_STATUSES:
            task.connections = 0  # budget is only held while running
        task.status = status
        task.error_message = error_message
        task.speed_bps = 0.0
        task.updated_at = time.time()
        self.storage.save(task)
        level = self._STATUS_LOG_LEVEL.get(status)
        if level is not None:
            message = error_message or {
                DownloadStatus.COMPLETED: "Finished",
                DownloadStatus.PAUSED: "Paused",
                DownloadStatus.CANCELLED: "Cancelled",
            }.get(status, status.value)
            self._log(task_id, level, message)
        self.task_updated.emit(task_id)
        if task.status in (DownloadStatus.COMPLETED, DownloadStatus.FAILED):
            self.task_finished.emit(task_id, status_value)

    def _on_digest_computed(self, task_id: str, digest_sha256: str, verified: bool | None) -> None:
        """Persist the worker-computed SHA-256 and the verification badge.

        `verified` is a three-way result from the worker: True only when
        the server advertised a Digest header (RFC 9530) and it matched
        what we computed; None when the server sent no header at all (most
        don't -- the common, expected case, not an error); False when it
        DID send one and it didn't match -- a real integrity failure. The
        stored `digest_verified` bool collapses None/False to False (the
        "verified" badge should only ever mean a confirmed match), but the
        log -- which is what a user actually sees -- keeps the two apart,
        since a mismatch means something is genuinely wrong with the file.
        """
        try:
            task = self.tasks.get(task_id)
            if task is None:
                return
            task.digest_sha256 = digest_sha256
            task.digest_verified = bool(verified)
            task.updated_at = time.time()
            self.storage.save(task)
            if verified is True:
                self._log(task_id, "ok", f"Verified: SHA-256 {digest_sha256[:12]}…")
            elif verified is False:
                self._log(
                    task_id, "error",
                    f"Integrity check FAILED: SHA-256 {digest_sha256[:12]}… does not match the "
                    "server's Digest header — the file may be corrupted or altered in transit",
                )
            else:
                self._log(task_id, "info", f"SHA-256 {digest_sha256[:12]}… (no server Digest to verify against)")
            self.task_updated.emit(task_id)
        except Exception as exc:
            self._log(task_id, "error", f"digest handler error: {exc}")

    def _on_connections_resolved(self, task_id: str, connections: int) -> None:
        try:
            task = self.tasks.get(task_id)
            if task is None or task.status not in ACTIVE_STATUSES:
                return
            previous, task.connections = task.connections, connections
            if connections > 1:
                self._log(task_id, "info", f"Downloading over {connections} connections")
            self.task_updated.emit(task_id)
            if connections < previous:
                self._dispatch_queue()  # budget was freed for other downloads
        except Exception as exc:
            self._log(task_id, "error", f"connections handler error: {exc}")

    def _on_size_unverified(self, task_id: str) -> None:
        self._log(
            task_id, "warn",
            "Size not verified — the server didn't report a length, so a truncated "
            "transfer can't be told apart from a complete one for this file",
        )

    def check_dedupe(self, url: str) -> DedupeResult:
        """Check whether a URL is already represented in our completed history.

        "Same URL" is the simple case. A more aggressive match would
        fingerprint the content (SHA-256 of completed tasks) and compare,
        but URLs are deterministic and avoid any hashing cost on the
        dedupe path. The UI surfaces this as "Already downloaded" in the
        extension popup.
        """
        for task in self.tasks.values():
            if task.status == DownloadStatus.COMPLETED and task.url == url:
                return DedupeResult(
                    is_duplicate=True,
                    existing_task_id=task.id,
                    existing_path=task.dest_path,
                    existing_size=task.total_bytes,
                    existing_digest=task.digest_sha256 or None,
                )
        return DedupeResult(is_duplicate=False)

    def apply_auto_folder(self, filename: str, base_dir: str | None = None) -> str:
        """Pick a subfolder under the download dir based on the file's extension.

        Returns the *unchanged* path when auto-foldering is disabled in
        settings (so callers can pass a plain folder and the rule is opt-in).
        When enabled, returns "base_dir/<type>/<filename>".
        """
        base = base_dir or self.settings.download_dir
        if not getattr(self.settings, "auto_folder_by_type", False):
            return os.path.join(base, filename)
        return apply_auto_folder_for_path(filename, base)


def apply_auto_folder_for_path(filename: str, base_dir: str) -> str:
    """Pure function form (no Settings required) used by the native host.

    The native host runs without an instance of DownloadManager (it only
    needs to write a row to the DB), so it can't call the instance method
    — but the routing rules live here so both paths stay in sync.
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext in AUTO_FOLDER_VIDEO:
        sub = "Video"
    elif ext in AUTO_FOLDER_AUDIO:
        sub = "Audio"
    elif ext in AUTO_FOLDER_DOCUMENT:
        sub = "Documents"
    elif ext in AUTO_FOLDER_ARCHIVE:
        sub = "Archives"
    else:
        sub = "Other"
    return os.path.join(base_dir, sub, filename)


_AUTO_FOLDER_SUBDIRS = {"Video", "Audio", "Documents", "Archives", "Other"}


def prune_empty_auto_folder_dir(dest_path: str, download_dir: str) -> None:
    """Remove the auto-folder subfolder (Video/Audio/.../Other) a just-
    deleted file lived in, if that was the last thing in it.

    apply_auto_folder_for_path creates these on demand but nothing ever
    cleaned one up, so a download folder accumulates empty Video/Audio/...
    directories forever as files move through it. Deliberately narrow: only
    touches a directory whose name is one of the fixed auto-folder names
    and whose *parent* is exactly download_dir, and os.rmdir refuses to
    remove a non-empty directory -- so this can never reach outside that
    known structure or delete anything that still has content.
    """
    parent = os.path.dirname(dest_path)
    if os.path.basename(parent) not in _AUTO_FOLDER_SUBDIRS:
        return
    if os.path.dirname(parent) != os.path.normpath(download_dir):
        return
    try:
        os.rmdir(parent)
    except OSError:
        pass  # not empty, or already gone -- either way, nothing to do
