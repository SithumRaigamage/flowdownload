"""Storage schema migrations: older databases must keep opening."""
from __future__ import annotations

import sqlite3

from app.engine.models import DownloadTask
from app.engine.storage import Storage

# The downloads table as it was before the referrer column existed.
OLD_SCHEMA = """
CREATE TABLE downloads (
    id TEXT PRIMARY KEY, url TEXT NOT NULL, dest_path TEXT NOT NULL, filename TEXT,
    total_bytes INTEGER DEFAULT 0, downloaded_bytes INTEGER DEFAULT 0, status TEXT NOT NULL,
    error_message TEXT DEFAULT '', etag TEXT DEFAULT '', last_modified TEXT DEFAULT '',
    cookie_header TEXT DEFAULT '', digest_sha256 TEXT DEFAULT '', digest_verified INTEGER DEFAULT 0,
    created_at REAL, updated_at REAL
);
"""


def test_old_database_gains_referrer_column(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(OLD_SCHEMA)
    conn.execute("INSERT INTO downloads (id, url, dest_path, filename, status, created_at, updated_at) "
                 "VALUES ('a', 'https://e.test/f', '/tmp/f', 'f', 'paused', 1, 1)")
    conn.commit()
    conn.close()

    storage = Storage(str(db))
    (task,) = storage.load_all()
    assert task.referrer == ""
    task.referrer = "https://page.test/"
    storage.save(task)
    assert Storage(str(db)).load_all()[0].referrer == "https://page.test/"


def test_load_new_returns_only_unknown_rows(tmp_path):
    storage = Storage(str(tmp_path / "t.db"))
    known = DownloadTask(url="http://x/a", dest_path="/d/a")
    fresh = DownloadTask(url="http://x/b", dest_path="/d/b")
    storage.save(known)
    storage.save(fresh)
    assert [t.id for t in storage.load_new({known.id})] == [fresh.id]
    assert storage.load_new({known.id, fresh.id}) == []
    assert storage.dest_paths() == {"/d/a", "/d/b"}
