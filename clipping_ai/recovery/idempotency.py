"""Persistent idempotency keys and stable request fingerprints."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clipping_ai.recovery.schema import migrate_jobs_db


def stable_fingerprint(**values: Any) -> str:
    """Return a deterministic, non-secret fingerprint for request fields."""
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def reserve_idempotency(
    conn: sqlite3.Connection,
    *,
    scope: str,
    key: str,
    job_type: str,
    job_id: str,
) -> str | None:
    """Reserve a key inside an existing ``BEGIN IMMEDIATE`` transaction.

    Returns the existing job ID when the key is already present, or ``None``
    after inserting a new reservation.
    """
    cursor = conn.execute(
        """
        INSERT INTO idempotency_keys (scope, key, job_type, job_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(scope, key) DO NOTHING
        """,
        (scope, key, job_type, job_id, datetime.now(UTC).isoformat()),
    )
    if cursor.rowcount == 1:
        return None
    row = conn.execute(
        "SELECT job_id FROM idempotency_keys WHERE scope = ? AND key = ?",
        (scope, key),
    ).fetchone()
    return str(row["job_id"]) if row else None


def release_idempotency(conn: sqlite3.Connection, *, scope: str, key: str) -> None:
    conn.execute(
        "DELETE FROM idempotency_keys WHERE scope = ? AND key = ?", (scope, key)
    )


@dataclass(frozen=True)
class IdempotencyRecord:
    scope: str
    key: str
    job_type: str
    job_id: str
    created_at: str


@dataclass
class IdempotencyStore:
    db_path: Path | str
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.db_path = Path(self.db_path)
        migrate_jobs_db(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
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

    def get(self, scope: str, key: str) -> IdempotencyRecord | None:
        with self._session() as conn:
            row = conn.execute(
                "SELECT * FROM idempotency_keys WHERE scope = ? AND key = ?",
                (scope, key),
            ).fetchone()
        if row is None:
            return None
        return IdempotencyRecord(
            scope=row["scope"],
            key=row["key"],
            job_type=row["job_type"],
            job_id=row["job_id"],
            created_at=row["created_at"],
        )

    def reserve(
        self, *, scope: str, key: str, job_type: str, job_id: str
    ) -> tuple[IdempotencyRecord | None, bool]:
        """Return ``(existing, created)`` atomically."""
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing_id = reserve_idempotency(
                conn, scope=scope, key=key, job_type=job_type, job_id=job_id
            )
        if existing_id is None:
            return None, True
        existing = self.get(scope, key)
        if existing is None:  # pragma: no cover - reserved row vanished
            return None, True
        return existing, False

    def release(self, scope: str, key: str) -> None:
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            release_idempotency(conn, scope=scope, key=key)
