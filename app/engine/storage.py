from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

from app.engine.models import DownloadTask

SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    dest_path TEXT NOT NULL,
    filename TEXT,
    total_bytes INTEGER DEFAULT 0,
    downloaded_bytes INTEGER DEFAULT 0,
    status TEXT NOT NULL,
    error_message TEXT DEFAULT '',
    etag TEXT DEFAULT '',
    last_modified TEXT DEFAULT '',
    cookie_header TEXT DEFAULT '',
    digest_sha256 TEXT DEFAULT '',
    digest_verified INTEGER DEFAULT 0,
    created_at REAL,
    updated_at REAL
);
"""


class Storage:
    """SQLite-backed persistence so downloads survive an app restart.

    A single connection is reused across threads (download workers run in
    their own QThread), so every call is guarded by a lock rather than
    opening a fresh connection per call.
    """

    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # timeout=10: this process's own writes are already serialized by
        # self._lock, but the native host (a *separate* process, launched
        # fresh on every Chrome hand-off) opens its own connection to the
        # same file -- without a busy timeout, a write landing mid-commit
        # from the other process raises "database is locked" immediately
        # instead of waiting briefly for the lock to clear.
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
        with self._lock:
            self._conn.execute(SCHEMA)
            self._migrate()
            # WAL lets readers (the 2s external-poll in manager.py) proceed
            # without blocking on the writer, further reducing the chance
            # either process ever hits the busy timeout above.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.commit()
        # cookie_header holds real browser session cookies -- keep the file
        # readable only by its owner on a shared machine. Best-effort: some
        # filesystems (e.g. certain network mounts) don't support chmod.
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass

    def _migrate(self) -> None:
        existing_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(downloads)")}
        for column, ddl_type in (("etag", "TEXT DEFAULT ''"), ("last_modified", "TEXT DEFAULT ''"),
                                  ("cookie_header", "TEXT DEFAULT ''"),
                                  ("digest_sha256", "TEXT DEFAULT ''"),
                                  ("digest_verified", "INTEGER DEFAULT 0"),
                                  ("referrer", "TEXT DEFAULT ''")):
            if column not in existing_columns:
                self._conn.execute(f"ALTER TABLE downloads ADD COLUMN {column} {ddl_type}")
        # Two processes (this app and the Chrome native host, launched fresh
        # per hand-off) can each compute a "reserved dest_path" set and pick
        # the same free-looking filename in the narrow window between that
        # read and their eventual INSERT. A plain in-memory check can't see
        # across processes, but the database itself can: this constraint
        # turns that race into an IntegrityError the inserting side catches
        # and retries against (see DownloadManager.add_download and the
        # native host), instead of silently letting two tasks share a path.
        try:
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_downloads_dest_path ON downloads(dest_path)"
            )
        except sqlite3.IntegrityError:
            # An existing DB with duplicate dest_paths already in it (from
            # before this constraint existed) would otherwise fail to open
            # at all. Skip the index there rather than break startup --
            # this installation just loses the extra race protection.
            pass

    def save(self, task: DownloadTask) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO downloads
                    (id, url, dest_path, filename, total_bytes, downloaded_bytes,
                     status, error_message, etag, last_modified, cookie_header,
                     digest_sha256, digest_verified, referrer, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    url=excluded.url,
                    dest_path=excluded.dest_path,
                    filename=excluded.filename,
                    total_bytes=excluded.total_bytes,
                    downloaded_bytes=excluded.downloaded_bytes,
                    status=excluded.status,
                    error_message=excluded.error_message,
                    etag=excluded.etag,
                    last_modified=excluded.last_modified,
                    cookie_header=excluded.cookie_header,
                    digest_sha256=excluded.digest_sha256,
                    digest_verified=excluded.digest_verified,
                    referrer=excluded.referrer,
                    updated_at=excluded.updated_at
                """,
                task.to_row(),
            )
            self._conn.commit()

    def delete(self, task_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM downloads WHERE id = ?", (task_id,))
            self._conn.commit()

    def load_all(self) -> list[DownloadTask]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, url, dest_path, filename, total_bytes, downloaded_bytes, "
                "status, error_message, etag, last_modified, cookie_header, "
                "digest_sha256, digest_verified, referrer, created_at, updated_at "
                "FROM downloads ORDER BY created_at DESC"
            ).fetchall()
        return [DownloadTask.from_row(row) for row in rows]
