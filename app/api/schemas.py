from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.engine.models import DownloadTask
from app.engine.settings import Settings


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


class ProbeResponse(BaseModel):
    filename: str


class SettingsOut(BaseModel):
    download_dir: str
    max_concurrent_downloads: int
    notifications_enabled: bool
    minimize_to_tray: bool
    auto_folder_by_type: bool
    duplicate_policy: Literal["rename", "skip", "overwrite"]
    connections_per_download: int
    site_connection_limit: int

    @classmethod
    def from_settings(cls, settings: Settings) -> SettingsOut:
        return cls(
            download_dir=settings.download_dir,
            max_concurrent_downloads=settings.max_concurrent_downloads,
            notifications_enabled=settings.notifications_enabled,
            minimize_to_tray=settings.minimize_to_tray,
            auto_folder_by_type=settings.auto_folder_by_type,
            duplicate_policy=settings.duplicate_policy,
            connections_per_download=settings.connections_per_download,
            site_connection_limit=settings.site_connection_limit,
        )


class SettingsPatch(BaseModel):
    download_dir: str | None = None
    # Same bounds as the UI's stepper (SLOT_MIN/SLOT_MAX in index.html); 0 or
    # less would leave every download queued forever.
    max_concurrent_downloads: int | None = Field(default=None, ge=1, le=50)
    notifications_enabled: bool | None = None
    minimize_to_tray: bool | None = None
    auto_folder_by_type: bool | None = None
    duplicate_policy: Literal["rename", "skip", "overwrite"] | None = None
    connections_per_download: int | None = Field(default=None, ge=1, le=8)
    site_connection_limit: int | None = Field(default=None, ge=0, le=50)

    @field_validator("download_dir")
    @classmethod
    def _existing_absolute_dir(cls, value: str | None) -> str | None:
        # The UI only ever sends what the folder picker returned; anything
        # else (relative, missing, a file) would surface later as every
        # download failing on write.
        if value is not None and not (os.path.isabs(value) and os.path.isdir(value)):
            raise ValueError("Download folder must be an existing folder")
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
