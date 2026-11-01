from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class DownloadStatus(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class DownloadTask:
    url: str
    dest_path: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    filename: str = ""
    total_bytes: int = 0
    downloaded_bytes: int = 0
    status: DownloadStatus = DownloadStatus.QUEUED
    error_message: str = ""
    speed_bps: float = 0.0
    etag: str = ""
    last_modified: str = ""
    # Cookies captured from the browser at the moment a Chrome-intercepted
    # download was handed off, so sites that require a login can still be
    # fetched -- a bare requests.Session() has no session cookie otherwise.
    cookie_header: str = ""
    # SHA-256 hex digest of the completed file. Used for deduplication:
    # if two downloads share a URL or path AND a matching digest, they are
    # the same file even if their names differ.
    digest_sha256: str = ""
    # Whether the server's Digest header (if any) matched what we computed.
    # False means the server had no Digest header; True means it matched;
    # a dedicated "failed" status for mismatch is overkill since we log it.
    digest_verified: bool = False
    # A checksum the user typed when adding the download, as "algo:hex"
    # (checksum.parse_expected); "" when none was given.
    expected_checksum: str = ""
    # Result of the last integrity check: "" = not checked, "verified",
    # or "mismatch". A mismatch keeps the file (status stays COMPLETED)
    # so the user can decide; the detail sheet shows it in red.
    checksum_status: str = ""
    # What it was checked against: "you", "server" or "checksum file".
    checksum_source: str = ""
    # Page the download was started from, sent as the Referer header. Many
    # CDNs refuse (or throttle) requests that arrive without one.
    referrer: str = ""
    # Whether the server has shown it honours byte ranges (a 206, or
    # Accept-Ranges: bytes). Resuming also needs etag/last_modified.
    accept_ranges: bool = False
    # Connections currently in use (in memory only, never persisted): counts
    # against the per-site connection budget while the task is active.
    connections: int = 0
    # A split download's file map, [[start, done, end], ...] by byte offset
    # (in memory only; the sidecar is the durable copy). Empty otherwise.
    segments: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def part_path(self) -> str:
        return self.dest_path + ".part"

    @property
    def segments_path(self) -> str:
        # Split-file progress sidecar; see app/engine/segmented.py.
        return self.part_path + ".segments"

    @property
    def progress(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return min(1.0, self.downloaded_bytes / self.total_bytes)

    def to_row(self) -> tuple:
        return (
            self.id,
            self.url,
            self.dest_path,
            self.filename,
            self.total_bytes,
            self.downloaded_bytes,
            self.status.value,
            self.error_message,
            self.etag,
            self.last_modified,
            self.cookie_header,
            self.digest_sha256,
            self.digest_verified,
            self.referrer,
            self.accept_ranges,
            self.expected_checksum,
            self.checksum_status,
            self.checksum_source,
            self.created_at,
            self.updated_at,
        )

    @classmethod
    def from_row(cls, row: tuple) -> DownloadTask:
        (
            id_, url, dest_path, filename, total_bytes, downloaded_bytes,
            status, error_message, etag, last_modified, cookie_header,
            digest_sha256, digest_verified, referrer, accept_ranges,
            expected_checksum, checksum_status, checksum_source, created_at, updated_at,
        ) = row
        return cls(
            id=id_,
            url=url,
            dest_path=dest_path,
            filename=filename,
            total_bytes=total_bytes,
            downloaded_bytes=downloaded_bytes,
            status=DownloadStatus(status),
            error_message=error_message,
            etag=etag,
            last_modified=last_modified,
            cookie_header=cookie_header,
            digest_sha256=digest_sha256 or "",
            digest_verified=bool(digest_verified),
            referrer=referrer or "",
            accept_ranges=bool(accept_ranges),
            expected_checksum=expected_checksum or "",
            checksum_status=checksum_status or "",
            checksum_source=checksum_source or "",
            created_at=created_at,
            updated_at=updated_at,
        )
