"""SQLite storage for sheet links (pipeline.db). Holds no credentials."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clipping_ai.pipeline.sheet import SheetRow, canonical_key

STATUSES = ("new", "sent", "skipped")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_source (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    sheet_url TEXT,
    last_sync_at TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS pipeline_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    title TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    job_id TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pipeline_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clip_flags (
    clip_id TEXT PRIMARY KEY,
    excluded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS analytics_snapshots (
    video_id TEXT PRIMARY KEY,
    clip_id TEXT,
    views INTEGER NOT NULL DEFAULT 0,
    minutes_watched REAL NOT NULL DEFAULT 0,
    avg_view_seconds REAL NOT NULL DEFAULT 0,
    likes INTEGER NOT NULL DEFAULT 0,
    comments INTEGER NOT NULL DEFAULT 0,
    subscribers_gained INTEGER NOT NULL DEFAULT 0,
    window_start TEXT,
    window_end TEXT,
    fetched_at TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "allow_public_sheet": "0",
    "publish_hour": "18",
    "publish_minute": "0",
    "per_day": "1",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PipelineStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._session() as conn:
            conn.executescript(_SCHEMA)
            conn.execute("INSERT OR IGNORE INTO pipeline_source (id) VALUES (1)")
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns that older pipeline.db files do not have yet. Nothing is dropped."""
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(pipeline_items)")}
        if "dedupe_key" not in columns:
            conn.execute("ALTER TABLE pipeline_items ADD COLUMN dedupe_key TEXT")
        if "reused_job" not in columns:
            conn.execute(
                "ALTER TABLE pipeline_items ADD COLUMN reused_job INTEGER NOT NULL DEFAULT 0"
            )
        for row in conn.execute(
            "SELECT id, url FROM pipeline_items WHERE dedupe_key IS NULL"
        ).fetchall():
            conn.execute(
                "UPDATE pipeline_items SET dedupe_key = ? WHERE id = ?",
                (canonical_key(row["url"]), row["id"]),
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pipeline_items_key ON pipeline_items (dedupe_key)"
        )

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def source(self) -> dict[str, Any]:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM pipeline_source WHERE id = 1").fetchone()
        return {
            "sheet_url": row["sheet_url"],
            "last_sync_at": row["last_sync_at"],
            "last_error": row["last_error"],
        }

    def set_sheet_url(self, url: str | None) -> None:
        with self._session() as conn:
            conn.execute(
                "UPDATE pipeline_source SET sheet_url = ?, last_error = NULL WHERE id = 1", (url,)
            )

    def record_sync(self, error: str | None) -> None:
        with self._session() as conn:
            if error is None:
                conn.execute(
                    "UPDATE pipeline_source SET last_sync_at = ?, last_error = NULL WHERE id = 1",
                    (_now(),),
                )
            else:
                conn.execute("UPDATE pipeline_source SET last_error = ? WHERE id = 1", (error,))

    def add_rows(self, rows: list[SheetRow]) -> int:
        """Insert links not seen before. Existing items keep their status. Returns new count."""
        return self.add_rows_detailed(rows)[0]

    def add_rows_detailed(self, rows: list[SheetRow]) -> tuple[int, int]:
        """Returns (added, duplicates). A duplicate is a link already in the queue, either the
        same text or another spelling of the same video (youtu.be vs youtube.com/watch)."""
        added = 0
        duplicates = 0
        now = _now()
        with self._session() as conn:
            for row in rows:
                key = canonical_key(row.url)
                known = conn.execute(
                    "SELECT 1 FROM pipeline_items WHERE url = ? OR dedupe_key = ? LIMIT 1",
                    (row.url, key),
                ).fetchone()
                if known:
                    duplicates += 1
                    continue
                conn.execute(
                    "INSERT INTO pipeline_items "
                    "(url, title, notes, dedupe_key, first_seen_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (row.url, row.title, row.notes, key, now, now),
                )
                added += 1
        return added, duplicates

    def get_setting(self, key: str) -> str:
        with self._session() as conn:
            row = conn.execute(
                "SELECT value FROM pipeline_settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else DEFAULT_SETTINGS[key]

    def set_setting(self, key: str, value: str) -> None:
        if key not in DEFAULT_SETTINGS:
            raise KeyError(key)
        with self._session() as conn:
            conn.execute(
                "INSERT INTO pipeline_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def excluded_clips(self) -> set[str]:
        with self._session() as conn:
            rows = conn.execute("SELECT clip_id FROM clip_flags WHERE excluded = 1").fetchall()
        return {r["clip_id"] for r in rows}

    def set_excluded(self, clip_id: str, excluded: bool) -> None:
        with self._session() as conn:
            conn.execute(
                "INSERT INTO clip_flags (clip_id, excluded) VALUES (?, ?) "
                "ON CONFLICT(clip_id) DO UPDATE SET excluded = excluded.excluded",
                (clip_id, 1 if excluded else 0),
            )

    def save_analytics(self, rows: list[dict[str, Any]]) -> None:
        now = _now()
        with self._session() as conn:
            for r in rows:
                conn.execute(
                    "INSERT INTO analytics_snapshots (video_id, clip_id, views, minutes_watched, "
                    "avg_view_seconds, likes, comments, subscribers_gained, window_start, "
                    "window_end, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(video_id) DO UPDATE SET clip_id = excluded.clip_id, "
                    "views = excluded.views, minutes_watched = excluded.minutes_watched, "
                    "avg_view_seconds = excluded.avg_view_seconds, likes = excluded.likes, "
                    "comments = excluded.comments, "
                    "subscribers_gained = excluded.subscribers_gained, "
                    "window_start = excluded.window_start, window_end = excluded.window_end, "
                    "fetched_at = excluded.fetched_at",
                    (
                        r["video_id"],
                        r.get("clip_id"),
                        r["views"],
                        r["minutes_watched"],
                        r["avg_view_seconds"],
                        r["likes"],
                        r["comments"],
                        r["subscribers_gained"],
                        r.get("window_start"),
                        r.get("window_end"),
                        now,
                    ),
                )

    def analytics(self) -> list[dict[str, Any]]:
        with self._session() as conn:
            rows = conn.execute("SELECT * FROM analytics_snapshots ORDER BY views DESC").fetchall()
        return [dict(r) for r in rows]

    def list_items(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._session() as conn:
            rows = conn.execute(
                "SELECT * FROM pipeline_items ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get(self, item_id: int) -> dict[str, Any] | None:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM pipeline_items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    def set_status(
        self, item_id: int, status: str, job_id: str | None = None, *, reused: bool | None = None
    ) -> None:
        with self._session() as conn:
            conn.execute(
                "UPDATE pipeline_items SET status = ?, job_id = COALESCE(?, job_id), "
                "reused_job = COALESCE(?, reused_job), updated_at = ? WHERE id = ?",
                (status, job_id, None if reused is None else int(reused), _now(), item_id),
            )

    def counts(self) -> dict[str, int]:
        with self._session() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM pipeline_items GROUP BY status"
            ).fetchall()
        counts = {status: 0 for status in STATUSES}
        counts.update({r["status"]: r["n"] for r in rows})
        return counts
