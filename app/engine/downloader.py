from __future__ import annotations

import os
import re
import threading
import time

import requests
from PySide6.QtCore import QThread, Signal

from app.engine.models import DownloadStatus
from app.engine.retry_policy import compute_retry_delay

CHUNK_SIZE = 64 * 1024
CONNECT_TIMEOUT = 10
# Deliberately short: this is the per-read socket timeout, so it also bounds
# how long a pause request can be stuck waiting behind a stalled (but not yet
# disconnected) read, and how quickly a truly dead connection gets detected
# and handed to the retry policy instead of hanging silently.
READ_TIMEOUT = 10
PROGRESS_EMIT_INTERVAL = 0.25
SPEED_SMOOTHING = 0.3  # EMA factor: higher = more responsive, lower = steadier ETA
CONTENT_RANGE_TOTAL_RE = re.compile(r"/(\d+)$")


class Paused(Exception):
    """Raised internally to unwind out of the streaming loop on pause/cancel."""


class DownloadWorker(QThread):
    progress = Signal(str, int, int, float)  # task_id, downloaded, total, speed_bps
    status_changed = Signal(str, str, str)  # task_id, status_value, error_message
    metadata_resolved = Signal(str, int, str, str)  # task_id, total_bytes, etag, last_modified

    def __init__(self, task_id: str, url: str, dest_path: str, etag: str, last_modified: str,
                 cookie_header: str, session: requests.Session, parent=None):
        super().__init__(parent)
        # Deliberately plain values/instance attributes, not a reference to the
        # manager's shared DownloadTask: this thread mutates them freely as it
        # downloads, and nothing outside this thread ever touches them. All
        # communication back to the manager (which owns the real DownloadTask,
        # on the main thread) goes exclusively through Qt signals -- sharing
        # one mutable object across threads without a lock previously let the
        # main thread's belated processing of a stale progress signal
        # overwrite a downloaded_bytes value the worker had already moved
        # past, permanently undercounting the rest of the download.
        self.task_id = task_id
        self.url = url
        self.dest_path = dest_path
        self.part_path = dest_path + ".part"
        self.etag = etag
        self.last_modified = last_modified
        self.cookie_header = cookie_header
        self._session = session
        self._pause_event = threading.Event()
        self._cancel_event = threading.Event()
        self._smoothed_speed = 0.0
        self._total_bytes = 0

    def pause(self) -> None:
        self._pause_event.set()

    def cancel(self) -> None:
        self._cancel_event.set()
        self._pause_event.set()

    def run(self) -> None:
        attempt = 0
        while True:
            try:
                downloaded = self._attempt_download()
                self._finalize(downloaded)
                self.status_changed.emit(self.task_id, DownloadStatus.COMPLETED.value, "")
                return
            except Paused:
                status = DownloadStatus.CANCELLED if self._cancel_event.is_set() else DownloadStatus.PAUSED
                self.status_changed.emit(self.task_id, status.value, "")
                return
            except requests.exceptions.RequestException as exc:
                attempt += 1
                delay = compute_retry_delay(attempt)
                if delay is None:
                    self.status_changed.emit(self.task_id, DownloadStatus.FAILED.value, str(exc))
                    return
                self._smoothed_speed = 0.0
                self.status_changed.emit(
                    self.task_id, DownloadStatus.RETRYING.value,
                    f"{exc.__class__.__name__} — retrying in {delay:.0f}s",
                )
                if self._interruptible_sleep(delay):
                    status = DownloadStatus.CANCELLED if self._cancel_event.is_set() else DownloadStatus.PAUSED
                    self.status_changed.emit(self.task_id, status.value, "")
                    return
            except OSError as exc:
                self.status_changed.emit(self.task_id, DownloadStatus.FAILED.value, str(exc))
                return

    def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleep in small slices so pause/cancel take effect promptly.

        Returns True if a pause/cancel arrived during the sleep.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._pause_event.wait(timeout=0.5):
                return True
        return False

    def _attempt_download(self) -> int:
        """Returns the total bytes on disk once this attempt's streaming completes."""
        existing_bytes = os.path.getsize(self.part_path) if os.path.exists(self.part_path) else 0

        headers = {}
        if self.cookie_header:
            headers["Cookie"] = self.cookie_header
        if existing_bytes > 0:
            headers["Range"] = f"bytes={existing_bytes}-"
            # If-Range makes the resume conditional: the server only honors the
            # Range and sends 206 if the file is unchanged since we started it.
            # If it changed (different ETag/Last-Modified), it ignores Range and
            # sends 200 with the full new body instead of silently splicing new
            # content onto our stale bytes.
            if self.etag:
                headers["If-Range"] = self.etag
            elif self.last_modified:
                headers["If-Range"] = self.last_modified

        response = self._session.get(
            self.url,
            headers=headers,
            stream=True,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        try:
            if response.status_code == 206:
                mode = "ab"
                # A missing Content-Length (e.g. chunked transfer encoding,
                # which some servers use even for a Range response) means
                # the remaining size is unknown -- treating that as "0 more
                # bytes coming" would understate the total, and _finalize's
                # size check would then fail a download that actually
                # completed correctly.
                content_length_header = response.headers.get("Content-Length")
                self._total_bytes = (
                    existing_bytes + int(content_length_header) if content_length_header is not None else 0
                )
            elif response.status_code == 200:
                # Server ignored Range (unsupported, or the resource changed
                # under an If-Range check): restart from byte 0, whatever we
                # had on disk is stale.
                mode = "wb"
                existing_bytes = 0
                self._total_bytes = int(response.headers.get("Content-Length", 0))
                self.etag = response.headers.get("ETag", "")
                self.last_modified = response.headers.get("Last-Modified", "")
            elif response.status_code == 416:
                # Range Not Satisfiable: our .part is already >= the full size,
                # most likely we finished writing it but crashed before the
                # rename-to-final-name step. Trust Content-Range's total and
                # let _finalize verify + rename rather than re-downloading.
                total = self._parse_range_total(response.headers.get("Content-Range", ""))
                if total is not None and existing_bytes >= total:
                    self._total_bytes = total
                    return existing_bytes
                response.raise_for_status()
                raise requests.exceptions.RequestException(f"Unexpected 416 without resolvable total")
            else:
                response.raise_for_status()
                raise requests.exceptions.RequestException(f"Unexpected status {response.status_code}")

            self.metadata_resolved.emit(self.task_id, self._total_bytes, self.etag, self.last_modified)
            return self._stream_to_file(response, mode, existing_bytes)
        finally:
            response.close()

    @staticmethod
    def _parse_range_total(content_range: str) -> int | None:
        match = CONTENT_RANGE_TOTAL_RE.search(content_range)
        return int(match.group(1)) if match else None

    def _stream_to_file(self, response, mode: str, existing_bytes: int) -> int:
        downloaded = existing_bytes
        downloaded_since_emit = 0
        last_emit = time.monotonic()

        with open(self.part_path, mode) as f:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if self._pause_event.is_set():
                    raise Paused()
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                downloaded_since_emit += len(chunk)

                now = time.monotonic()
                elapsed = now - last_emit
                if elapsed >= PROGRESS_EMIT_INTERVAL:
                    instantaneous = downloaded_since_emit / elapsed
                    self._smoothed_speed = (
                        instantaneous if self._smoothed_speed == 0.0
                        else SPEED_SMOOTHING * instantaneous + (1 - SPEED_SMOOTHING) * self._smoothed_speed
                    )
                    self.progress.emit(self.task_id, downloaded, self._total_bytes, self._smoothed_speed)
                    downloaded_since_emit = 0
                    last_emit = now

        return downloaded

    def _finalize(self, downloaded: int) -> None:
        if self._total_bytes and os.path.getsize(self.part_path) != self._total_bytes:
            raise OSError(
                f"Downloaded size ({os.path.getsize(self.part_path)}) does not match "
                f"expected size ({self._total_bytes})"
            )
        os.replace(self.part_path, self.dest_path)
        self.progress.emit(self.task_id, downloaded, self._total_bytes, 0.0)
