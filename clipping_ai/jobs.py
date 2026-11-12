"""Persistent cache for transcription chunks so interrupted jobs can resume."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from clipping_ai.schemas import TranscriptResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcription_chunks (
    cache_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    offset REAL NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunk_source
    ON transcription_chunks(source_hash, provider, model);
"""


class TranscriptionCache:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._session() as conn:
            conn.executescript(_SCHEMA)

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

    def get(self, cache_key: str) -> TranscriptResult | None:
        with self._session() as conn:
            row = conn.execute(
                "SELECT result_json FROM transcription_chunks WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        if row is None:
            return None
        try:
            return TranscriptResult.model_validate_json(row["result_json"])
        except Exception:
            return None

    def put(
        self,
        cache_key: str,
        *,
        provider: str,
        model: str,
        source_hash: str,
        chunk_index: int,
        offset: float,
        result: TranscriptResult,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        payload = result.model_dump_json()
        with self._session() as conn:
            conn.execute(
                """
                INSERT INTO transcription_chunks (
                    cache_key, provider, model, source_hash, chunk_index, offset,
                    result_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    result_json = excluded.result_json,
                    updated_at = excluded.updated_at
                """,
                (cache_key, provider, model, source_hash, chunk_index, offset, payload, now, now),
            )

    def clear_source(self, source_hash: str) -> int:
        with self._session() as conn:
            cursor = conn.execute(
                "DELETE FROM transcription_chunks WHERE source_hash = ?", (source_hash,)
            )
            return int(cursor.rowcount)

    def count(self) -> int:
        with self._session() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM transcription_chunks").fetchone()
            return int(row["n"]) if row else 0
