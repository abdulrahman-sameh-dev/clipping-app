"""Durable stage records with lease ownership and checkpoint preservation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from clipping_ai.recovery.schema import migrate_jobs_db


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class StageError(RuntimeError):
    """Base error for durable stage operations."""


class StageOwnershipError(StageError):
    """Raised when a worker tries to mutate a stage it no longer owns."""


class StageArtifactError(StageError):
    """Raised when a required artifact is missing or cannot be validated."""


@dataclass(frozen=True)
class StageRecord:
    job_type: str
    job_id: str
    stage: str
    unit_key: str
    status: StageStatus
    attempt: int
    input_fingerprint: str | None = None
    checkpoint: Any = None
    artifact_path: Path | None = None
    artifact_sha256: str | None = None
    artifact_size: int | None = None
    error: dict[str, Any] | None = None
    lease_owner: str | None = None
    lease_expires_at: float | None = None
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None

    @property
    def lease_expired(self) -> bool:
        return (
            self.status == StageStatus.RUNNING
            and self.lease_expires_at is not None
            and self.lease_expires_at <= time.time()
        )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _iso_from_epoch(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, UTC).isoformat()


def _json_dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_load(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_error(error: dict[str, Any] | None) -> dict[str, Any] | None:
    if error is None:
        return None
    allowed = {"category", "message", "retryable", "retry_after", "details"}
    return {key: value for key, value in error.items() if key in allowed}


def _owns_lease(record: StageRecord | None, owner: str, now: float) -> bool:
    return bool(
        record is not None
        and record.status == StageStatus.RUNNING
        and record.lease_owner == owner
        and record.lease_expires_at is not None
        and record.lease_expires_at > now
    )


@dataclass
class StageStore:
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

    @contextmanager
    def _immediate(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _row_to_record(self, row: sqlite3.Row) -> StageRecord:
        return StageRecord(
            job_type=row["job_type"],
            job_id=row["job_id"],
            stage=row["stage"],
            unit_key=row["unit_key"],
            status=StageStatus(row["status"]),
            attempt=int(row["attempt"]),
            input_fingerprint=row["input_fingerprint"],
            checkpoint=_json_load(row["checkpoint_json"]),
            artifact_path=Path(row["artifact_path"]) if row["artifact_path"] else None,
            artifact_sha256=row["artifact_sha256"],
            artifact_size=int(row["artifact_size"])
            if row["artifact_size"] is not None
            else None,
            error=_json_load(row["error_json"]),
            lease_owner=row["lease_owner"],
            lease_expires_at=float(row["lease_expires_at"])
            if row["lease_expires_at"] is not None
            else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _key(
        job_type: str, job_id: str, stage: str, unit_key: str
    ) -> tuple[str, str, str, str]:
        return job_type, job_id, stage, unit_key

    def ensure(
        self,
        *,
        job_type: str,
        job_id: str,
        stage: str,
        unit_key: str = "",
        status: StageStatus = StageStatus.PENDING,
        input_fingerprint: str | None = None,
        checkpoint: Any = None,
        artifact_path: Path | None = None,
    ) -> StageRecord:
        """Create a stage once, returning the existing record when present."""
        now = _now_iso()
        with self._immediate() as conn:
            conn.execute(
                """
                INSERT INTO job_stages (
                    job_type, job_id, stage, unit_key, status, attempt,
                    input_fingerprint, checkpoint_json, artifact_path,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                ON CONFLICT(job_type, job_id, stage, unit_key) DO NOTHING
                """,
                (
                    job_type,
                    job_id,
                    stage,
                    unit_key,
                    status.value,
                    input_fingerprint,
                    _json_dump(checkpoint),
                    str(artifact_path) if artifact_path else None,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM job_stages
                WHERE job_type = ? AND job_id = ? AND stage = ? AND unit_key = ?
                """,
                self._key(job_type, job_id, stage, unit_key),
            ).fetchone()
        if row is None:  # pragma: no cover - defensive
            raise StageError("Stage could not be created.")
        return self._row_to_record(row)

    def get(
        self, *, job_type: str, job_id: str, stage: str, unit_key: str = ""
    ) -> StageRecord | None:
        with self._session() as conn:
            row = conn.execute(
                """
                SELECT * FROM job_stages
                WHERE job_type = ? AND job_id = ? AND stage = ? AND unit_key = ?
                """,
                self._key(job_type, job_id, stage, unit_key),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def list_stages(
        self,
        *,
        job_type: str | None = None,
        job_id: str | None = None,
        status: StageStatus | None = None,
        limit: int | None = None,
    ) -> list[StageRecord]:
        clauses: list[str] = []
        values: list[Any] = []
        if job_type is not None:
            clauses.append("job_type = ?")
            values.append(job_type)
        if job_id is not None:
            clauses.append("job_id = ?")
            values.append(job_id)
        if status is not None:
            clauses.append("status = ?")
            values.append(status.value)
        query = "SELECT * FROM job_stages"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, job_id, stage, unit_key"
        if limit is not None:
            query += " LIMIT ?"
            values.append(max(1, int(limit)))
        with self._session() as conn:
            rows = conn.execute(query, values).fetchall()
        return [self._row_to_record(row) for row in rows]

    def claim_next(
        self,
        *,
        owner: str,
        lease_seconds: float = 300.0,
        job_type: str | None = None,
        job_id: str | None = None,
        stage: str | None = None,
        unit_key: str | None = None,
        now: float | None = None,
    ) -> StageRecord | None:
        """Claim a pending stage or a running stage whose lease has expired."""
        current = time.time() if now is None else now
        expires = current + lease_seconds
        clauses = [
            "(status = ? OR (status = ? AND lease_expires_at IS NOT NULL "
            "AND lease_expires_at <= ?))"
        ]
        values: list[Any] = [
            StageStatus.PENDING.value,
            StageStatus.RUNNING.value,
            current,
        ]
        if job_type is not None:
            clauses.append("job_type = ?")
            values.append(job_type)
        if job_id is not None:
            clauses.append("job_id = ?")
            values.append(job_id)
        if stage is not None:
            clauses.append("stage = ?")
            values.append(stage)
        if unit_key is not None:
            clauses.append("unit_key = ?")
            values.append(unit_key)
        where = " AND ".join(clauses)
        with self._immediate() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM job_stages
                WHERE {where}
                ORDER BY created_at, job_id, stage, unit_key
                LIMIT 1
                """,
                values,
            ).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                f"""
                UPDATE job_stages
                SET status = ?, attempt = attempt + 1, lease_owner = ?,
                    lease_expires_at = ?, error_json = NULL, completed_at = NULL,
                    updated_at = ?
                WHERE job_type = ? AND job_id = ? AND stage = ? AND unit_key = ?
                  AND {where}
                """,
                (
                    StageStatus.RUNNING.value,
                    owner,
                    expires,
                    _iso_from_epoch(current),
                    row["job_type"],
                    row["job_id"],
                    row["stage"],
                    row["unit_key"],
                    *values,
                ),
            )
            if cursor.rowcount == 0:
                raise StageError("Stage claim lost a race; retry the claim.")
            updated = conn.execute(
                """
                SELECT * FROM job_stages
                WHERE job_type = ? AND job_id = ? AND stage = ? AND unit_key = ?
                """,
                (row["job_type"], row["job_id"], row["stage"], row["unit_key"]),
            ).fetchone()
        if updated is None:  # pragma: no cover - defensive
            raise StageError("Claimed stage disappeared.")
        return self._row_to_record(updated)

    def reset(
        self,
        *,
        job_type: str,
        job_id: str,
        stage: str,
        unit_key: str = "",
        input_fingerprint: str,
        clear_checkpoint: bool = False,
        now: float | None = None,
    ) -> StageRecord:
        """Reopen a stage so a changed fingerprint or invalid artifact reruns.

        Existing checkpoints are preserved unless ``clear_checkpoint`` is set.
        A live lease cannot be reset; callers must recover dead leases first.
        """
        current = time.time() if now is None else now
        checkpoint_sql = ", checkpoint_json = NULL" if clear_checkpoint else ""
        with self._session() as conn:
            cursor = conn.execute(
                f"""
                UPDATE job_stages
                SET status = ?, input_fingerprint = ?, lease_owner = NULL,
                    lease_expires_at = NULL, error_json = NULL,
                    completed_at = NULL, artifact_path = NULL,
                    artifact_sha256 = NULL, artifact_size = NULL,
                    updated_at = ?{checkpoint_sql}
                WHERE job_type = ? AND job_id = ? AND stage = ? AND unit_key = ?
                  AND (
                      status IN (?, ?, ?)
                      OR (status = ? AND lease_expires_at IS NOT NULL
                          AND lease_expires_at <= ?)
                  )
                """,
                (
                    StageStatus.PENDING.value,
                    input_fingerprint,
                    _iso_from_epoch(current),
                    job_type,
                    job_id,
                    stage,
                    unit_key,
                    StageStatus.PENDING.value,
                    StageStatus.DONE.value,
                    StageStatus.FAILED.value,
                    StageStatus.RUNNING.value,
                    current,
                ),
            )
            if cursor.rowcount != 1:
                raise StageOwnershipError("Stage is currently owned by another worker.")
        record = self.get(
            job_type=job_type, job_id=job_id, stage=stage, unit_key=unit_key
        )
        if record is None:  # pragma: no cover - defensive
            raise StageError("Stage disappeared after reset.")
        return record

    def release_running(
        self,
        *,
        job_type: str,
        job_id: str | None = None,
        stage: str | None = None,
        now: float | None = None,
    ) -> int:
        """Recover running stages after a process restart under the data lock."""
        current = time.time() if now is None else now
        clauses = ["job_type = ?", "status = ?"]
        values: list[Any] = [job_type, StageStatus.RUNNING.value]
        if job_id is not None:
            clauses.append("job_id = ?")
            values.append(job_id)
        if stage is not None:
            clauses.append("stage = ?")
            values.append(stage)
        with self._session() as conn:
            cursor = conn.execute(
                f"""
                UPDATE job_stages
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE {' AND '.join(clauses)}
                """,
                (
                    StageStatus.PENDING.value,
                    _iso_from_epoch(current),
                    *values,
                ),
            )
            return int(cursor.rowcount)

    def heartbeat(
        self,
        *,
        job_type: str,
        job_id: str,
        stage: str,
        unit_key: str = "",
        owner: str,
        lease_seconds: float = 300.0,
        now: float | None = None,
    ) -> bool:
        current = time.time() if now is None else now
        with self._session() as conn:
            cursor = conn.execute(
                """
                UPDATE job_stages
                SET lease_expires_at = ?, updated_at = ?
                WHERE job_type = ? AND job_id = ? AND stage = ? AND unit_key = ?
                  AND status = ? AND lease_owner = ?
                  AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                """,
                (
                    current + lease_seconds,
                    _iso_from_epoch(current),
                    job_type,
                    job_id,
                    stage,
                    unit_key,
                    StageStatus.RUNNING.value,
                    owner,
                    current,
                ),
            )
            return cursor.rowcount == 1

    def save_checkpoint(
        self,
        *,
        job_type: str,
        job_id: str,
        stage: str,
        unit_key: str = "",
        owner: str,
        checkpoint: Any,
        now: float | None = None,
    ) -> StageRecord:
        current = time.time() if now is None else now
        with self._session() as conn:
            cursor = conn.execute(
                """
                UPDATE job_stages
                SET checkpoint_json = ?, updated_at = ?
                WHERE job_type = ? AND job_id = ? AND stage = ? AND unit_key = ?
                  AND status = ? AND lease_owner = ?
                  AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                """,
                (
                    _json_dump(checkpoint),
                    _iso_from_epoch(current),
                    job_type,
                    job_id,
                    stage,
                    unit_key,
                    StageStatus.RUNNING.value,
                    owner,
                    current,
                ),
            )
            if cursor.rowcount != 1:
                raise StageOwnershipError("Stage ownership was lost before checkpoint save.")
        record = self.get(
            job_type=job_type, job_id=job_id, stage=stage, unit_key=unit_key
        )
        if record is None:  # pragma: no cover - defensive
            raise StageError("Stage disappeared after checkpoint save.")
        return record

    def complete(
        self,
        *,
        job_type: str,
        job_id: str,
        stage: str,
        unit_key: str = "",
        owner: str,
        checkpoint: Any = None,
        artifact_path: Path | str | None = None,
        require_artifact: bool = False,
        now: float | None = None,
    ) -> StageRecord:
        """Complete a stage only after its checkpoint and artifact are durable."""
        current_time = time.time() if now is None else now
        current = self.get(
            job_type=job_type, job_id=job_id, stage=stage, unit_key=unit_key
        )
        if not _owns_lease(current, owner, current_time):
            raise StageOwnershipError("Stage ownership was lost before completion.")
        artifact = Path(artifact_path) if artifact_path is not None else None
        if require_artifact and artifact is None:
            raise StageArtifactError("A required artifact path was not supplied.")
        artifact_hash: str | None = None
        artifact_size: int | None = None
        if artifact is not None:
            if not artifact.is_file():
                raise StageArtifactError(f"Required artifact does not exist: {artifact}")
            artifact_hash = _sha256(artifact)
            artifact_size = artifact.stat().st_size

        updates = [
            "status = ?",
            "lease_owner = NULL",
            "lease_expires_at = NULL",
            "error_json = NULL",
            "completed_at = ?",
            "updated_at = ?",
        ]
        values: list[Any] = [
            StageStatus.DONE.value,
            _now_iso(),
            _now_iso(),
        ]
        if checkpoint is not None:
            updates.insert(0, "checkpoint_json = ?")
            values.insert(0, _json_dump(checkpoint))
        if artifact is not None:
            updates.extend(
                ["artifact_path = ?", "artifact_sha256 = ?", "artifact_size = ?"]
            )
            values.extend([str(artifact), artifact_hash, artifact_size])
        values.extend([job_type, job_id, stage, unit_key, StageStatus.RUNNING.value, owner])
        with self._session() as conn:
            cursor = conn.execute(
                f"""
                UPDATE job_stages
                SET {', '.join(updates)}
                WHERE job_type = ? AND job_id = ? AND stage = ? AND unit_key = ?
                  AND status = ? AND lease_owner = ?
                  AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                """,
                [*values, current_time],
            )
            if cursor.rowcount != 1:
                raise StageOwnershipError("Stage ownership was lost before completion.")
        record = self.get(
            job_type=job_type, job_id=job_id, stage=stage, unit_key=unit_key
        )
        if record is None:  # pragma: no cover - defensive
            raise StageError("Stage disappeared after completion.")
        return record

    def fail(
        self,
        *,
        job_type: str,
        job_id: str,
        stage: str,
        unit_key: str = "",
        owner: str,
        error: dict[str, Any],
        retryable: bool,
        now: float | None = None,
    ) -> StageRecord:
        """Record a failure without discarding the last valid checkpoint."""
        current = time.time() if now is None else now
        stamp = _iso_from_epoch(current)
        next_status = StageStatus.PENDING if retryable else StageStatus.FAILED
        with self._session() as conn:
            cursor = conn.execute(
                """
                UPDATE job_stages
                SET status = ?, error_json = ?, lease_owner = NULL,
                    lease_expires_at = NULL, completed_at = ?, updated_at = ?
                WHERE job_type = ? AND job_id = ? AND stage = ? AND unit_key = ?
                  AND status = ? AND lease_owner = ?
                  AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                """,
                (
                    next_status.value,
                    _json_dump(_safe_error(error)),
                    None if retryable else stamp,
                    stamp,
                    job_type,
                    job_id,
                    stage,
                    unit_key,
                    StageStatus.RUNNING.value,
                    owner,
                    current,
                ),
            )
            if cursor.rowcount != 1:
                raise StageOwnershipError("Stage ownership was lost before failure update.")
        record = self.get(
            job_type=job_type, job_id=job_id, stage=stage, unit_key=unit_key
        )
        if record is None:  # pragma: no cover - defensive
            raise StageError("Stage disappeared after failure update.")
        return record

    def release(
        self,
        *,
        job_type: str,
        job_id: str,
        stage: str,
        unit_key: str = "",
        owner: str,
        now: float | None = None,
    ) -> bool:
        """Return a running stage to pending after a clean cooperative stop."""
        current = time.time() if now is None else now
        with self._session() as conn:
            cursor = conn.execute(
                """
                UPDATE job_stages
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE job_type = ? AND job_id = ? AND stage = ? AND unit_key = ?
                  AND status = ? AND lease_owner = ?
                  AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                """,
                (
                    StageStatus.PENDING.value,
                    _iso_from_epoch(current),
                    job_type,
                    job_id,
                    stage,
                    unit_key,
                    StageStatus.RUNNING.value,
                    owner,
                    current,
                ),
            )
            return cursor.rowcount == 1

    def expire_leases(self, *, now: float | None = None) -> int:
        current = time.time() if now is None else now
        with self._session() as conn:
            cursor = conn.execute(
                """
                UPDATE job_stages
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE status = ? AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= ?
                """,
                (
                    StageStatus.PENDING.value,
                    _iso_from_epoch(current),
                    StageStatus.RUNNING.value,
                    current,
                ),
            )
            return int(cursor.rowcount)

    def list_expired(self, *, now: float | None = None) -> list[StageRecord]:
        current = time.time() if now is None else now
        with self._session() as conn:
            rows = conn.execute(
                """
                SELECT * FROM job_stages
                WHERE status = ? AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= ?
                ORDER BY lease_expires_at, created_at
                """,
                (StageStatus.RUNNING.value, current),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def list_inconsistent(self) -> list[StageRecord]:
        with self._session() as conn:
            rows = conn.execute(
                """
                SELECT * FROM job_stages
                WHERE (status = ? AND (lease_owner IS NULL OR lease_expires_at IS NULL))
                   OR (status = ? AND (lease_owner IS NOT NULL OR lease_expires_at IS NOT NULL))
                   OR (status = ? AND (completed_at IS NULL OR lease_owner IS NOT NULL))
                   OR (status = ? AND completed_at IS NULL)
                ORDER BY created_at, job_id, stage
                """,
                (
                    StageStatus.RUNNING.value,
                    StageStatus.PENDING.value,
                    StageStatus.DONE.value,
                    StageStatus.FAILED.value,
                ),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]
