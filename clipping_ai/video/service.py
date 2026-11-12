"""Video ingestion, metadata, audio preparation, and job lifecycle."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from clipping_ai.audio import hash_file
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.settings import Settings, get_settings
from clipping_ai.video.audio import extract_audio, split_audio_file
from clipping_ai.video.config import VideoConfig, load_video_config
from clipping_ai.video.ingest import (
    DownloadedSource,
    YtDlpDownloader,
    copy_stream_to_file,
    extension_for,
    is_safe_job_id,
    sanitize_original_filename,
    validate_url,
)
from clipping_ai.video.metadata import probe_video
from clipping_ai.video.models import JobStatus, SourceType, VideoJob, VideoMetadata
from clipping_ai.video.store import VideoJobStore


class VideoService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        config: VideoConfig | None = None,
        store: VideoJobStore | None = None,
        downloader: YtDlpDownloader | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self.config = config or load_video_config(self.settings.config_dir)
        self.store = store or VideoJobStore(self.settings.jobs_db_path)
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
            job = self.store.create(
                job_id=job_id,
                source_type=SourceType.UPLOAD,
                source_value=filename,
                original_filename=filename,
                source_hash=source_hash,
                input_path=input_path,
            )
            return job, False
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
        job = self.store.create(
            job_id=uuid4().hex,
            source_type=SourceType.URL,
            source_value=source_url,
        )
        return job, False

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

    def process(self, job_id: str) -> VideoJob:
        job = self.store.require(job_id)
        if job.status == JobStatus.READY:
            return job
        job = self.store.mark_processing(job_id)
        directory = self.job_dir(job_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            if job.source_type == SourceType.URL and job.input_path is None:
                job = self._ingest_url(job, directory)
            if job.input_path is None:
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    "The job has no input file.",
                    retryable=False,
                )
            input_path = self._safe_data_path(job.input_path)
            if not input_path.is_file():
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    "The job input file is missing.",
                    retryable=False,
                )

            self.store.update(
                job_id,
                stage="metadata",
                progress=0.25,
            )
            metadata = probe_video(input_path, self.config)
            self.store.update(
                job_id,
                metadata=metadata,
                progress=0.4,
            )

            audio_dir = directory / "audio"
            chunks_dir = audio_dir / "chunks"
            shutil.rmtree(audio_dir, ignore_errors=True)
            audio_path = audio_dir / f"audio{self.config.audio_extension}"
            self.store.update(job_id, stage="audio", progress=0.5)
            extract_audio(input_path, audio_path, self.config)
            self.store.update(job_id, audio_path=audio_path, progress=0.65)
            chunks = split_audio_file(
                audio_path,
                chunks_dir,
                self.config,
                duration=metadata.duration_seconds,
            )
            self.store.update(
                job_id,
                audio_chunks=chunks,
                stage="finalizing",
                progress=0.9,
            )
            return self.store.mark_ready(job_id)
        except AIError as exc:
            self.store.fail(job_id, exc.to_dict())
            raise
        except Exception as exc:
            error = AIError(
                ErrorCategory.INTERNAL_ERROR,
                f"Video preparation failed: {exc}",
                retryable=False,
                cause=exc,
            )
            self.store.fail(job_id, error.to_dict())
            raise error from exc
        finally:
            if self.config.cleanup_temp:
                shutil.rmtree(directory / "tmp", ignore_errors=True)

    def _ingest_url(self, job: VideoJob, directory: Path) -> VideoJob:
        self.store.update(
            job.id,
            stage="ingesting",
            progress=0.05,
        )
        tmp_dir = directory / "tmp"
        shutil.rmtree(tmp_dir, ignore_errors=True)
        downloaded: DownloadedSource = self.downloader.download(job.source_value, tmp_dir)
        extension = downloaded.path.suffix.lower()
        if not self.config.allows_extension(extension):
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Downloaded format '{extension}' is not supported.",
                retryable=False,
            )
        input_path = directory / f"input{extension}"
        downloaded.path.replace(input_path)
        source_hash = hash_file(input_path)
        return self.store.update(
            job.id,
            input_path=input_path,
            source_hash=source_hash,
            original_filename=downloaded.title,
            progress=0.2,
        )

    def inspect(self, path: Path | str) -> VideoMetadata:
        self._require_enabled()
        return probe_video(Path(path), self.config)

    def get(self, job_id: str) -> VideoJob:
        return self.store.require(job_id)

    def status(self, job_id: str) -> dict[str, object]:
        return self.store.require(job_id).status_dict()

    def list(self, *, limit: int = 50, status: JobStatus | None = None) -> list[VideoJob]:
        return self.store.list(limit=limit, status=status)

    def retry(self, job_id: str) -> VideoJob:
        return self.store.retry(job_id)

    def cancel(self, job_id: str) -> VideoJob:
        return self.store.cancel(job_id)

    def recover(self) -> int:
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
