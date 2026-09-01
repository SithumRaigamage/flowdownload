from __future__ import annotations

from pydantic import BaseModel

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
    created_at: float
    updated_at: float

    @classmethod
    def from_task(cls, task: DownloadTask) -> "DownloadOut":
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
            accept_ranges=bool(task.etag or task.last_modified),
            etag=task.etag,
            last_modified=task.last_modified,
            # The cookie value itself is session-auth material -- the UI only
            # ever needs to know whether one was forwarded, not what it is.
            has_cookie=bool(task.cookie_header),
            digest_sha256=task.digest_sha256,
            digest_verified=task.digest_verified,
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
    dest_path: str


class ProbeResponse(BaseModel):
    filename: str


class SettingsOut(BaseModel):
    download_dir: str
    max_concurrent_downloads: int
    notifications_enabled: bool
    minimize_to_tray: bool
    auto_folder_by_type: bool

    @classmethod
    def from_settings(cls, settings: Settings) -> "SettingsOut":
        return cls(
            download_dir=settings.download_dir,
            max_concurrent_downloads=settings.max_concurrent_downloads,
            notifications_enabled=settings.notifications_enabled,
            minimize_to_tray=settings.minimize_to_tray,
            auto_folder_by_type=settings.auto_folder_by_type,
        )


class SettingsPatch(BaseModel):
    download_dir: str | None = None
    max_concurrent_downloads: int | None = None
    notifications_enabled: bool | None = None
    minimize_to_tray: bool | None = None
    auto_folder_by_type: bool | None = None


class DedupeCheckRequest(BaseModel):
    url: str


class DedupeCheckResponse(BaseModel):
    is_duplicate: bool
    existing_task_id: str | None = None
    existing_path: str | None = None
    existing_size: int = 0
    existing_digest: str | None = None
