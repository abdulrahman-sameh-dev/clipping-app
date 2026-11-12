"""Models for source-video clip rendering jobs."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ClipFormat(StrEnum):
    VERTICAL = "vertical"
    SQUARE = "square"
    LANDSCAPE = "landscape"


class ClipQuality(StrEnum):
    DRAFT = "draft"
    STANDARD = "standard"
    HIGH = "high"


class ClipFit(StrEnum):
    CROP = "crop"
    PAD = "pad"
    BLUR = "blur"


class ClipStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ClipStage(StrEnum):
    QUEUED = "queued"
    CAPTIONS = "captions"
    RENDERING = "rendering"
    VERIFYING = "verifying"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ClipJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    source_job_id: str
    highlight_index: int | None = None
    start_time: float = Field(ge=0)
    end_time: float = Field(gt=0)
    duration: float = Field(gt=0)
    format: ClipFormat
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0)
    quality: ClipQuality
    fit: ClipFit
    captions: bool = False
    caption_language: str | None = None
    caption_path: Path | None = None
    output_path: Path | None = None
    size_bytes: int | None = None
    status: ClipStatus = ClipStatus.QUEUED
    stage: ClipStage = ClipStage.QUEUED
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    error: dict[str, Any] | None = None
    cancel_requested: bool = False
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _derive_duration(cls, data: Any) -> Any:
        if isinstance(data, dict) and "duration" not in data:
            start = data.get("start_time")
            end = data.get("end_time")
            if start is not None and end is not None:
                data = dict(data)
                data["duration"] = float(end) - float(start)
        return data

    @model_validator(mode="after")
    def _validate_range(self) -> ClipJob:
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be greater than start_time")
        if abs(self.duration - (self.end_time - self.start_time)) > 1e-6:
            raise ValueError("duration must equal end_time - start_time")
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def status_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_job_id": self.source_job_id,
            "highlight_index": self.highlight_index,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration": self.duration,
            "format": self.format.value,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "quality": self.quality.value,
            "fit": self.fit.value,
            "captions": self.captions,
            "caption_language": self.caption_language,
            "status": self.status.value,
            "stage": self.stage.value,
            "progress": self.progress,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }
