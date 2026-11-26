"""Models for media signals, highlight candidates, and final suggestions."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from clipping_ai.schemas import TranscriptSegment


class ContentType(StrEnum):
    AUTO = "auto"
    PODCAST = "podcast"
    EDUCATIONAL = "educational"
    GENERAL = "general"
    GAMING = "gaming"
    LIVE = "live"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class AudioWindow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    start: float = Field(ge=0)
    end: float = Field(ge=0)
    rms_dbfs: float
    peak_dbfs: float
    energy: float = Field(ge=0, le=1)
    silence: bool = False

    @model_validator(mode="after")
    def _end_after_start(self) -> AudioWindow:
        if self.end < self.start:
            raise ValueError("audio window end must be >= start")
        return self


class SceneChange(BaseModel):
    model_config = ConfigDict(extra="ignore")

    time: float = Field(ge=0)
    score: float = Field(ge=0, le=1)


class MotionSample(BaseModel):
    model_config = ConfigDict(extra="ignore")

    time: float = Field(ge=0)
    score: float = Field(ge=0, le=1)


class MediaClassification(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content_type: ContentType = ContentType.UNKNOWN
    confidence: float = Field(default=0.0, ge=0, le=1)
    scores: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    ambiguous: bool = False


class MediaSignals(BaseModel):
    model_config = ConfigDict(extra="ignore")

    duration: float = Field(ge=0)
    audio_windows: list[AudioWindow] = Field(default_factory=list)
    scene_changes: list[SceneChange] = Field(default_factory=list)
    motion_samples: list[MotionSample] = Field(default_factory=list)
    audio_analyzed: bool = False
    video_analyzed: bool = False
    motion_analyzed: bool = False
    notes: list[str] = Field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        energies = [window.energy for window in self.audio_windows]
        active = [window for window in self.audio_windows if not window.silence]
        motion_scores = [sample.score for sample in self.motion_samples]
        duration = self.duration if self.duration > 0 else 0.0
        return {
            "audio_analyzed": self.audio_analyzed,
            "video_analyzed": self.video_analyzed,
            "motion_analyzed": self.motion_analyzed,
            "audio_windows": len(self.audio_windows),
            "scene_changes": len(self.scene_changes),
            "motion_samples": len(self.motion_samples),
            "mean_energy": round(sum(energies) / len(energies), 4) if energies else None,
            "max_energy": round(max(energies), 4) if energies else None,
            "active_audio_ratio": (
                round(len(active) / len(self.audio_windows), 4) if self.audio_windows else None
            ),
            "silence_ratio": (
                round(1.0 - len(active) / len(self.audio_windows), 4)
                if self.audio_windows
                else None
            ),
            "scene_rate_per_minute": (
                round(len(self.scene_changes) / duration * 60.0, 4) if duration else None
            ),
            "mean_motion": (
                round(sum(motion_scores) / len(motion_scores), 4) if motion_scores else None
            ),
            "max_motion": round(max(motion_scores), 4) if motion_scores else None,
            "notes": list(self.notes),
        }


class Highlight(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    start_time: float = Field(ge=0, description="Clip start in seconds")
    end_time: float = Field(ge=0, description="Clip end in seconds")
    title: str
    description: str
    selection_reason: str
    confidence: float = Field(ge=0, le=1)
    category: str
    transcript_segments: list[TranscriptSegment] = Field(default_factory=list)
    signal_score: float | None = Field(default=None, ge=0, le=1)
    detection_signals: dict[str, float] = Field(default_factory=dict)
    source: str = "deterministic"

    @model_validator(mode="after")
    def _end_after_start(self) -> Highlight:
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be greater than start_time")
        return self


class HighlightResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    job_id: str
    content_type: ContentType
    language: str | None = None
    duration: float
    highlights: list[Highlight] = Field(default_factory=list)
    signals: dict[str, Any] = Field(default_factory=dict)
    classification: MediaClassification | None = None
    method: str = "deterministic"
    notes: list[str] = Field(default_factory=list)


class LlmOutcome(BaseModel):
    model_config = ConfigDict(extra="ignore")

    highlights: list[Highlight] = Field(default_factory=list)
    error: str | None = None
    response_data: dict[str, Any] | None = None
    provider: str | None = None
    model: str | None = None
    applied: bool = False
    failed: bool = False
    retryable: bool = False
    retry_after: float | None = None
