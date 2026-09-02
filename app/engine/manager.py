from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

import requests
from PySide6.QtCore import QObject, QTimer, Signal

from app.engine.downloader import DownloadWorker
from app.engine.models import DownloadStatus, DownloadTask
from app.engine.paths import dedupe_path
from app.engine.settings import Settings
from app.engine.storage import Storage

ACTIVE_STATUSES = (DownloadStatus.DOWNLOADING, DownloadStatus.RETRYING)
EXTERNAL_POLL_INTERVAL_MS = 2000
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
        self._dispatch_queue()

    def _load_existing(self) -> None:
        for task in self.storage.load_all():
            # Anything mid-flight when the app last closed is not actually
            # running anymore; reflect that, and re-sync progress from the
            # .part file on disk since that's the real source of truth.
            # Tasks that were only QUEUED (never got a worker slot) stay
            # queued -- they'll be picked up by _dispatch_queue() below,
            # same as if they'd just been added.
            if task.status in ACTIVE_STATUSES:
                task.status = DownloadStatus.PAUSED
                self._log(task.id, "info", "Interrupted by app restart")
            if os.path.exists(task.part_path):
                task.downloaded_bytes = os.path.getsize(task.part_path)
            task.updated_at = time.time()
            self.storage.save(task)
            self.tasks[task.id] = task
        self._dispatch_queue()

    def _log(self, task_id: str, level: str, message: str) -> None:
        entries = self.history.setdefault(task_id, [])
        entries.append({"time": time.time(), "level": level, "message": message})
        del entries[:-HISTORY_LIMIT]

    def get_history(self, task_id: str) -> list[dict]:
        return list(self.history.get(task_id, []))

    def add_download(self, url: str, dest_path: str) -> str:
        # Read from storage rather than self.tasks: a download the Chrome
        # extension just queued via the native host lands in storage
        # immediately but isn't polled into self.tasks for up to
        # EXTERNAL_POLL_INTERVAL_MS, so self.tasks alone could still miss it.
        reserved = {t.dest_path for t in self.storage.load_all()}
        dest_path = dedupe_path(dest_path, reserved)
        task = DownloadTask(
            url=url, dest_path=dest_path, filename=os.path.basename(dest_path),
            status=DownloadStatus.QUEUED,
        )
        self.tasks[task.id] = task
        self.storage.save(task)
        self._log(task.id, "info", "Added to queue")
        self.task_added.emit(task.id)
        self._dispatch_queue()
        return task.id

    def start(self, task_id: str) -> None:
        """User explicitly wants this task running now (Resume / Retry click)."""
        existing = self._workers.get(task_id)
        if existing is not None and existing.isRunning():
            return
        if self._active_count() >= self.settings.max_concurrent_downloads:
            task = self.tasks[task_id]
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
        """Resume all paused, queued, failed, and cancelled tasks."""
        for task in self.tasks.values():
            if task.status in (DownloadStatus.PAUSED, DownloadStatus.QUEUED,
                               DownloadStatus.FAILED, DownloadStatus.CANCELLED):
                task.status = DownloadStatus.QUEUED
                task.error_message = ""
                task.updated_at = time.time()
                self.storage.save(task)
                self.task_updated.emit(task.id)
        self._dispatch_queue()

    def cancel(self, task_id: str) -> None:
        worker = self._workers.get(task_id)
        if worker is not None:
            worker.cancel()
        else:
            task = self.tasks.get(task_id)
            if task is not None:
                task.status = DownloadStatus.CANCELLED
                task.updated_at = time.time()
                self.storage.save(task)
                self.task_updated.emit(task_id)

    def remove(self, task_id: str) -> None:
        worker = self._workers.get(task_id)
        if worker is not None:
            worker.cancel()
            worker.wait(2000)

        task = self.tasks.pop(task_id, None)
        if task is not None and os.path.exists(task.part_path):
            os.remove(task.part_path)

        self.storage.delete(task_id)
        self.history.pop(task_id, None)
        self.task_removed.emit(task_id)

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        self._dispatch_queue()

    def shutdown(self) -> None:
        """Cancel every running download and wait for threads to exit cleanly.

        Uses cancel (not pause) so workers exit at the next chunk boundary
        rather than leaving a half-written .part file. Closes the shared
        requests session to interrupt any worker blocked in a network syscall.
        Workers that are still alive after the wait window are kept alive in
        the module-level _STUCK_WORKERS list so they are not garbage-collected
        while running -- which would trigger QThread's "destroyed while still
        running" abort. The process is exiting anyway, so leaking them is fine.
        """
        # Cancel all workers so they exit at the next chunk boundary.
        for worker in list(self._workers.values()):
            worker.cancel()
        # Close the shared session to unblock any worker stuck in a syscall.
        try:
            self._session.close()
        except Exception:
            pass
        # Wait for workers to finish. Workers stuck in network I/O will exit
        # once the socket times out (READ_TIMEOUT=10s) -- the RequestException
        # path sees the cancel event and returns via the Paused branch.
        for worker in list(self._workers.values()):
            if not worker.wait(5000):
                # Still alive (stuck in network I/O). Move to module-level
                # list so the manager's GC doesn't destroy a running QThread.
                _STUCK_WORKERS.append(worker)

    def _active_count(self) -> int:
        return sum(1 for t in self.tasks.values() if t.status in ACTIVE_STATUSES)

    def _dispatch_queue(self) -> None:
        free_slots = self.settings.max_concurrent_downloads - self._active_count()
        if free_slots <= 0:
            return
        # Only auto-start QUEUED tasks. PAUSED tasks must wait for an explicit
        # Resume click — otherwise the dispatcher re-spawns them immediately
        # after the user clicks Pause, which made Pause All look broken.
        candidates = sorted(
            (t for t in self.tasks.values()
             if t.status == DownloadStatus.QUEUED),
            key=lambda t: t.created_at,
        )
        for task in candidates[:free_slots]:
            self._spawn(task.id)

    def _spawn(self, task_id: str) -> None:
        task = self.tasks[task_id]
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
            task.cookie_header, self._session,
        )
        worker.progress.connect(self._on_progress)
        worker.status_changed.connect(self._on_status_changed)
        worker.metadata_resolved.connect(self._on_metadata_resolved)
        worker.digest_computed.connect(self._on_digest_computed)
        worker.finished.connect(lambda tid=task_id: self._on_worker_finished(tid))
        self._workers[task_id] = worker
        worker.start()

    def _on_worker_finished(self, task_id: str) -> None:
        worker = self._workers.pop(task_id, None)
        if worker is not None:
            worker.deleteLater()
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
        self.storage.save(task)
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

    def _on_digest_computed(self, task_id: str, digest_sha256: str, verified: bool) -> None:
        """Persist the worker-computed SHA-256 and the verification badge.

        `verified` is True only when the server advertised a Digest header
        (RFC 9530) and the value matched what we computed. Most servers
        don't send Digest, so verified=False is the common, expected case —
        not an error. The UI uses this to decide whether to draw the
        "verified" badge or leave the digest unbadged.
        """
        try:
            task = self.tasks.get(task_id)
            if task is None:
                return
            task.digest_sha256 = digest_sha256
            task.digest_verified = verified
            task.updated_at = time.time()
            self.storage.save(task)
            if verified:
                self._log(task_id, "ok", f"Verified: SHA-256 {digest_sha256[:12]}…")
            else:
                self._log(task_id, "info", f"SHA-256 {digest_sha256[:12]}… (no server Digest to verify against)")
            self.task_updated.emit(task_id)
        except Exception as exc:
            self._log(task_id, "error", f"digest handler error: {exc}")

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
