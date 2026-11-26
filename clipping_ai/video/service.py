"""Video ingestion, metadata, audio preparation, and job lifecycle."""

from __future__ import annotations

import builtins
import hashlib
import shutil
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from clipping_ai.audio import hash_file
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.recovery.idempotency import stable_fingerprint
from clipping_ai.recovery.stages import (
    StageError,
    StageOwnershipError,
    StageRecord,
    StageStatus,
    StageStore,
)
from clipping_ai.settings import Settings, get_settings
from clipping_ai.video.audio import build_chunk_manifest, extract_audio
from clipping_ai.video.config import VideoConfig, load_video_config
from clipping_ai.video.ingest import (
    YtDlpDownloader,
    copy_stream_to_file,
    extension_for,
    is_safe_job_id,
    sanitize_original_filename,
    validate_url,
)
from clipping_ai.video.metadata import probe_video
from clipping_ai.video.models import JobStage, JobStatus, SourceType, VideoJob, VideoMetadata
from clipping_ai.video.preparation import (
    STAGE_AUDIO,
    STAGE_CHUNKS,
    STAGE_PROBE,
    STAGE_SOURCE,
    AudioArtifact,
    SourceArtifact,
    audio_checkpoint,
    audio_fingerprint,
    audio_from_checkpoint,
    chunk_fingerprint,
    chunks_from_checkpoint,
    metadata_checkpoint,
    metadata_from_checkpoint,
    probe_fingerprint,
    source_checkpoint,
    source_fingerprint,
    source_sha256_from_checkpoint,
)
from clipping_ai.video.store import VideoJobStore


@dataclass(frozen=True)
class _StageResult:
    value: Any
    checkpoint: dict[str, Any]
    artifact: Path | None = None
    require_artifact: bool = False


def _now() -> str:
    return datetime.now(UTC).isoformat()


_STAGE_LEASE_SECONDS = 300.0
_STAGE_HEARTBEAT_SECONDS = 60.0


