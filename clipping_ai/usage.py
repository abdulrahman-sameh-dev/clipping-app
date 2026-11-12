"""Lightweight local usage tracking backed by SQLite.

This records what *we* observed locally. It is not a provider quota oracle:
providers do not expose exact remaining free-tier quota through a common API.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    task TEXT,
    request_kind TEXT NOT NULL DEFAULT 'text',
    success INTEGER NOT NULL,
    error_category TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    audio_seconds REAL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    cost REAL,
    quota_error INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts);
CREATE INDEX IF NOT EXISTS idx_usage_provider_model ON usage_events(provider, model);
"""


def _utcnow() -> datetime:
    return datetime.now(UTC)


class UsageTracker:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self._session() as conn:
            conn.executescript(_SCHEMA)

    def record(
        self,
        *,
        provider: str,
        model: str | None,
        task: str | None,
        request_kind: str = "text",
        success: bool,
        error_category: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        audio_seconds: float | None = None,
        retry_count: int = 0,
        cost: float | None = None,
        quota_error: bool = False,
        latency_ms: int | None = None,
        request_id: str | None = None,
    ) -> str:
        request_id = request_id or uuid.uuid4().hex
        with self._session() as conn:
            conn.execute(
                """
                INSERT INTO usage_events (
                    request_id, ts, provider, model, task, request_kind, success,
                    error_category, input_tokens, output_tokens, audio_seconds,
                    retry_count, cost, quota_error, latency_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    _utcnow().isoformat(),
                    provider,
                    model,
                    task,
                    request_kind,
                    1 if success else 0,
                    error_category,
                    input_tokens,
                    output_tokens,
                    audio_seconds,
                    retry_count,
                    cost,
                    1 if quota_error else 0,
                    latency_ms,
                ),
            )
        return request_id

    def count_today(self, provider: str, model: str | None = None) -> int:
        start = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        query = "SELECT COUNT(*) AS n FROM usage_events WHERE ts >= ? AND provider = ?"
        params: list[Any] = [start.isoformat(), provider]
        if model:
            query += " AND model = ?"
            params.append(model)
        with self._session() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row["n"]) if row else 0

    def summary(self, days: int = 7) -> list[dict[str, Any]]:
        since = (_utcnow() - timedelta(days=max(days, 1))).isoformat()
        with self._session() as conn:
            rows = conn.execute(
                """
                SELECT provider, model, task,
                       COUNT(*) AS requests,
                       SUM(success) AS successes,
                       SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failures,
                       SUM(COALESCE(input_tokens, 0)) AS input_tokens,
                       SUM(COALESCE(output_tokens, 0)) AS output_tokens,
                       SUM(COALESCE(audio_seconds, 0)) AS audio_seconds,
                       SUM(COALESCE(cost, 0)) AS reported_cost,
                       SUM(quota_error) AS quota_errors,
                       MAX(ts) AS last_request
                FROM usage_events
                WHERE ts >= ?
                GROUP BY provider, model, task
                ORDER BY requests DESC
                """,
                (since,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._session() as conn:
            rows = conn.execute(
                "SELECT * FROM usage_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def daily_counts(self, days: int = 7) -> list[dict[str, Any]]:
        since = (_utcnow() - timedelta(days=max(days, 1))).date().isoformat()
        with self._session() as conn:
            rows = conn.execute(
                """
                SELECT substr(ts, 1, 10) AS day, provider,
                       COUNT(*) AS requests,
                       SUM(COALESCE(audio_seconds, 0)) AS audio_seconds,
                       SUM(quota_error) AS quota_errors
                FROM usage_events
                WHERE substr(ts, 1, 10) >= ?
                GROUP BY day, provider
                ORDER BY day DESC, provider
                """,
                (since,),
            ).fetchall()
        return [dict(row) for row in rows]

    def totals(self) -> dict[str, Any]:
        with self._session() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS requests,
                       SUM(success) AS successes,
                       SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failures,
                       SUM(COALESCE(input_tokens, 0)) AS input_tokens,
                       SUM(COALESCE(output_tokens, 0)) AS output_tokens,
                       SUM(COALESCE(audio_seconds, 0)) AS audio_seconds,
                       SUM(COALESCE(cost, 0)) AS reported_cost,
                       SUM(quota_error) AS quota_errors
                FROM usage_events
                """
            ).fetchone()
        return dict(row) if row else {}
