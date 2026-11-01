from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.engine.models import DownloadTask
from app.engine.paths import validate_categories
from app.engine.settings import Settings

# Bounds for the speed limit, matched by the UI's field (SPEED_MIN_KB/SPEED_MAX_KB in index.html).
MIN_SPEED_LIMIT_BPS = 16 * 1024
MAX_SPEED_LIMIT_BPS = 10 * 1024 ** 3


class DownloadOut(BaseModel):
    id: str
    url: str
    dest_path: str
    filename: str
    total_bytes: int
    downloaded_bytes: int
    status: str
    error_message: str
    speed_bps: float
    progress: float
    accept_ranges: bool
    etag: str
    last_modified: str
    has_cookie: bool
    digest_sha256: str
    digest_verified: bool
    expected_checksum: str
    checksum_status: Literal["", "verified", "mismatch"]
    checksum_source: str
    speed_limit_bps: int
    referrer: str
    connections: int
    # [[start, done, end], ...] byte ranges of a split download, for the UI's
    # file-map ring; empty for single-stream downloads.
    segments: list[list[int]] = []
    created_at: float
    updated_at: float

    @classmethod
    def from_task(cls, task: DownloadTask) -> DownloadOut:
        return cls(
            id=task.id,
            url=task.url,
            dest_path=task.dest_path,
            filename=task.filename,
            total_bytes=task.total_bytes,
            downloaded_bytes=task.downloaded_bytes,
            status=task.status.value,
            error_message=task.error_message,
            speed_bps=task.speed_bps,
            progress=task.progress,
            # "Resumable": ranges supported AND a validator for If-Range.
            accept_ranges=task.accept_ranges and bool(task.etag or task.last_modified),
            etag=task.etag,
            last_modified=task.last_modified,
            # The cookie value itself is session-auth material -- the UI only
            # ever needs to know whether one was forwarded, not what it is.
            has_cookie=bool(task.cookie_header),
            digest_sha256=task.digest_sha256,
            digest_verified=task.digest_verified,
            expected_checksum=task.expected_checksum,
            checksum_status=task.checksum_status if task.checksum_status in ("verified", "mismatch") else "",
            checksum_source=task.checksum_source,
            speed_limit_bps=task.speed_limit_bps,
            referrer=task.referrer,
            connections=task.connections,
            segments=task.segments,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )


class HistoryEntry(BaseModel):
    time: float
    level: str
    message: str


class DiskInfo(BaseModel):
    path: str
    free_bytes: int
    total_bytes: int


class ChromeStatus(BaseModel):
    host_registered: bool
    last_ping_time: float | None = None
    last_profile_name: str | None = None
    all_known_profiles: dict[str, dict] = {}


class PickFolderResponse(BaseModel):
    path: str | None


class NewDownloadRequest(BaseModel):
    url: str
    # Only a filename -- the destination directory is always the server's
    # settings.download_dir, never client-supplied, so this can't be used
    # to write outside the download folder. See DownloadManager.add_download.
    filename: str
    # Optional MD5/SHA-1/SHA-256 to check the finished file against;
    # checksum.parse_expected validates it (a bad one is a 400).
    checksum: str = Field(default="", max_length=200)


class ProbeResponse(BaseModel):
    filename: str


class UpdateCheckResponse(BaseModel):
    current_version: str
    latest_version: str
    update_available: bool
    release_url: str


class Category(BaseModel):
    name: str = Field(max_length=200)
    extensions: list[str] = Field(max_length=200)
    folder: str = Field(max_length=4096)


class SettingsOut(BaseModel):
    download_dir: str
    max_concurrent_downloads: int
    notifications_enabled: bool
    minimize_to_tray: bool
    auto_folder_by_type: bool
    categories: list[Category]
    duplicate_policy: Literal["rename", "skip", "overwrite"]
    connections_per_download: int
    site_connection_limit: int
    speed_limit_enabled: bool
    speed_limit_bps: int
    theme: Literal["system", "light", "dark"]

    @classmethod
    def from_settings(cls, settings: Settings) -> SettingsOut:
        return cls(
            download_dir=settings.download_dir,
            max_concurrent_downloads=settings.max_concurrent_downloads,
            notifications_enabled=settings.notifications_enabled,
            minimize_to_tray=settings.minimize_to_tray,
            auto_folder_by_type=settings.auto_folder_by_type,
            categories=[Category(**c) for c in settings.categories],
            duplicate_policy=settings.duplicate_policy,
            connections_per_download=settings.connections_per_download,
            site_connection_limit=settings.site_connection_limit,
            speed_limit_enabled=settings.speed_limit_enabled,
            speed_limit_bps=settings.speed_limit_bps,
            theme=settings.theme if settings.theme in ("system", "light", "dark") else "system",
        )


class SettingsPatch(BaseModel):
    download_dir: str | None = None
    # Same bounds as the UI's stepper (SLOT_MIN/SLOT_MAX in index.html); 0 or
    # less would leave every download queued forever.
    max_concurrent_downloads: int | None = Field(default=None, ge=1, le=50)
    notifications_enabled: bool | None = None
    minimize_to_tray: bool | None = None
    auto_folder_by_type: bool | None = None
    categories: list[Category] | None = None
    duplicate_policy: Literal["rename", "skip", "overwrite"] | None = None
    connections_per_download: int | None = Field(default=None, ge=1, le=8)
    site_connection_limit: int | None = Field(default=None, ge=0, le=50)
    speed_limit_enabled: bool | None = None
    # Below about 16 KB/s a 64 KB chunk takes seconds to pass, long enough
    # for some servers to drop the connection as idle.
    speed_limit_bps: int | None = Field(default=None, ge=MIN_SPEED_LIMIT_BPS, le=MAX_SPEED_LIMIT_BPS)
    theme: Literal["system", "light", "dark"] | None = None

    @field_validator("categories")
    @classmethod
    def _valid_categories(cls, value: list[Category] | None) -> list[Category] | None:
        # Names unique, each extension in one category, folders safe; the
        # message reaches the Settings editor as the 400's detail.
        if value is None:
            return None
        return [Category(**c) for c in validate_categories([c.model_dump() for c in value])]

    @field_validator("download_dir")
    @classmethod
    def _existing_absolute_dir(cls, value: str | None) -> str | None:
        # The UI only ever sends what the folder picker returned; anything
        # else (relative, missing, a file) would surface later as every
        # download failing on write.
        if value is not None and not (os.path.isabs(value) and os.path.isdir(value)):
            raise ValueError("Download folder must be an existing folder")
        return value


class DownloadPatch(BaseModel):
    # This download's own cap; 0 removes it. Same bounds as the app-wide one.
    speed_limit_bps: int = Field(ge=0, le=MAX_SPEED_LIMIT_BPS)

    @field_validator("speed_limit_bps")
    @classmethod
    def _zero_or_at_least_minimum(cls, value: int) -> int:
        if 0 < value < MIN_SPEED_LIMIT_BPS:
            raise ValueError(f"Speed limit must be at least {MIN_SPEED_LIMIT_BPS // 1024} KB/s")
        return value


class RefreshUrlRequest(BaseModel):
    url: str


class DedupeCheckRequest(BaseModel):
    url: str


class DedupeCheckResponse(BaseModel):
    is_duplicate: bool
    existing_task_id: str | None = None
    existing_path: str | None = None
    existing_size: int = 0
    existing_digest: str | None = None
