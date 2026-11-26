"""SQLite persistence for clip rendering jobs."""

from __future__ import annotations

import builtins
import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clipping_ai.clips.models import (
    ClipFit,
    ClipFormat,
    ClipJob,
    ClipMetadataStatus,
    ClipQuality,
    ClipStage,
    ClipStatus,
)
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.recovery.idempotency import reserve_idempotency
from clipping_ai.recovery.schema import migrate_jobs_db
from clipping_ai.video.ingest import is_safe_job_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_jobs (
    id TEXT PRIMARY KEY,
    source_job_id TEXT NOT NULL,
    highlight_index INTEGER,
    start_time REAL NOT NULL,
    end_time REAL NOT NULL,
    duration REAL NOT NULL,
    format TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    fps REAL NOT NULL,
    quality TEXT NOT NULL,
    fit TEXT NOT NULL,
    captions INTEGER NOT NULL DEFAULT 0,
    caption_language TEXT,
    caption_path TEXT,
    output_path TEXT,
    size_bytes INTEGER,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    error_json TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    title TEXT,
    description TEXT,
    hashtags_json TEXT NOT NULL DEFAULT '[]',
    visual_settings_json TEXT NOT NULL DEFAULT '{}',
    metadata_language TEXT,
    metadata_status TEXT,
    metadata_error TEXT,
    metadata_provider TEXT,
    metadata_model TEXT,
    metadata_at TEXT,
    metadata_edited_at TEXT,
    published INTEGER NOT NULL DEFAULT 0,
    published_at TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_clip_jobs_source
    ON clip_jobs(source_job_id, created_at);
CREATE INDEX IF NOT EXISTS idx_clip_jobs_status
    ON clip_jobs(status, created_at);
"""

_ADDED_COLUMNS = {
    "title": "TEXT",
    "description": "TEXT",
    "hashtags_json": "TEXT NOT NULL DEFAULT '[]'",
    "visual_settings_json": "TEXT NOT NULL DEFAULT '{}'",
    "metadata_language": "TEXT",
    "metadata_status": "TEXT",
    "metadata_error": "TEXT",
    "metadata_provider": "TEXT",
    "metadata_model": "TEXT",
    "metadata_at": "TEXT",
    "metadata_edited_at": "TEXT",
    "published": "INTEGER NOT NULL DEFAULT 0",
    "published_at": "TEXT",
    "deleted_at": "TEXT",
}

_UPDATE_COLUMNS = {
    "highlight_index",
    "start_time",
    "end_time",
    "duration",
    "format",
    "width",
    "height",
    "fps",
    "quality",
    "fit",
    "captions",
    "caption_language",
    "caption_path",
    "output_path",
    "size_bytes",
    "status",
    "stage",
    "progress",
    "error",
    "cancel_requested",
    "title",
    "description",
    "hashtags",
    "visual_settings",
    "metadata_language",
    "metadata_status",
    "metadata_error",
    "metadata_provider",
    "metadata_model",
    "metadata_at",
    "metadata_edited_at",
    "published",
    "published_at",
    "deleted_at",
    "started_at",
    "completed_at",
}

_ENUM_COLUMNS = (
    ClipFormat,
    ClipQuality,
    ClipFit,
    ClipStatus,
    ClipStage,
    ClipMetadataStatus,
)

_JSON_COLUMNS = {"hashtags": "hashtags_json", "visual_settings": "visual_settings_json"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False)


class ClipJobStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._session() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)
        migrate_jobs_db(self.db_path)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        existing = {
            row["name"] for row in conn.execute("PRAGMA table_info(clip_jobs)").fetchall()
        }
        for column, column_type in _ADDED_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE clip_jobs ADD COLUMN {column} {column_type}")

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
    def _validate_id(clip_id: str) -> str:
        if not is_safe_job_id(clip_id):
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Invalid clip ID.",
                retryable=False,
            )
        return clip_id

    def _row_to_job(self, row: sqlite3.Row) -> ClipJob:
        error = None
        if row["error_json"]:
            try:
                error = json.loads(row["error_json"])
            except json.JSONDecodeError:
                error = {"message": row["error_json"]}
        visual_settings: dict[str, Any] = {}
        if row["visual_settings_json"]:
            try:
                raw_visual = json.loads(row["visual_settings_json"])
                if isinstance(raw_visual, dict):
                    visual_settings = raw_visual
            except json.JSONDecodeError:
                visual_settings = {}
        hashtags: list[str] = []
        if row["hashtags_json"]:
            try:
                raw_hashtags = json.loads(row["hashtags_json"])
                if isinstance(raw_hashtags, list):
                    hashtags = [str(item) for item in raw_hashtags]
            except json.JSONDecodeError:
                hashtags = []
        return ClipJob(
            id=row["id"],
            source_job_id=row["source_job_id"],
            highlight_index=row["highlight_index"],
            start_time=float(row["start_time"]),
            end_time=float(row["end_time"]),
            duration=float(row["duration"]),
            format=ClipFormat(row["format"]),
            width=int(row["width"]),
            height=int(row["height"]),
            fps=float(row["fps"]),
            quality=ClipQuality(row["quality"]),
            fit=ClipFit(row["fit"]),
            captions=bool(row["captions"]),
            caption_language=row["caption_language"],
            caption_path=Path(row["caption_path"]) if row["caption_path"] else None,
            output_path=Path(row["output_path"]) if row["output_path"] else None,
            size_bytes=int(row["size_bytes"]) if row["size_bytes"] is not None else None,
            status=ClipStatus(row["status"]),
            stage=ClipStage(row["stage"]),
            progress=float(row["progress"]),
            error=error,
            cancel_requested=bool(row["cancel_requested"]),
            title=row["title"],
            description=row["description"],
            hashtags=hashtags,
            visual_settings=visual_settings,
            metadata_language=row["metadata_language"],
            metadata_status=(
                ClipMetadataStatus(row["metadata_status"])
                if row["metadata_status"]
                else None
            ),
            metadata_error=row["metadata_error"],
            metadata_provider=row["metadata_provider"],
            metadata_model=row["metadata_model"],
            metadata_at=row["metadata_at"],
            metadata_edited_at=row["metadata_edited_at"],
            published=bool(row["published"]),
            published_at=row["published_at"],
            deleted_at=row["deleted_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )

    def create(self, **fields: Any) -> ClipJob:
        now = _now()
        fields.setdefault("created_at", now)
        fields.setdefault("updated_at", now)
        job = ClipJob.model_validate(fields)
        self._validate_id(job.id)
        with self._session() as conn:
            conn.execute(
                """
                INSERT INTO clip_jobs (
                    id, source_job_id, highlight_index, start_time, end_time,
                    duration, format, width, height, fps, quality, fit, captions,
                    caption_language, caption_path, output_path, size_bytes,
                    status, stage, progress, error_json, cancel_requested,
                    title, description, hashtags_json, visual_settings_json,
                    metadata_language, metadata_status, metadata_error,
                    metadata_provider, metadata_model, metadata_at,
                    metadata_edited_at, published, published_at, deleted_at,
                    created_at, updated_at, started_at, completed_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    job.id,
                    job.source_job_id,
                    job.highlight_index,
                    job.start_time,
                    job.end_time,
                    job.duration,
                    job.format.value,
                    job.width,
                    job.height,
                    job.fps,
                    job.quality.value,
                    job.fit.value,
                    int(job.captions),
                    job.caption_language,
                    str(job.caption_path) if job.caption_path else None,
                    str(job.output_path) if job.output_path else None,
                    job.size_bytes,
                    job.status.value,
                    job.stage.value,
                    job.progress,
                    _json(job.error),
                    int(job.cancel_requested),
                    job.title,
                    job.description,
                    _json(job.hashtags),
                    _json(job.visual_settings),
                    job.metadata_language,
                    job.metadata_status.value if job.metadata_status else None,
                    job.metadata_error,
                    job.metadata_provider,
                    job.metadata_model,
                    job.metadata_at,
                    job.metadata_edited_at,
                    int(job.published),
                    job.published_at,
                    job.deleted_at,
                    job.created_at,
                    job.updated_at,
                    job.started_at,
                    job.completed_at,
                ),
            )
        return self.require(job.id)

    def create_idempotent(
        self, *, idempotency_key: str, **fields: Any
    ) -> tuple[ClipJob, bool]:
        """Create a clip job once for a stable request fingerprint."""
        now = _now()
        fields.setdefault("created_at", now)
        fields.setdefault("updated_at", now)
        job = ClipJob.model_validate(fields)
        self._validate_id(job.id)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing_id = reserve_idempotency(
                    conn,
                    scope="clip.create",
                    key=idempotency_key,
                    job_type="clip",
                    job_id=job.id,
                )
                if existing_id is not None:
                    row = conn.execute(
                        "SELECT * FROM clip_jobs WHERE id = ?", (existing_id,)
                    ).fetchone()
                    conn.commit()
                    if row is None:
                        raise AIError(
                            ErrorCategory.NOT_FOUND,
                            f"Clip job '{existing_id}' was not found.",
                            retryable=False,
                        )
                    return self._row_to_job(row), True

                conn.execute(
                    """
                    INSERT INTO clip_jobs (
                        id, source_job_id, highlight_index, start_time, end_time,
                        duration, format, width, height, fps, quality, fit, captions,
                        caption_language, caption_path, output_path, size_bytes,
                        status, stage, progress, error_json, cancel_requested,
                        title, description, hashtags_json, visual_settings_json,
                        metadata_language, metadata_status, metadata_error,
                        metadata_provider, metadata_model, metadata_at,
                        metadata_edited_at, published, published_at, deleted_at,
                        created_at, updated_at, started_at, completed_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        job.id,
                        job.source_job_id,
                        job.highlight_index,
                        job.start_time,
                        job.end_time,
                        job.duration,
                        job.format.value,
                        job.width,
                        job.height,
                        job.fps,
                        job.quality.value,
                        job.fit.value,
                        int(job.captions),
                        job.caption_language,
                        str(job.caption_path) if job.caption_path else None,
                        str(job.output_path) if job.output_path else None,
                        job.size_bytes,
                        job.status.value,
                        job.stage.value,
                        job.progress,
                        _json(job.error),
                        int(job.cancel_requested),
                        job.title,
                        job.description,
                        _json(job.hashtags),
                        _json(job.visual_settings),
                        job.metadata_language,
                        job.metadata_status.value if job.metadata_status else None,
                        job.metadata_error,
                        job.metadata_provider,
                        job.metadata_model,
                        job.metadata_at,
                        job.metadata_edited_at,
                        int(job.published),
                        job.published_at,
                        job.deleted_at,
                        job.created_at,
                        job.updated_at,
                        job.started_at,
                        job.completed_at,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return self.require(job.id), False

    def get(self, clip_id: str) -> ClipJob | None:
        self._validate_id(clip_id)
        with self._session() as conn:
            row = conn.execute("SELECT * FROM clip_jobs WHERE id = ?", (clip_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def require(self, clip_id: str) -> ClipJob:
        job = self.get(clip_id)
        if job is None:
            raise AIError(
                ErrorCategory.NOT_FOUND,
                f"Clip job '{clip_id}' was not found.",
                retryable=False,
            )
        return job

    def update(self, clip_id: str, **fields: Any) -> ClipJob:
        self._validate_id(clip_id)
        unknown = set(fields) - _UPDATE_COLUMNS
        if unknown:
            raise ValueError(f"Unknown clip job fields: {', '.join(sorted(unknown))}")
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            column = "error_json" if key == "error" else _JSON_COLUMNS.get(key, key)
            if key in {"error", "hashtags", "visual_settings"}:
                value = _json(value)
            elif isinstance(value, Path):
                value = str(value)
            elif isinstance(value, _ENUM_COLUMNS):
                value = value.value
            elif isinstance(value, bool):
                value = int(value)
            assignments.append(f"{column} = ?")
            values.append(value)
        assignments.append("updated_at = ?")
        values.append(_now())
        values.append(clip_id)
        with self._session() as conn:
            cursor = conn.execute(
                f"UPDATE clip_jobs SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            if cursor.rowcount == 0:
                raise AIError(
                    ErrorCategory.NOT_FOUND,
                    f"Clip job '{clip_id}' was not found.",
                    retryable=False,
                )
        return self.require(clip_id)

    def claim_next_queued(self) -> ClipJob | None:
        now = _now()
        claimed_id: str | None = None
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """
                    SELECT id FROM clip_jobs
                    WHERE status = ? AND cancel_requested = 0 AND deleted_at IS NULL
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (ClipStatus.QUEUED.value,),
                ).fetchone()
                if row is not None:
                    claimed_id = row["id"]
                    conn.execute(
                        """
                        UPDATE clip_jobs
                        SET status = ?, stage = ?, progress = 0, error_json = NULL,
                            started_at = ?, completed_at = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            ClipStatus.PROCESSING.value,
                            ClipStage.QUEUED.value,
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

    def mark_processing(self, clip_id: str) -> ClipJob:
        job = self.require(clip_id)
        if job.status == ClipStatus.READY:
            return job
        if job.status in {ClipStatus.FAILED, ClipStatus.CANCELLED}:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Clip job '{clip_id}' is {job.status.value}; retry it first.",
                retryable=False,
            )
        if job.status == ClipStatus.QUEUED:
            return self.update(
                clip_id,
                status=ClipStatus.PROCESSING,
                stage=ClipStage.QUEUED,
                progress=0.0,
                error=None,
                started_at=_now(),
                completed_at=None,
            )
        return job

    def mark_ready(
        self,
        clip_id: str,
        *,
        output_path: Path,
        size_bytes: int,
        caption_path: Path | None = None,
    ) -> ClipJob:
        return self.update(
            clip_id,
            status=ClipStatus.READY,
            stage=ClipStage.READY,
            progress=1.0,
            error=None,
            output_path=output_path,
            size_bytes=size_bytes,
            caption_path=caption_path,
            completed_at=_now(),
        )

    def fail(self, clip_id: str, error: dict[str, Any]) -> ClipJob:
        return self.update(
            clip_id,
            status=ClipStatus.FAILED,
            stage=ClipStage.FAILED,
            error=error,
            completed_at=_now(),
        )

    def request_cancel(self, clip_id: str) -> ClipJob:
        job = self.require(clip_id)
        if job.status in {ClipStatus.READY, ClipStatus.FAILED, ClipStatus.CANCELLED}:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Clip job '{clip_id}' is already {job.status.value}.",
                retryable=False,
            )
        if job.status == ClipStatus.QUEUED:
            return self.update(
                clip_id,
                status=ClipStatus.CANCELLED,
                stage=ClipStage.CANCELLED,
                progress=0.0,
                cancel_requested=True,
                completed_at=_now(),
            )
        return self.update(clip_id, cancel_requested=True)

    def is_cancel_requested(self, clip_id: str) -> bool:
        return self.require(clip_id).cancel_requested

    def mark_cancelled(self, clip_id: str) -> ClipJob:
        return self.update(
            clip_id,
            status=ClipStatus.CANCELLED,
            stage=ClipStage.CANCELLED,
            cancel_requested=True,
            completed_at=_now(),
        )

    def retry(self, clip_id: str) -> ClipJob:
        job = self.require(clip_id)
        if job.status not in {ClipStatus.FAILED, ClipStatus.CANCELLED}:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Only failed or cancelled clip jobs can be retried; "
                f"'{clip_id}' is {job.status.value}.",
                retryable=False,
            )
        return self.update(
            clip_id,
            status=ClipStatus.QUEUED,
            stage=ClipStage.QUEUED,
            progress=0.0,
            error=None,
            cancel_requested=False,
            output_path=None,
            size_bytes=None,
            started_at=None,
            completed_at=None,
        )

    def recover_interrupted(self) -> int:
        now = _now()
        with self._session() as conn:
            cancelled = conn.execute(
                """
                UPDATE clip_jobs
                SET status = ?, stage = ?, cancel_requested = 1,
                    completed_at = ?, updated_at = ?
                WHERE status = ? AND cancel_requested = 1 AND deleted_at IS NULL
                """,
                (
                    ClipStatus.CANCELLED.value,
                    ClipStage.CANCELLED.value,
                    now,
                    now,
                    ClipStatus.PROCESSING.value,
                ),
            )
            requeued = conn.execute(
                """
                UPDATE clip_jobs
                SET status = ?, stage = ?, progress = 0, error_json = NULL,
                    started_at = NULL, completed_at = NULL, updated_at = ?
                WHERE status = ? AND cancel_requested = 0 AND deleted_at IS NULL
                """,
                (
                    ClipStatus.QUEUED.value,
                    ClipStage.QUEUED.value,
                    now,
                    ClipStatus.PROCESSING.value,
                ),
            )
            return int(cancelled.rowcount) + int(requeued.rowcount)

    def list(
        self,
        *,
        source_job_id: str | None = None,
        status: ClipStatus | None = None,
        ids: list[str] | None = None,
        limit: int = 50,
        deleted: bool = False,
    ) -> list[ClipJob]:
        if ids is not None and not ids:
            return []
        limit = max(1, min(limit, 500))
        clauses: list[str] = ["deleted_at IS NOT NULL" if deleted else "deleted_at IS NULL"]
        values: list[Any] = []
        if source_job_id is not None:
            clauses.append("source_job_id = ?")
            values.append(source_job_id)
        if status is not None:
            clauses.append("status = ?")
            values.append(status.value)
        if ids is not None:
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"id IN ({placeholders})")
            values.extend(ids)
        values.append(limit)
        with self._session() as conn:
            rows = conn.execute(
                f"SELECT * FROM clip_jobs WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC LIMIT ?",
                values,
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def list_deleted(self, *, limit: int = 500) -> builtins.list[ClipJob]:
        return self.list(limit=limit, deleted=True)

    def delete_row(self, clip_id: str) -> None:
        self._validate_id(clip_id)
        with self._session() as conn:
            conn.execute("DELETE FROM clip_jobs WHERE id = ?", (clip_id,))

    def count(self, *, deleted: bool = False) -> int:
        clause = "deleted_at IS NOT NULL" if deleted else "deleted_at IS NULL"
        with self._session() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM clip_jobs WHERE {clause}"
            ).fetchone()
        return int(row["n"]) if row else 0

    def pending_metadata_source_ids(self, *, limit: int = 500) -> builtins.list[str]:
        limit = max(1, min(limit, 500))
        with self._session() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT source_job_id
                FROM clip_jobs
                WHERE deleted_at IS NULL
                  AND metadata_status IN (?, ?)
                LIMIT ?
                """,
                (
                    ClipMetadataStatus.PENDING.value,
                    ClipMetadataStatus.DEFERRED_QUOTA.value,
                    limit,
                ),
            ).fetchall()
        return [row["source_job_id"] for row in rows]

    def counts_by_source(
        self, source_ids: Sequence[str]
    ) -> dict[str, dict[str, int]]:
        counts = {
            source_id: {"total": 0, "published": 0} for source_id in set(source_ids)
        }
        if not counts:
            return counts
        ids = list(counts)
        placeholders = ",".join("?" for _ in ids)
        with self._session() as conn:
            rows = conn.execute(
                f"""
                SELECT source_job_id,
                       COUNT(*) AS total,
                       SUM(CASE WHEN published = 1 THEN 1 ELSE 0 END) AS published
                FROM clip_jobs
                WHERE deleted_at IS NULL AND source_job_id IN ({placeholders})
                GROUP BY source_job_id
                """,
                ids,
            ).fetchall()
        for row in rows:
            counts[row["source_job_id"]] = {
                "total": int(row["total"]),
                "published": int(row["published"] or 0),
            }
        return counts
