from __future__ import annotations

import binascii
import hashlib
import hmac
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


# 4xx statuses that mean "retrying won't help" -- the resource is gone,
# forbidden, or the request itself is malformed, none of which changes by
# waiting and asking again. 408 (Request Timeout) and 429 (Too Many
# Requests) are deliberately excluded: those are the server explicitly
# asking for a retry.
PERMANENT_HTTP_STATUSES = {400, 401, 403, 404, 405, 406, 410, 451}


def _is_permanent_http_error(exc: requests.exceptions.RequestException) -> bool:
    response = getattr(exc, "response", None)
    return response is not None and response.status_code in PERMANENT_HTTP_STATUSES


class Paused(Exception):
    """Raised internally to unwind out of the streaming loop on pause/cancel."""


class DownloadWorker(QThread):
    progress = Signal(str, int, int, float)  # task_id, downloaded, total, speed_bps
    status_changed = Signal(str, str, str)  # task_id, status_value, error_message
    metadata_resolved = Signal(str, int, str, str)  # task_id, total_bytes, etag, last_modified
    digest_computed = Signal(str, str, object)  # task_id, sha256_hex, verified (True/False/None)
    size_unverified = Signal(str)  # task_id -- server gave no way to confirm the download size

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
        # Parsed from the server's Digest header on the first response, if present.
        # Stored as hex so it can be compared directly against our computed hash.
        self._server_digest_sha256: str | None = None
        # Set when we've emitted RETRYING; cleared (and DOWNLOADING emitted)
        # once a reconnect is accepted, so the status doesn't stay stuck on
        # RETRYING for the rest of a download that recovered.
        self._retrying = False
        # Bytes received on the current connection. A drop after real
        # progress resets the retry counter, so a download that keeps
        # moving is never failed just for reconnecting often.
        self._attempt_bytes = 0

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
                # Compute the SHA-256 digest after the file is on disk and
                # renamed to its final name. This is a best-effort signal -- if
                # hashing fails (OSError), the download is still valid; the
                # manager will simply not record a digest for this task.
                try:
                    digest = _sha256_file(self.dest_path)
                    # verified is the badge surface: True when the server's
                    # Digest header (RFC 9530) was present and matched what
                    # we computed, None when the server gave no header (most
                    # don't -- not an error), False when it sent one and it
                    # DIDN'T match -- a real integrity failure (MITM or
                    # corrupted upstream), surfaced distinctly in the
                    # download log rather than folded into the None case.
                    verified = self._verify_against_server(digest)
                    self.digest_computed.emit(self.task_id, digest, verified)
                except OSError:
                    pass
                self.status_changed.emit(self.task_id, DownloadStatus.COMPLETED.value, "")
                return
            except Paused:
                status = DownloadStatus.CANCELLED if self._cancel_event.is_set() else DownloadStatus.PAUSED
                self.status_changed.emit(self.task_id, status.value, "")
                return
            except requests.exceptions.RequestException as exc:
                if _is_permanent_http_error(exc):
                    self.status_changed.emit(self.task_id, DownloadStatus.FAILED.value, str(exc))
                    return
                if self._attempt_bytes > 0:
                    attempt = 0  # this connection made progress -- start the backoff over
                attempt += 1
                delay = compute_retry_delay(attempt)
                if delay is None:
                    self.status_changed.emit(self.task_id, DownloadStatus.FAILED.value, str(exc))
                    return
                self._smoothed_speed = 0.0
                self._retrying = True
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
        self._attempt_bytes = 0
        existing_bytes = os.path.getsize(self.part_path) if os.path.exists(self.part_path) else 0

        # A resume is only safe when we can pair it with If-Range: that's
        # what makes the server reject stale bytes (sending 200 + the full
        # body instead of 206) if the file changed since we started. With
        # neither ETag nor Last-Modified from the original response, there
        # is nothing to validate against -- a bare Range request could get
        # silently honored against a *different* file underneath, splicing
        # new bytes onto old ones with no error. Rather than risk that,
        # treat the partial file as unusable and restart from scratch, same
        # as the "server ignored Range" 200-response branch below already
        # does when a resume attempt IS made but rejected.
        can_resume = existing_bytes > 0 and bool(self.etag or self.last_modified)
        if existing_bytes > 0 and not can_resume:
            existing_bytes = 0

        headers = {}
        if self.cookie_header:
            headers["Cookie"] = self.cookie_header
        if can_resume:
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

            # Capture the server's Digest header (RFC 9530) for verification.
            # Only relevant when the file is being written fresh (mode "wb");
            # a 206 response covers only a suffix, so any Digest it advertises
            # would describe the whole file, not just the partial content.
            if mode == "wb":
                self._server_digest_sha256 = _parse_sha256_digest(response.headers.get("Digest"))

            self.metadata_resolved.emit(self.task_id, self._total_bytes, self.etag, self.last_modified)
            if self._retrying:
                self._retrying = False
                self.status_changed.emit(self.task_id, DownloadStatus.DOWNLOADING.value, "")
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

        # The destination folder may not exist yet: an auto-folder subfolder
        # (Video/, Audio/, ...) on first use, or a download folder the user
        # deleted since choosing it.
        os.makedirs(os.path.dirname(self.part_path), exist_ok=True)
        with open(self.part_path, mode) as f:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if self._pause_event.is_set():
                    raise Paused()
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                downloaded_since_emit += len(chunk)
                self._attempt_bytes += len(chunk)

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

    def _verify_against_server(self, computed_hex: str) -> bool | None:
        return _verify_against_server(self._server_digest_sha256, computed_hex)

    def _finalize(self, downloaded: int) -> None:
        if self._total_bytes and os.path.getsize(self.part_path) != self._total_bytes:
            raise OSError(
                f"Downloaded size ({os.path.getsize(self.part_path)}) does not match "
                f"expected size ({self._total_bytes})"
            )
        if not self._total_bytes:
            # No Content-Length AND not chunked (chunked truncation is
            # already caught earlier -- iter_content raises
            # ChunkedEncodingError if the stream ends mid-chunk, which
            # propagates out of _attempt_download as a RequestException and
            # goes through the normal retry path, never reaching here).
            # This is the rare leftover case: an old-style connection-close
            # -delimited body, where nothing in the protocol distinguishes
            # "the server finished" from "the connection just dropped" --
            # there's genuinely no length signal to verify against, so
            # surface that honestly instead of silently completing as if
            # the size had been checked.
            self.size_unverified.emit(self.task_id)
        os.replace(self.part_path, self.dest_path)
        self.progress.emit(self.task_id, downloaded, self._total_bytes, 0.0)