class VideoService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        config: VideoConfig | None = None,
        store: VideoJobStore | None = None,
        stage_store: StageStore | None = None,
        downloader: YtDlpDownloader | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self.config = config or load_video_config(self.settings.config_dir)
        self.store = store or VideoJobStore(self.settings.jobs_db_path)
        self.stage_store = stage_store or StageStore(self.settings.jobs_db_path)
        self.downloader = downloader or YtDlpDownloader(self.config)
        self.data_dir = self.settings.data_dir.resolve()
        self.jobs_root = self.data_dir / "videos" / "jobs"
        self.tmp_root = self.data_dir / "videos" / "tmp"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.tmp_root.mkdir(parents=True, exist_ok=True)

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Video processing is disabled in config/video.json.",
                retryable=False,
            )

    def job_dir(self, job_id: str) -> Path:
        if not is_safe_job_id(job_id):
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Invalid job ID.",
                retryable=False,
            )
        return self.jobs_root / job_id

    def _make_job_dir(self, job_id: str) -> Path:
        directory = self.job_dir(job_id)
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        return directory

    def _safe_data_path(self, path: Path | str) -> Path:
        resolved = Path(path).resolve()
        root = self.data_dir.resolve()
        if not resolved.is_relative_to(root):
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Job file path is outside the configured data directory.",
                retryable=False,
            )
        return resolved

    def create_upload(
        self,
        original_filename: str | None,
        stream: BinaryIO,
    ) -> tuple[VideoJob, bool]:
        self._require_enabled()
        filename = sanitize_original_filename(original_filename)
        extension = extension_for(filename, self.config)
        job_id = uuid4().hex
        directory = self._make_job_dir(job_id)
        part_path = directory / "input.part"
        try:
            _size, source_hash = copy_stream_to_file(
                stream,
                part_path,
                max_bytes=self.config.max_upload_bytes,
            )
            if self.config.duplicate_detection:
                existing = self.store.find_by_hash(source_hash)
                if existing is not None:
                    shutil.rmtree(directory, ignore_errors=True)
                    return existing, True
            input_path = directory / f"input{extension}"
            part_path.replace(input_path)
            job, duplicate = self.store.create_idempotent(
                job_id=job_id,
                idempotency_key=stable_fingerprint(
                    scope="video.upload", source_hash=source_hash
                ),
                source_type=SourceType.UPLOAD,
                source_value=filename,
                original_filename=filename,
                source_hash=source_hash,
                input_path=input_path,
                dedupe_hash=self.config.duplicate_detection,
            )
            if duplicate:
                shutil.rmtree(directory, ignore_errors=True)
            return job, duplicate
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def create_url(self, url: str) -> tuple[VideoJob, bool]:
        self._require_enabled()
        source_url = validate_url(url, self.config)
        if self.config.duplicate_detection:
            existing = self.store.find_by_source(SourceType.URL, source_url)
            if existing is not None:
                return existing, True
        job, duplicate = self.store.create_idempotent(
            job_id=uuid4().hex,
            idempotency_key=stable_fingerprint(
                scope="video.url",
                url_sha256=hashlib.sha256(source_url.encode("utf-8")).hexdigest(),
            ),
            source_type=SourceType.URL,
            source_value=source_url,
            dedupe_source=self.config.duplicate_detection,
        )
        return job, duplicate

    def prepare_local(self, path: Path | str) -> VideoJob:
        source = Path(path)
        if not source.is_file():
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Video file does not exist: {source}",
                retryable=False,
            )
        with source.open("rb") as fh:
            job, duplicate = self.create_upload(source.name, fh)
        if job.status == JobStatus.QUEUED:
            return self.process(job.id)
        return job

    def prepare_url(self, url: str) -> VideoJob:
        job, _duplicate = self.create_url(url)
        if job.status == JobStatus.QUEUED:
            return self.process(job.id)
        return job

    def process(self, job_id: str, *, owner: str | None = None) -> VideoJob:
        job = self.store.require(job_id)
        if job.status == JobStatus.READY:
            return job
        job = self.store.mark_processing(job_id)
        directory = self.job_dir(job_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stage_owner = owner or f"video-service-{uuid4().hex}"
        try:
            job, input_path = self._prepare_source(job, directory, stage_owner)
            metadata = self._prepare_probe(job, input_path, stage_owner)
            audio = self._prepare_audio(job, input_path, directory, stage_owner)
            self._prepare_chunks(job, audio, directory, metadata, stage_owner)
            return self.store.mark_ready(job_id)
        except AIError as exc:
            self.store.fail(job_id, exc.to_dict())
            raise
        except Exception as exc:
            error = AIError(
                ErrorCategory.INTERNAL_ERROR,
                f"Video preparation failed: {exc}",
                retryable=isinstance(exc, OSError),
                cause=exc,
            )
            self.store.fail(job_id, error.to_dict())
            raise error from exc
        finally:
            if self.config.cleanup_temp:
                shutil.rmtree(directory / "tmp", ignore_errors=True)

    def _prepare_source(
        self, job: VideoJob, directory: Path, owner: str
    ) -> tuple[VideoJob, Path]:
        fingerprint = source_fingerprint(job)
        record = self.stage_store.ensure(
            job_type="video",
            job_id=job.id,
            stage=STAGE_SOURCE,
            input_fingerprint=fingerprint,
        )
        if record.status is StageStatus.DONE and record.input_fingerprint == fingerprint:
            artifact = self._valid_source(job)
            checkpoint_hash = source_sha256_from_checkpoint(record.checkpoint)
            if (
                artifact is not None
                and (job.source_hash is None or job.source_hash == artifact.sha256)
                and record.artifact_path == artifact.path
                and record.artifact_sha256 == artifact.sha256
                and checkpoint_hash == artifact.sha256
            ):
                updated = self._persist_source_artifact(job, artifact)
                return updated, artifact.path

        def work(_active: StageRecord) -> _StageResult:
            artifact = self._valid_source(job)
            if artifact is None and job.source_type == SourceType.UPLOAD:
                raise AIError(
                    ErrorCategory.NOT_FOUND,
                    "The uploaded source video is missing or no longer matches its "
                    "recorded fingerprint. Re-upload the file; it cannot be "
                    "reconstructed automatically.",
                    retryable=False,
                    details={"reason": "unrecoverable_source"},
                )
            if artifact is None:
                self.store.update(
                    job.id,
                    stage=JobStage.INGESTING,
                    progress=0.05,
                )
                updated, artifact = self._download_source(job, directory)
            else:
                updated = self._persist_source_artifact(job, artifact)
            checkpoint = source_checkpoint(
                source_type=job.source_type.value,
                path=artifact.path,
                source_sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
            )
            return _StageResult(
                value=(updated, artifact.path),
                checkpoint=checkpoint,
                artifact=artifact.path,
                require_artifact=True,
            )

        return self._run_stage(
            job=job,
            stage=STAGE_SOURCE,
            fingerprint=fingerprint,
            owner=owner,
            work=work,
            clear_checkpoint_on_done=False,
        )

    def _valid_source(self, job: VideoJob) -> SourceArtifact | None:
        if job.input_path is None:
            return None
        path = self._safe_data_path(job.input_path)
        try:
            if not path.is_file():
                return None
            size = path.stat().st_size
            if size <= 0:
                return None
            source_hash = hash_file(path)
        except OSError:
            return None
        if job.source_hash is not None and source_hash != job.source_hash:
            return None
        return SourceArtifact(path=path, sha256=source_hash, size_bytes=size)

    def _persist_source_artifact(self, job: VideoJob, artifact: SourceArtifact) -> VideoJob:
        current_path = job.input_path.resolve() if job.input_path is not None else None
        if job.source_hash == artifact.sha256 and current_path == artifact.path:
            return job
        return self.store.update(
            job.id,
            input_path=artifact.path,
            source_hash=artifact.sha256,
        )

    def _download_source(
        self, job: VideoJob, directory: Path
    ) -> tuple[VideoJob, SourceArtifact]:
        tmp_dir = directory / "tmp"
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            downloaded = self.downloader.download(job.source_value, tmp_dir)
            path = self._safe_data_path(downloaded.path)
            if not path.is_file() or path.stat().st_size == 0:
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    "The downloaded source file is empty or missing.",
                    retryable=False,
                )
            extension = path.suffix.lower()
            if not self.config.allows_extension(extension):
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    f"Downloaded format '{extension}' is not supported.",
                    retryable=False,
                )
            if path.stat().st_size > self.config.max_upload_bytes:
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    "Downloaded file exceeds the maximum supported size.",
                    retryable=False,
                )
            size = path.stat().st_size
            source_hash = hash_file(path)
            destination = (directory / f"input{extension}").resolve()
            old_path = job.input_path.resolve() if job.input_path is not None else None
            path.replace(destination)
            updated = self.store.update(
                job.id,
                input_path=destination,
                source_hash=source_hash,
                original_filename=downloaded.title,
                progress=0.2,
            )
            if old_path is not None and old_path != destination:
                with suppress(OSError):
                    old_path.unlink(missing_ok=True)
            return updated, SourceArtifact(
                path=destination, sha256=source_hash, size_bytes=size
            )
        finally:
            if self.config.cleanup_temp:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def _prepare_probe(
        self, job: VideoJob, input_path: Path, owner: str
    ) -> VideoMetadata:
        source_hash = job.source_hash
        if source_hash is None:
            raise AIError(
                ErrorCategory.INTERNAL_ERROR,
                "The source fingerprint is unavailable after acquisition.",
                retryable=False,
            )
        fingerprint = probe_fingerprint(source_hash, self.config)
        record = self.stage_store.ensure(
            job_type="video",
            job_id=job.id,
            stage=STAGE_PROBE,
            input_fingerprint=fingerprint,
        )
        if record.status is StageStatus.DONE and record.input_fingerprint == fingerprint:
            metadata = metadata_from_checkpoint(
                record.checkpoint, source_sha256=source_hash
            )
            if metadata is not None:
                self.store.update(
                    job.id,
                    stage=JobStage.METADATA,
                    metadata=metadata,
                    progress=0.4,
                )
                return metadata

        self.store.update(job.id, stage=JobStage.METADATA, progress=0.25)

        def work(active: StageRecord) -> _StageResult:
            metadata = metadata_from_checkpoint(
                active.checkpoint, source_sha256=source_hash
            )
            if metadata is None:
                metadata = probe_video(input_path, self.config)
                checkpoint = metadata_checkpoint(metadata, source_sha256=source_hash)
            else:
                checkpoint = active.checkpoint
            return _StageResult(value=metadata, checkpoint=checkpoint)

        metadata = self._run_stage(
            job=job,
            stage=STAGE_PROBE,
            fingerprint=fingerprint,
            owner=owner,
            work=work,
            clear_checkpoint_on_done=True,
        )
        self.store.update(job.id, metadata=metadata, progress=0.4)
        return metadata

    def _prepare_audio(
        self, job: VideoJob, input_path: Path, directory: Path, owner: str
    ) -> AudioArtifact:
        source_hash = job.source_hash
        if source_hash is None:
            raise AIError(
                ErrorCategory.INTERNAL_ERROR,
                "The source fingerprint is unavailable after acquisition.",
                retryable=False,
            )
        fingerprint = audio_fingerprint(source_hash, self.config)
        audio_dir = directory / "audio"
        audio_path = audio_dir / f"audio{self.config.audio_extension}"
        record = self.stage_store.ensure(
            job_type="video",
            job_id=job.id,
            stage=STAGE_AUDIO,
            input_fingerprint=fingerprint,
        )
        if record.status is StageStatus.DONE and record.input_fingerprint == fingerprint:
            artifact = audio_from_checkpoint(
                record.checkpoint,
                expected_path=audio_path,
                source_sha256=source_hash,
                config=self.config,
            )
            if artifact is not None and (
                record.artifact_sha256 is None
                or record.artifact_sha256 == artifact.sha256
            ):
                self.store.update(
                    job.id,
                    stage=JobStage.AUDIO,
                    audio_path=artifact.path,
                    progress=0.65,
                )
                return artifact

        self.store.update(job.id, stage=JobStage.AUDIO, progress=0.5)

        def work(active: StageRecord) -> _StageResult:
            existing = audio_from_checkpoint(
                active.checkpoint,
                expected_path=audio_path,
                source_sha256=source_hash,
                config=self.config,
            )
            if existing is not None:
                return _StageResult(
                    value=existing,
                    checkpoint=active.checkpoint,
                    artifact=existing.path,
                    require_artifact=True,
                )
            audio_dir.mkdir(parents=True, exist_ok=True)
            temp_dir = directory / "tmp"
            temp_dir.mkdir(parents=True, exist_ok=True)
            temp_path = temp_dir / f"audio-{uuid4().hex}{self.config.audio_extension}"
            try:
                extract_audio(input_path, temp_path, self.config)
                size = temp_path.stat().st_size
                if size <= 0:
                    raise AIError(
                        ErrorCategory.INVALID_REQUEST,
                        "Audio extraction produced an empty file.",
                        retryable=False,
                    )
                artifact_hash = hash_file(temp_path)
                temp_path.replace(audio_path)
            finally:
                temp_path.unlink(missing_ok=True)
            artifact = AudioArtifact(
                path=audio_path,
                sha256=artifact_hash,
                size_bytes=size,
            )
            return _StageResult(
                value=artifact,
                checkpoint=audio_checkpoint(
                    artifact, source_sha256=source_hash, config=self.config
                ),
                artifact=artifact.path,
                require_artifact=True,
            )

        artifact = self._run_stage(
            job=job,
            stage=STAGE_AUDIO,
            fingerprint=fingerprint,
            owner=owner,
            work=work,
            clear_checkpoint_on_done=True,
        )
        self.store.update(job.id, audio_path=artifact.path, progress=0.65)
        return artifact

    def _prepare_chunks(
        self,
        job: VideoJob,
        audio: AudioArtifact,
        directory: Path,
        metadata: VideoMetadata,
        owner: str,
    ) -> None:
        duration = metadata.duration_seconds
        fingerprint = chunk_fingerprint(audio.sha256, duration, self.config)
        chunks_dir = directory / "audio" / "chunks"
        record = self.stage_store.ensure(
            job_type="video",
            job_id=job.id,
            stage=STAGE_CHUNKS,
            input_fingerprint=fingerprint,
        )
        if record.status is StageStatus.DONE and record.input_fingerprint == fingerprint:
            chunks = chunks_from_checkpoint(
                record.checkpoint,
                output_dir=chunks_dir,
                audio_sha256=audio.sha256,
                duration=duration,
                config=self.config,
            )
            if chunks is not None:
                self.store.update(
                    job.id,
                    audio_chunks=chunks,
                    stage=JobStage.FINALIZING,
                    progress=0.9,
                )
                return

        def checkpoint(partial: dict[str, Any]) -> None:
            self.stage_store.save_checkpoint(
                job_type="video",
                job_id=job.id,
                stage=STAGE_CHUNKS,
                owner=owner,
                checkpoint=partial,
            )

        def work(active: StageRecord) -> _StageResult:
            existing = active.checkpoint if isinstance(active.checkpoint, dict) else None
            chunks, manifest = build_chunk_manifest(
                audio.path,
                chunks_dir,
                self.config,
                duration=duration,
                audio_sha256=audio.sha256,
                existing_manifest=existing,
                on_chunk=checkpoint,
            )
            return _StageResult(value=chunks, checkpoint=manifest)

        chunks = self._run_stage(
            job=job,
            stage=STAGE_CHUNKS,
            fingerprint=fingerprint,
            owner=owner,
            work=work,
            clear_checkpoint_on_done=False,
        )
        self.store.update(
            job.id,
            audio_chunks=chunks,
            stage=JobStage.FINALIZING,
            progress=0.9,
        )

    def _run_stage(
        self,
        *,
        job: VideoJob,
        stage: str,
        fingerprint: str,
        owner: str,
        work: Callable[[StageRecord], _StageResult],
        clear_checkpoint_on_done: bool,
    ) -> Any:
        active = self._claim_stage(
            job=job,
            stage=stage,
            fingerprint=fingerprint,
            owner=owner,
            clear_checkpoint_on_done=clear_checkpoint_on_done,
        )
        stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(active, owner, stop),
            name=f"clipping-stage-{stage}",
            daemon=True,
        )
        heartbeat.start()
        try:
            result = work(active)
            self.stage_store.save_checkpoint(
                job_type="video",
                job_id=job.id,
                stage=stage,
                owner=owner,
                checkpoint=result.checkpoint,
            )
            self.stage_store.complete(
                job_type="video",
                job_id=job.id,
                stage=stage,
                owner=owner,
                checkpoint=result.checkpoint,
                artifact_path=result.artifact,
                require_artifact=result.require_artifact,
            )
            return result.value
        except StageOwnershipError:
            raise
        except AIError as exc:
            self._fail_stage(
                job=job, stage=stage, owner=owner, error=exc.to_dict(), retryable=exc.retryable
            )
            raise
        except StageError as exc:
            error = AIError(
                ErrorCategory.INTERNAL_ERROR,
                str(exc),
                retryable=True,
                cause=exc,
            )
            self._fail_stage(
                job=job,
                stage=stage,
                owner=owner,
                error=error.to_dict(),
                retryable=True,
            )
            raise error from exc
        except Exception as exc:
            retryable = isinstance(exc, OSError)
            error = AIError(
                ErrorCategory.INTERNAL_ERROR,
                f"Video preparation stage '{stage}' failed: {exc}",
                retryable=retryable,
                cause=exc,
            )
            self._fail_stage(
                job=job,
                stage=stage,
                owner=owner,
                error=error.to_dict(),
                retryable=retryable,
            )
            raise error from exc
        finally:
            stop.set()
            heartbeat.join(timeout=2.0)

    def _claim_stage(
        self,
        *,
        job: VideoJob,
        stage: str,
        fingerprint: str,
        owner: str,
        clear_checkpoint_on_done: bool,
    ) -> StageRecord:
        record = self.stage_store.ensure(
            job_type="video",
            job_id=job.id,
            stage=stage,
            input_fingerprint=fingerprint,
        )
        if record.input_fingerprint != fingerprint:
            self._reset_stage(
                job=job, stage=stage, fingerprint=fingerprint, clear_checkpoint=True
            )
        elif record.status is StageStatus.DONE:
            self._reset_stage(
                job=job,
                stage=stage,
                fingerprint=fingerprint,
                clear_checkpoint=clear_checkpoint_on_done,
            )
        elif record.status is StageStatus.FAILED:
            self._reset_stage(
                job=job, stage=stage, fingerprint=fingerprint, clear_checkpoint=False
            )
        claimed = self.stage_store.claim_next(
            owner=owner,
            lease_seconds=_STAGE_LEASE_SECONDS,
            job_type="video",
            job_id=job.id,
            stage=stage,
        )
        if claimed is None:
            raise StageOwnershipError(f"Stage '{stage}' is owned by another worker.")
        return claimed

    def _reset_stage(
        self, *, job: VideoJob, stage: str, fingerprint: str, clear_checkpoint: bool
    ) -> None:
        self.stage_store.reset(
            job_type="video",
            job_id=job.id,
            stage=stage,
            input_fingerprint=fingerprint,
            clear_checkpoint=clear_checkpoint,
        )

    def _heartbeat_loop(
        self, active: StageRecord, owner: str, stop: threading.Event
    ) -> None:
        while not stop.wait(_STAGE_HEARTBEAT_SECONDS):
            try:
                renewed = self.stage_store.heartbeat(
                    job_type=active.job_type,
                    job_id=active.job_id,
                    stage=active.stage,
                    unit_key=active.unit_key,
                    owner=owner,
                    lease_seconds=_STAGE_LEASE_SECONDS,
                )
            except Exception:
                return
            if not renewed:
                return

    def _fail_stage(
        self,
        *,
        job: VideoJob,
        stage: str,
        owner: str,
        error: dict[str, Any],
        retryable: bool,
    ) -> None:
        with suppress(StageOwnershipError):
            self.stage_store.fail(
                job_type="video",
                job_id=job.id,
                stage=stage,
                owner=owner,
                error=error,
                retryable=retryable,
            )

    def inspect(self, path: Path | str) -> VideoMetadata:
        self._require_enabled()
        return probe_video(Path(path), self.config)

    def get(self, job_id: str) -> VideoJob:
        return self.store.require(job_id)

    def source_media_path(self, job_id: str) -> tuple[VideoJob, Path]:
        job = self.store.require(job_id)
        if job.deleted_at is not None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Restore this source video before previewing it.",
                retryable=False,
            )
        if job.status != JobStatus.READY:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Source video is not ready for preview.",
                retryable=False,
            )
        if job.input_path is None:
            raise AIError(
                ErrorCategory.NOT_FOUND,
                "Source media is not available.",
                retryable=False,
            )
        if job.metadata is not None and not job.metadata.has_video:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Source media has no video stream.",
                retryable=False,
            )
        path = self._safe_data_path(job.input_path)
        if not path.is_file():
            raise AIError(
                ErrorCategory.NOT_FOUND,
                "Source media file is missing.",
                retryable=False,
            )
        return job, path

    def status(self, job_id: str) -> dict[str, object]:
        return self.store.require(job_id).status_dict()

    def list(
        self,
        *,
        limit: int = 50,
        status: JobStatus | None = None,
        deleted: bool = False,
    ) -> list[VideoJob]:
        return self.store.list(limit=limit, status=status, deleted=deleted)

    def list_deleted(self, *, limit: int = 500) -> builtins.list[VideoJob]:
        return self.store.list_deleted(limit=limit)

    def display_title(self, job: VideoJob) -> str:
        return job.display_title()

    def set_all_clips_published(self, job_id: str, published: bool) -> VideoJob:
        job = self.store.require(job_id)
        if job.deleted_at is not None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Restore this source video before changing publishing status.",
                retryable=False,
            )
        if published == job.all_clips_published:
            return job
        return self.store.update(
            job_id,
            all_clips_published=published,
            all_clips_published_at=_now() if published else None,
        )

    def soft_delete(self, job_id: str) -> VideoJob:
        job = self.store.require(job_id)
        if job.deleted_at is not None:
            return job
        return self.store.update(job_id, deleted_at=_now())

    def restore(self, job_id: str) -> VideoJob:
        job = self.store.require(job_id)
        if job.deleted_at is None:
            return job
        return self.store.update(job_id, deleted_at=None)

    def permanently_delete(self, job_id: str) -> None:
        job = self.store.require(job_id)
        if job.deleted_at is None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Move this source video to trash before deleting it permanently.",
                retryable=False,
            )
        if job.status in {JobStatus.QUEUED, JobStatus.PROCESSING}:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Wait for the source job to stop before deleting it permanently.",
                retryable=False,
            )
        directory = self.job_dir(job_id).resolve()
        if not directory.is_relative_to(self.jobs_root.resolve()):
            raise AIError(
                ErrorCategory.INTERNAL_ERROR,
                "Refusing to delete a source outside its owned directory.",
                retryable=False,
            )
        for path in (job.input_path, job.audio_path):
            if path is None:
                continue
            resolved = Path(path).resolve()
            if not resolved.is_relative_to(directory):
                raise AIError(
                    ErrorCategory.INTERNAL_ERROR,
                    "Refusing to delete a source file outside its owned directory.",
                    retryable=False,
                )
        if directory.exists():
            shutil.rmtree(directory)
        self.store.delete_row(job_id)

    def retry(self, job_id: str) -> VideoJob:
        job = self.store.require(job_id)
        if job.deleted_at is not None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Restore this source video before retrying it.",
                retryable=False,
            )
        return self.store.retry(job_id)

    def cancel(self, job_id: str) -> VideoJob:
        job = self.store.require(job_id)
        if job.deleted_at is not None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Restore this source video before cancelling it.",
                retryable=False,
            )
        return self.store.cancel(job_id)

    def recover(self) -> int:
        self.stage_store.release_running(job_type="video")
        return self.store.recover_interrupted()

    def config_dict(self) -> dict[str, object]:
        return {
            "enabled": self.config.enabled,
            "max_upload_bytes": self.config.max_upload_bytes,
            "max_duration_seconds": self.config.max_duration_seconds,
            "allowed_extensions": self.config.allowed_extensions,
            "audio_codec": self.config.audio_codec,
            "audio_sample_rate": self.config.audio_sample_rate,
            "audio_channels": self.config.audio_channels,
            "chunk_seconds": self.config.chunk_seconds,
            "chunk_overlap_seconds": self.config.chunk_overlap_seconds,
            "duplicate_detection": self.config.duplicate_detection,
        }
