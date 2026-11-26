"""Persistent provider cooldown and health metadata.

Only non-sensitive routing state is stored here: provider/model identifiers,
categories, and timestamps.  API keys, headers, prompts, and provider
responses never enter this table.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from clipping_ai.recovery.schema import migrate_jobs_db


@dataclass(frozen=True)
class ProviderRuntime:
    provider: str
    model: str
    cooldown_until: float | None = None
    cooldown_reason: str | None = None
    cooldown_category: str | None = None
    quota_reset_at: float | None = None
    retry_after_at: float | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None
    last_failure_category: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def cooldown_remaining(self, *, now: float | None = None) -> float:
        current = time.time() if now is None else now
        if self.cooldown_until is None:
            return 0.0
        return max(0.0, self.cooldown_until - current)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class ProviderRuntimeStore:
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

    def _row_to_record(self, row: sqlite3.Row) -> ProviderRuntime:
        return ProviderRuntime(
            provider=row["provider"],
            model=row["model"],
            cooldown_until=float(row["cooldown_until"])
            if row["cooldown_until"] is not None
            else None,
            cooldown_reason=row["cooldown_reason"],
            cooldown_category=row["cooldown_category"],
            quota_reset_at=float(row["quota_reset_at"])
            if row["quota_reset_at"] is not None
            else None,
            retry_after_at=float(row["retry_after_at"])
            if row["retry_after_at"] is not None
            else None,
            last_success_at=row["last_success_at"],
            last_failure_at=row["last_failure_at"],
            last_failure_category=row["last_failure_category"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(self, provider: str, model: str = "") -> ProviderRuntime | None:
        with self._session() as conn:
            row = conn.execute(
                "SELECT * FROM provider_runtime WHERE provider = ? AND model = ?",
                (provider, model),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def cooldown_remaining(
        self, provider: str, model: str = "", *, now: float | None = None
    ) -> float:
        record = self.get(provider, model)
        return record.cooldown_remaining(now=now) if record else 0.0

    def set_cooldown(
        self,
        provider: str,
        *,
        model: str = "",
        until: float,
        reason: str,
        category: str,
        quota_reset_at: float | None = None,
    ) -> None:
        now = _now_iso()
        with self._session() as conn:
            conn.execute(
                """
                INSERT INTO provider_runtime (
                    provider, model, cooldown_until, cooldown_reason,
                    cooldown_category, quota_reset_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, model) DO UPDATE SET
                    cooldown_until = MAX(
                        COALESCE(provider_runtime.cooldown_until, 0),
                        excluded.cooldown_until
                    ),
                    cooldown_reason = excluded.cooldown_reason,
                    cooldown_category = excluded.cooldown_category,
                    quota_reset_at = COALESCE(
                        excluded.quota_reset_at, provider_runtime.quota_reset_at
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    provider,
                    model,
                    until,
                    reason,
                    category,
                    quota_reset_at,
                    now,
                    now,
                ),
            )

    def record_success(self, provider: str, model: str = "") -> None:
        now = _now_iso()
        with self._session() as conn:
            conn.execute(
                """
                INSERT INTO provider_runtime (
                    provider, model, last_success_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider, model) DO UPDATE SET
                    last_success_at = excluded.last_success_at,
                    updated_at = excluded.updated_at
                """,
                (provider, model, now, now, now),
            )

    def record_failure(
        self,
        provider: str,
        model: str = "",
        *,
        category: str,
        retry_after_seconds: float | None = None,
    ) -> None:
        now_iso = _now_iso()
        retry_after_at = (
            time.time() + max(0.0, retry_after_seconds)
            if retry_after_seconds is not None
            else None
        )
        with self._session() as conn:
            conn.execute(
                """
                INSERT INTO provider_runtime (
                    provider, model, retry_after_at, last_failure_at,
                    last_failure_category, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, model) DO UPDATE SET
                    retry_after_at = COALESCE(
                        excluded.retry_after_at, provider_runtime.retry_after_at
                    ),
                    last_failure_at = excluded.last_failure_at,
                    last_failure_category = excluded.last_failure_category,
                    updated_at = excluded.updated_at
                """,
                (
                    provider,
                    model,
                    retry_after_at,
                    now_iso,
                    category,
                    now_iso,
                    now_iso,
                ),
            )

    def clear_expired(self, *, now: float | None = None) -> int:
        current = time.time() if now is None else now
        with self._session() as conn:
            cursor = conn.execute(
                """
                UPDATE provider_runtime
                SET cooldown_until = NULL, cooldown_reason = NULL,
                    cooldown_category = NULL, updated_at = ?
                WHERE cooldown_until IS NOT NULL AND cooldown_until <= ?
                """,
                (_now_iso(), current),
            )
            return int(cursor.rowcount)

    def list(self) -> list[ProviderRuntime]:
        with self._session() as conn:
            rows = conn.execute(
                "SELECT * FROM provider_runtime ORDER BY provider, model"
            ).fetchall()
        return [self._row_to_record(row) for row in rows]