def _sha256_file(path: str, chunk_size: int = 64 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


_DIGEST_SHA256_RE = re.compile(r"sha-256=([A-Za-z0-9+/=]+)", re.IGNORECASE)


def _parse_sha256_digest(header_value: str | None) -> str | None:
    """Extract a base64 sha-256 value from an RFC 9530 Digest header.

    The header looks like "Digest: sha-256=X48E9qOokqqrvdts8nOJRJN3OWDUoyWxBf7kbu9DBPE=".
    We return the base64 form (matching what hashlib.digest() produces),
    so the manager can compare against a base64 server value. When the
    server doesn't send one (most don't), returns None.
    """
    if not header_value:
        return None
    match = _DIGEST_SHA256_RE.search(header_value)
    return match.group(1) if match else None


def _verify_against_server(server_b64: str | None, computed_hex: str) -> bool | None:
    """Compare computed hex digest against server's base64 digest.

    Three-way result, not a plain bool -- "the server sent no Digest
    header" and "the server sent one and it doesn't match" are very
    different situations (the second is a real integrity failure) and
    callers need to tell them apart:
      * None  -- no server value to compare against
      * True  -- match
      * False -- mismatch
    Imported lazily because it's only used post-download and pulling in
    base64 on every chunk-write would be wasteful.
    """
    if not server_b64:
        return None
    import base64
    try:
        server_bytes = base64.b64decode(server_b64)
        computed_bytes = bytes.fromhex(computed_hex)
    except (ValueError, binascii.Error):
        return False
    return hmac.compare_digest(server_bytes, computed_bytes)
