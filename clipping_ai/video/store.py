"""SQLite persistence for video preparation jobs."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.highlights.models import HighlightResult
from clipping_ai.schemas import TranscriptResult
from clipping_ai.video.ingest import is_safe_job_id
from clipping_ai.video.models import (
    AudioChunkInfo,
    JobStage,
    JobStatus,
    SourceType,
    VideoJob,
    VideoMetadata,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS video_jobs (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_value TEXT NOT NULL,
    original_filename TEXT,
    source_hash TEXT,
    input_path TEXT,
    metadata_json TEXT,
    audio_path TEXT,
    audio_chunks_json TEXT NOT NULL DEFAULT '[]',
    transcript_json TEXT,
    transcript_error TEXT,
    transcript_provider TEXT,
    transcript_model TEXT,
    transcript_at TEXT,
    highlight_json TEXT,
    highlight_error TEXT,
    highlight_at TEXT,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_video_jobs_status
    ON video_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_video_jobs_hash
    ON video_jobs(source_hash);
CREATE INDEX IF NOT EXISTS idx_video_jobs_source
    ON video_jobs(source_type, source_value);
"""

_ADDED_COLUMNS = {
    "transcript_json": "TEXT",
    "transcript_error": "TEXT",
    "transcript_provider": "TEXT",
    "transcript_model": "TEXT",
    "transcript_at": "TEXT",
    "highlight_json": "TEXT",
    "highlight_error": "TEXT",
    "highlight_at": "TEXT",
}

_UPDATE_COLUMNS = {
    "source_value",
    "original_filename",
    "source_hash",
    "input_path",
    "metadata",
    "audio_path",
    "audio_chunks",
    "transcript",
    "transcript_error",
    "transcript_provider",
    "transcript_model",
    "transcript_at",
    "highlight_result",
    "highlight_error",
    "highlight_at",
    "status",
    "stage",
    "progress",
    "error",
    "started_at",
    "completed_at",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif isinstance(value, list):
        value = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in value
        ]
    return json.dumps(value, ensure_ascii=False)


class VideoJobStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._session() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        existing = {
            row["name"] for row in conn.execute("PRAGMA table_info(video_jobs)").fetchall()
        }
        for column, column_type in _ADDED_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE video_jobs ADD COLUMN {column} {column_type}")

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

    @staticmethod
    def _validate_id(job_id: str) -> str:
        if not is_safe_job_id(job_id):
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Invalid job ID.",
                retryable=False,
            )
        return job_id

    def _row_to_job(self, row: sqlite3.Row) -> VideoJob:
        metadata: VideoMetadata | None = None
        if row["metadata_json"]:
            try:
                metadata = VideoMetadata.model_validate_json(row["metadata_json"])
            except Exception:
                metadata = None
        chunks: list[AudioChunkInfo] = []
        if row["audio_chunks_json"]:
            try:
                raw_chunks = json.loads(row["audio_chunks_json"])
                chunks = [AudioChunkInfo.model_validate(item) for item in raw_chunks]
            except Exception:
                chunks = []
        error = None
        if row["error_json"]:
            try:
                error = json.loads(row["error_json"])
            except json.JSONDecodeError:
                error = {"message": row["error_json"]}
        transcript = None
        if row["transcript_json"]:
            try:
                transcript = TranscriptResult.model_validate_json(row["transcript_json"])
            except Exception:
                transcript = None
        highlight_result = None
        if row["highlight_json"]:
            try:
                highlight_result = HighlightResult.model_validate_json(row["highlight_json"])
            except Exception:
                highlight_result = None
        return VideoJob(
            id=row["id"],
            source_type=SourceType(row["source_type"]),
            source_value=row["source_value"],
            original_filename=row["original_filename"],
            source_hash=row["source_hash"],
            input_path=row["input_path"],
            metadata=metadata,
            audio_path=row["audio_path"],
            audio_chunks=chunks,
            transcript=transcript,
            transcript_error=row["transcript_error"],
            transcript_provider=row["transcript_provider"],
            transcript_model=row["transcript_model"],
            transcript_at=row["transcript_at"],
            highlight_result=highlight_result,
            highlight_error=row["highlight_error"],
            highlight_at=row["highlight_at"],
            status=JobStatus(row["status"]),
            stage=JobStage(row["stage"]),
            progress=float(row["progress"]),
            error=error,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )

    def create(
        self,
        *,
        job_id: str,
        source_type: SourceType,
        source_value: str,
        original_filename: str | None = None,
        source_hash: str | None = None,
        input_path: Path | None = None,
    ) -> VideoJob:
        self._validate_id(job_id)
        now = _now()
        with self._session() as conn:
            conn.execute(
                """
                INSERT INTO video_jobs (
                    id, source_type, source_value, original_filename, source_hash,
                    input_path, metadata_json, audio_path, audio_chunks_json,
                    status, stage, progress, error_json, created_at, updated_at,
                    started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, '[]', ?, ?, 0, NULL, ?, ?, NULL, NULL)
                """,
                (
                    job_id,
                    source_type.value,
                    source_value,
                    original_filename,
                    source_hash,
                    str(input_path) if input_path else None,
                    JobStatus.QUEUED.value,
                    JobStage.QUEUED.value,
                    now,
                    now,
                ),
            )
        return self.require(job_id)

    def get(self, job_id: str) -> VideoJob | None:
        self._validate_id(job_id)
        with self._session() as conn:
            row = conn.execute("SELECT * FROM video_jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def require(self, job_id: str) -> VideoJob:
        job = self.get(job_id)
        if job is None:
            raise AIError(
                ErrorCategory.NOT_FOUND,
                f"Video job '{job_id}' was not found.",
                retryable=False,
            )
        return job

    def update(self, job_id: str, **fields: Any) -> VideoJob:
        self._validate_id(job_id)
        unknown = set(fields) - _UPDATE_COLUMNS
        if unknown:
            raise ValueError(f"Unknown video job fields: {', '.join(sorted(unknown))}")
        json_columns = {
            "metadata": "metadata_json",
            "audio_chunks": "audio_chunks_json",
            "error": "error_json",
            "transcript": "transcript_json",
            "highlight_result": "highlight_json",
        }
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            column = json_columns.get(key, key)
            if key in json_columns:
                value = _json(value)
            elif isinstance(value, Path):
                value = str(value)
            elif isinstance(value, (JobStatus, JobStage, SourceType)):
                value = value.value
            assignments.append(f"{column} = ?")
            values.append(value)
        assignments.append("updated_at = ?")
        values.append(_now())
        values.append(job_id)
        with self._session() as conn:
            cursor = conn.execute(
                f"UPDATE video_jobs SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            if cursor.rowcount == 0:
                raise AIError(
                    ErrorCategory.NOT_FOUND,
                    f"Video job '{job_id}' was not found.",
                    retryable=False,
                )
        return self.require(job_id)

    def claim_next_queued(self) -> VideoJob | None:
        now = _now()
        claimed_id: str | None = None
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """
                    SELECT id FROM video_jobs
                    WHERE status = ?
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (JobStatus.QUEUED.value,),
                ).fetchone()
                if row is not None:
                    claimed_id = row["id"]
                    conn.execute(
                        """
                        UPDATE video_jobs
                        SET status = ?, stage = ?, progress = 0, error_json = NULL,
                            started_at = ?, completed_at = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            JobStatus.PROCESSING.value,
                            JobStage.QUEUED.value,
                            now,
                            now,
                            claimed_id,
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return self.require(claimed_id) if claimed_id else None

    def mark_processing(self, job_id: str) -> VideoJob:
        job = self.require(job_id)
        if job.status == JobStatus.READY:
            return job
        if job.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Video job '{job_id}' is {job.status.value}; retry it first.",
                retryable=False,
            )
        if job.status == JobStatus.QUEUED:
            return self.update(
                job_id,
                status=JobStatus.PROCESSING,
                stage=JobStage.QUEUED,
                progress=0.0,
                error=None,
                started_at=_now(),
                completed_at=None,
            )
        return job

    def mark_ready(self, job_id: str) -> VideoJob:
        return self.update(
            job_id,
            status=JobStatus.READY,
            stage=JobStage.READY,
            progress=1.0,
            error=None,
            completed_at=_now(),
        )

    def fail(self, job_id: str, error: dict[str, Any]) -> VideoJob:
        return self.update(
            job_id,
            status=JobStatus.FAILED,
            stage=JobStage.FAILED,
            error=error,
            completed_at=_now(),
        )

    def cancel(self, job_id: str) -> VideoJob:
        job = self.require(job_id)
        if job.status != JobStatus.QUEUED:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Only queued jobs can be cancelled; '{job_id}' is {job.status.value}.",
                retryable=False,
            )
        return self.update(
            job_id,
            status=JobStatus.CANCELLED,
            stage=JobStage.CANCELLED,
            progress=0.0,
            completed_at=_now(),
        )

    def retry(self, job_id: str) -> VideoJob:
        job = self.require(job_id)
        if job.status not in {JobStatus.FAILED, JobStatus.CANCELLED}:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Only failed or cancelled jobs can be retried; '{job_id}' is {job.status.value}.",
                retryable=False,
            )
        return self.update(
            job_id,
            status=JobStatus.QUEUED,
            stage=JobStage.QUEUED,
            progress=0.0,
            error=None,
            started_at=None,
            completed_at=None,
        )

    def recover_interrupted(self) -> int:
        now = _now()
        with self._session() as conn:
            cursor = conn.execute(
                """
                UPDATE video_jobs
                SET status = ?, stage = ?, progress = 0, error_json = NULL,
                    started_at = NULL, completed_at = NULL, updated_at = ?
                WHERE status = ?
                """,
                (
                    JobStatus.QUEUED.value,
                    JobStage.QUEUED.value,
                    now,
                    JobStatus.PROCESSING.value,
                ),
            )
            return int(cursor.rowcount)

    def find_by_hash(
        self,
        source_hash: str,
        *,
        statuses: Sequence[JobStatus] = (
            JobStatus.QUEUED,
            JobStatus.PROCESSING,
            JobStatus.READY,
        ),
    ) -> VideoJob | None:
        placeholders = ", ".join("?" for _ in statuses)
        values = [source_hash, *(status.value for status in statuses)]
        with self._session() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM video_jobs
                WHERE source_hash = ? AND status IN ({placeholders})
                ORDER BY created_at ASC
                LIMIT 1
                """,
                values,
            ).fetchone()
        return self._row_to_job(row) if row else None

    def find_by_source(
        self,
        source_type: SourceType,
        source_value: str,
        *,
        statuses: Sequence[JobStatus] = (
            JobStatus.QUEUED,
            JobStatus.PROCESSING,
            JobStatus.READY,
        ),
    ) -> VideoJob | None:
        placeholders = ", ".join("?" for _ in statuses)
        values = [source_type.value, source_value, *(status.value for status in statuses)]
        with self._session() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM video_jobs
                WHERE source_type = ? AND source_value = ? AND status IN ({placeholders})
                ORDER BY created_at ASC
                LIMIT 1
                """,
                values,
            ).fetchone()
        return self._row_to_job(row) if row else None

    def list(
        self,
        *,
        limit: int = 50,
        status: JobStatus | None = None,
    ) -> list[VideoJob]:
        limit = max(1, min(limit, 500))
        if status is None:
            with self._session() as conn:
                rows = conn.execute(
                    "SELECT * FROM video_jobs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        else:
            with self._session() as conn:
                rows = conn.execute(
                    "SELECT * FROM video_jobs WHERE status = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (status.value, limit),
                ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def count(self) -> int:
        with self._session() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM video_jobs").fetchone()
        return int(row["n"]) if row else 0
