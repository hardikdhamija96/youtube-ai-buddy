import os
import sqlite3

DB_PATH = os.getenv("DB_PATH", "/data/app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id      TEXT UNIQUE,            -- YouTube ID; re-analysing upserts this row
    url           TEXT,
    title         TEXT,
    channel       TEXT,
    thumbnail_url TEXT,
    duration_sec  INTEGER,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | processing | done | error
    error_message TEXT,
    level         TEXT,
    verdict       TEXT,
    tldr          TEXT,
    chapters      JSON,                   -- [{title, content}, ...] in order
    notes         JSON,
    quiz          JSON,                   -- reveal-only, no scoring in v1
    created_at    DATETIME NOT NULL DEFAULT (datetime('now')),
    updated_at    DATETIME NOT NULL DEFAULT (datetime('now'))
);
"""


def get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(videos)")]
        # Scaffold-stage migration: an older table without video_id only held
        # stub rows, so drop and recreate it. Remove once real data exists.
        if cols and "video_id" not in cols:
            conn.execute("DROP TABLE videos")
        conn.executescript(SCHEMA)
