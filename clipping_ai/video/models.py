"""Pydantic models for video metadata and persistent jobs."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from clipping_ai.highlights.models import HighlightResult
from clipping_ai.schemas import TranscriptResult


class SourceType(StrEnum):
    UPLOAD = "upload"
    URL = "url"


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStage(StrEnum):
    QUEUED = "queued"
    INGESTING = "ingesting"
    METADATA = "metadata"
    AUDIO = "audio"
    FINALIZING = "finalizing"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VideoStreamInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int
    codec_name: str | None = None
    codec_long_name: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bit_rate: int | None = None
    duration: float | None = None
    pix_fmt: str | None = None


class AudioStreamInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int
    codec_name: str | None = None
    codec_long_name: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    channel_layout: str | None = None
    bit_rate: int | None = None
    duration: float | None = None


class VideoMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    duration_seconds: float
    size_bytes: int
    format_name: str | None = None
    bit_rate: int | None = None
    has_video: bool
    has_audio: bool
    video_streams: list[VideoStreamInfo] = Field(default_factory=list)
    audio_streams: list[AudioStreamInfo] = Field(default_factory=list)


class AudioChunkInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int
    path: Path
    offset: float
    duration: float
    size_bytes: int


class VideoJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    source_type: SourceType
    source_value: str
    original_filename: str | None = None
    source_hash: str | None = None
    input_path: Path | None = None
    metadata: VideoMetadata | None = None
    audio_path: Path | None = None
    audio_chunks: list[AudioChunkInfo] = Field(default_factory=list)
    transcript: TranscriptResult | None = None
    transcript_error: str | None = None
    transcript_provider: str | None = None
    transcript_model: str | None = None
    transcript_at: str | None = None
    highlight_result: HighlightResult | None = None
    highlight_error: str | None = None
    highlight_at: str | None = None
    status: JobStatus = JobStatus.QUEUED
    stage: JobStage = JobStage.QUEUED
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    error: dict[str, Any] | None = None
    all_clips_published: bool = False
    all_clips_published_at: str | None = None
    deleted_at: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def public_dict(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={"input_path", "audio_path", "audio_chunks", "source_hash"},
        )

    def display_title(self) -> str:
        return self.original_filename or self.source_value or f"Source {self.id[:8]}"

    def status_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status.value,
            "stage": self.stage.value,
            "progress": self.progress,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "has_transcript": self.transcript is not None,
            "has_highlights": self.highlight_result is not None,
            "transcript_error": self.transcript_error,
            "highlight_error": self.highlight_error,
        }
