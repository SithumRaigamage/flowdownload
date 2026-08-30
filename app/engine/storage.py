from __future__ import annotations

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
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        with self._lock:
            self._conn.execute(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        existing_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(downloads)")}
        for column, ddl_type in (("etag", "TEXT DEFAULT ''"), ("last_modified", "TEXT DEFAULT ''"),
                                  ("cookie_header", "TEXT DEFAULT ''")):
            if column not in existing_columns:
                self._conn.execute(f"ALTER TABLE downloads ADD COLUMN {column} {ddl_type}")

    def save(self, task: DownloadTask) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO downloads
                    (id, url, dest_path, filename, total_bytes, downloaded_bytes,
                     status, error_message, etag, last_modified, cookie_header, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                "status, error_message, etag, last_modified, cookie_header, created_at, updated_at "
                "FROM downloads ORDER BY created_at DESC"
            ).fetchall()
        return [DownloadTask.from_row(row) for row in rows]
