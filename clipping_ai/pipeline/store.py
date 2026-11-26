"""SQLite storage for sheet links (pipeline.db). Holds no credentials."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clipping_ai.pipeline.sheet import SheetRow

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
"""


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
        added = 0
        now = _now()
        with self._session() as conn:
            for row in rows:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO pipeline_items "
                    "(url, title, notes, first_seen_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (row.url, row.title, row.notes, now, now),
                )
                added += cursor.rowcount
        return added

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

    def set_status(self, item_id: int, status: str, job_id: str | None = None) -> None:
        with self._session() as conn:
            conn.execute(
                "UPDATE pipeline_items SET status = ?, job_id = COALESCE(?, job_id), "
                "updated_at = ? WHERE id = ?",
                (status, job_id, _now(), item_id),
            )

    def counts(self) -> dict[str, int]:
        with self._session() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM pipeline_items GROUP BY status"
            ).fetchall()
        counts = {status: 0 for status in STATUSES}
        counts.update({r["status"]: r["n"] for r in rows})
        return counts
