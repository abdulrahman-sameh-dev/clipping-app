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
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    publish_hour INTEGER NOT NULL DEFAULT 18,
    publish_minute INTEGER NOT NULL DEFAULT 0,
    per_day INTEGER NOT NULL DEFAULT 1,
    strong_ratio REAL NOT NULL DEFAULT 1.5,
    weak_ratio REAL NOT NULL DEFAULT 0.5,
    sheet_url TEXT,
    last_sync_at TEXT,
    last_error TEXT,
    schedule_fetched_at TEXT,
    schedule_error TEXT,
    youtube_channel_id TEXT,
    youtube_channel_title TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS remote_schedule (
    channel_id TEXT NOT NULL,
    video_id TEXT NOT NULL,
    title TEXT,
    publish_at TEXT NOT NULL,
    PRIMARY KEY (channel_id, video_id)
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


DEFAULT_CHANNEL = "default"
CATEGORIES = ("general", "gaming", "religious", "educational", "entertainment", "news", "other")
_CHANNEL_FIELDS = (
    "name",
    "category",
    "publish_hour",
    "publish_minute",
    "per_day",
    "strong_ratio",
    "weak_ratio",
)


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
            self._ensure_default_channel(conn)

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
        if "channel_id" not in columns:
            conn.execute(
                "ALTER TABLE pipeline_items ADD COLUMN channel_id TEXT NOT NULL "
                f"DEFAULT '{DEFAULT_CHANNEL}'"
            )
        flags = {r["name"] for r in conn.execute("PRAGMA table_info(clip_flags)")}
        if "channel_id" not in flags:
            conn.execute("ALTER TABLE clip_flags ADD COLUMN channel_id TEXT")

    @staticmethod
    def _ensure_default_channel(conn: sqlite3.Connection) -> None:
        """The first channel is the one that existed before channels: it takes over the sheet
        link, sync state and daily slot that were saved globally."""
        if conn.execute("SELECT 1 FROM channels WHERE id = ?", (DEFAULT_CHANNEL,)).fetchone():
            return
        source = conn.execute("SELECT * FROM pipeline_source WHERE id = 1").fetchone()
        saved = {
            r["key"]: r["value"]
            for r in conn.execute("SELECT key, value FROM pipeline_settings").fetchall()
        }
        conn.execute(
            "INSERT INTO channels (id, name, category, publish_hour, publish_minute, per_day, "
            "sheet_url, last_sync_at, last_error, created_at) VALUES (?, ?, 'general', ?, ?, ?, "
            "?, ?, ?, ?)",
            (
                DEFAULT_CHANNEL,
                "Main channel",
                int(saved.get("publish_hour", DEFAULT_SETTINGS["publish_hour"])),
                int(saved.get("publish_minute", DEFAULT_SETTINGS["publish_minute"])),
                int(saved.get("per_day", DEFAULT_SETTINGS["per_day"])),
                source["sheet_url"],
                source["last_sync_at"],
                source["last_error"],
                _now(),
            ),
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

    # --- channels -------------------------------------------------------------------------

    def list_channels(self) -> list[dict[str, Any]]:
        with self._session() as conn:
            rows = conn.execute("SELECT * FROM channels ORDER BY created_at, id").fetchall()
        return [dict(r) for r in rows]

    def get_channel(self, channel_id: str) -> dict[str, Any] | None:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
        return dict(row) if row else None

    def create_channel(self, channel_id: str, values: dict[str, Any]) -> dict[str, Any]:
        with self._session() as conn:
            conn.execute(
                "INSERT INTO channels (id, name, category, publish_hour, publish_minute, "
                "per_day, strong_ratio, weak_ratio, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    channel_id,
                    values["name"],
                    values.get("category", "general"),
                    values.get("publish_hour", 18),
                    values.get("publish_minute", 0),
                    values.get("per_day", 1),
                    values.get("strong_ratio", 1.5),
                    values.get("weak_ratio", 0.5),
                    _now(),
                ),
            )
        return self.get_channel(channel_id) or {}

    def update_channel(self, channel_id: str, values: dict[str, Any]) -> None:
        fields = [k for k in values if k in _CHANNEL_FIELDS]
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._session() as conn:
            conn.execute(
                f"UPDATE channels SET {sets} WHERE id = ?",  # noqa: S608 - names are whitelisted
                (*[values[k] for k in fields], channel_id),
            )

    def delete_channel(self, channel_id: str) -> None:
        with self._session() as conn:
            conn.execute("DELETE FROM remote_schedule WHERE channel_id = ?", (channel_id,))
            conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))

    def channel_item_count(self, channel_id: str) -> int:
        with self._session() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM pipeline_items WHERE channel_id = ?", (channel_id,)
            ).fetchone()
        return int(row["n"])

    def source(self, channel_id: str = DEFAULT_CHANNEL) -> dict[str, Any]:
        channel = self.get_channel(channel_id) or {}
        return {
            "sheet_url": channel.get("sheet_url"),
            "last_sync_at": channel.get("last_sync_at"),
            "last_error": channel.get("last_error"),
        }

    def set_sheet_url(self, url: str | None, channel_id: str = DEFAULT_CHANNEL) -> None:
        with self._session() as conn:
            conn.execute(
                "UPDATE channels SET sheet_url = ?, last_error = NULL WHERE id = ?",
                (url, channel_id),
            )

    def record_sync(self, error: str | None, channel_id: str = DEFAULT_CHANNEL) -> None:
        with self._session() as conn:
            if error is None:
                conn.execute(
                    "UPDATE channels SET last_sync_at = ?, last_error = NULL WHERE id = ?",
                    (_now(), channel_id),
                )
            else:
                conn.execute("UPDATE channels SET last_error = ? WHERE id = ?", (error, channel_id))

    # --- the channel's own schedule, as read from YouTube ---------------------------------------

    def save_remote_schedule(
        self, channel_id: str, videos: list[dict[str, Any]], youtube_channel: dict[str, Any]
    ) -> None:
        with self._session() as conn:
            conn.execute("DELETE FROM remote_schedule WHERE channel_id = ?", (channel_id,))
            for v in videos:
                conn.execute(
                    "INSERT OR REPLACE INTO remote_schedule (channel_id, video_id, title, "
                    "publish_at) VALUES (?, ?, ?, ?)",
                    (channel_id, v["video_id"], v.get("title"), v["publish_at"]),
                )
            conn.execute(
                "UPDATE channels SET schedule_fetched_at = ?, schedule_error = NULL, "
                "youtube_channel_id = ?, youtube_channel_title = ? WHERE id = ?",
                (
                    _now(),
                    youtube_channel.get("id") or None,
                    youtube_channel.get("title") or None,
                    channel_id,
                ),
            )

    def record_schedule_error(self, channel_id: str, message: str) -> None:
        with self._session() as conn:
            conn.execute(
                "UPDATE channels SET schedule_error = ? WHERE id = ?", (message, channel_id)
            )

    def clear_remote_schedule(self, channel_id: str) -> None:
        with self._session() as conn:
            conn.execute("DELETE FROM remote_schedule WHERE channel_id = ?", (channel_id,))
            conn.execute(
                "UPDATE channels SET schedule_fetched_at = NULL, schedule_error = NULL "
                "WHERE id = ?",
                (channel_id,),
            )

    def remote_schedule(self, channel_id: str) -> list[dict[str, Any]]:
        with self._session() as conn:
            rows = conn.execute(
                "SELECT video_id, title, publish_at FROM remote_schedule WHERE channel_id = ? "
                "ORDER BY publish_at",
                (channel_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def job_channels(self) -> dict[str, str]:
        """job id -> channel of the queue item that created it."""
        with self._session() as conn:
            rows = conn.execute(
                "SELECT job_id, channel_id FROM pipeline_items WHERE job_id IS NOT NULL"
            ).fetchall()
        return {r["job_id"]: r["channel_id"] for r in rows}

    def clip_channel_overrides(self) -> dict[str, str]:
        with self._session() as conn:
            rows = conn.execute(
                "SELECT clip_id, channel_id FROM clip_flags WHERE channel_id IS NOT NULL"
            ).fetchall()
        return {r["clip_id"]: r["channel_id"] for r in rows}

    def set_clip_channel(self, clip_id: str, channel_id: str | None) -> None:
        with self._session() as conn:
            conn.execute(
                "INSERT INTO clip_flags (clip_id, excluded, channel_id) VALUES (?, 0, ?) "
                "ON CONFLICT(clip_id) DO UPDATE SET channel_id = excluded.channel_id",
                (clip_id, channel_id),
            )

    def add_rows(self, rows: list[SheetRow], channel_id: str = DEFAULT_CHANNEL) -> int:
        """Insert links not seen before. Existing items keep their status. Returns new count."""
        return self.add_rows_detailed(rows, channel_id)[0]

    def add_rows_detailed(
        self, rows: list[SheetRow], channel_id: str = DEFAULT_CHANNEL
    ) -> tuple[int, int]:
        """Returns (added, duplicates). A duplicate is a link already in the queue, either the
        same text or another spelling of the same video (youtu.be vs youtube.com/watch). A link belongs to one channel only, so a link that
        is already queued for another channel counts as a duplicate too."""
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
                    "(url, title, notes, dedupe_key, channel_id, first_seen_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (row.url, row.title, row.notes, key, channel_id, now, now),
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

    def list_items(self, limit: int = 500, channel_id: str | None = None) -> list[dict[str, Any]]:
        where, args = ("WHERE channel_id = ? ", (channel_id,)) if channel_id else ("", ())
        with self._session() as conn:
            rows = conn.execute(
                f"SELECT * FROM pipeline_items {where}ORDER BY id DESC LIMIT ?",  # noqa: S608
                (*args, limit),
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

    def counts(self, channel_id: str | None = None) -> dict[str, int]:
        where, args = ("WHERE channel_id = ? ", (channel_id,)) if channel_id else ("", ())
        with self._session() as conn:
            rows = conn.execute(
                f"SELECT status, COUNT(*) AS n FROM pipeline_items {where}GROUP BY status",  # noqa: S608
                args,
            ).fetchall()
        counts = {status: 0 for status in STATUSES}
        counts.update({r["status"]: r["n"] for r in rows})
        return counts
