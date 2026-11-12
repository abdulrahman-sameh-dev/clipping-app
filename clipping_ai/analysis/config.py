"""Configuration for transcription orchestration and highlight detection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from clipping_ai.errors import AIError, ErrorCategory


class TranscriptionSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    default_language: str | None = None
    word_timestamps: bool = True
    clamp_timestamps: bool = True
    max_transcript_chars: int = 200_000
    min_word_probability: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _sane_values(self) -> TranscriptionSettings:
        if self.max_transcript_chars <= 0:
            raise ValueError("max_transcript_chars must be positive")
        return self


class HighlightSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    min_clip_seconds: float = Field(default=12.0, gt=0)
    max_clip_seconds: float = Field(default=180.0, gt=0)
    max_highlights: int = Field(default=10, ge=1, le=50)
    candidate_window_seconds: float = Field(default=60.0, gt=0)
    candidate_stride_seconds: float = Field(default=30.0, gt=0)
    min_confidence: float = Field(default=0.25, ge=0, le=1)
    audio_window_seconds: float = Field(default=0.5, gt=0)
    audio_timeout_seconds: float = Field(default=300.0, gt=0)
    audio_event_min_energy: float = Field(default=0.2, ge=0, le=1)
    audio_peak_delta: float = Field(default=0.12, ge=0, le=1)
    scene_timeout_seconds: float = Field(default=300.0, gt=0)
    scene_threshold: float = Field(default=0.25, ge=0, le=1)
    analyze_motion: bool = True
    motion_fps: float = Field(default=2.0, gt=0, le=10)
    motion_scale_width: int = Field(default=160, ge=64, le=640)
    motion_score_scale: float = Field(default=24.0, gt=0)
    motion_event_threshold: float = Field(default=0.18, ge=0, le=1)
    motion_timeout_seconds: float = Field(default=300.0, gt=0)
    silence_dbfs: float = -45.0
    spike_percentile: float = Field(default=85.0, ge=0, le=100)
    event_merge_seconds: float = Field(default=8.0, gt=0)
    pre_roll_seconds: float = Field(default=2.0, ge=0)
    post_roll_seconds: float = Field(default=3.0, ge=0)
    align_to_transcript: bool = True
    signal_base_score: float = Field(default=0.2, ge=0, le=1)
    audio_weight: float = Field(default=0.22, ge=0, le=1)
    peak_weight: float = Field(default=0.12, ge=0, le=1)
    scene_weight: float = Field(default=0.14, ge=0, le=1)
    motion_weight: float = Field(default=0.18, ge=0, le=1)
    silence_transition_weight: float = Field(default=0.08, ge=0, le=1)
    transcript_signal_weight: float = Field(default=0.25, ge=0, le=1)
    content_type_signal_penalty: float = Field(default=0.15, ge=0, le=1)
    dedupe_overlap_ratio: float = Field(default=0.55, ge=0, le=1)
    semantic_consolidation: bool = True
    semantic_gap_seconds: float = Field(default=2.0, gt=0)
    semantic_merge_gap_seconds: float = Field(default=3.0, ge=0)
    semantic_bridge_gap_seconds: float = Field(default=20.0, ge=0)
    semantic_topic_overlap: float = Field(default=0.2, ge=0, le=1)
    semantic_boundary_context_seconds: float = Field(default=1.0, ge=0)
    semantic_completeness_bonus: float = Field(default=0.05, ge=0, le=1)
    classification_min_confidence: float = Field(default=0.35, ge=0, le=1)
    classification_margin: float = Field(default=0.12, ge=0, le=1)
    use_llm: bool = True
    llm_max_transcript_chars: int = Field(default=24_000, gt=0)
    llm_max_candidates: int = Field(default=12, ge=1, le=50)

    @model_validator(mode="after")
    def _sane_ranges(self) -> HighlightSettings:
        if self.min_clip_seconds > self.max_clip_seconds:
            raise ValueError("min_clip_seconds must be <= max_clip_seconds")
        if self.candidate_stride_seconds > self.candidate_window_seconds:
            raise ValueError("candidate_stride_seconds must be <= candidate_window_seconds")
        if self.semantic_merge_gap_seconds > self.semantic_bridge_gap_seconds:
            raise ValueError("semantic_merge_gap_seconds must be <= semantic_bridge_gap_seconds")
        return self


class AnalysisConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    transcription: TranscriptionSettings = Field(default_factory=TranscriptionSettings)
    highlights: HighlightSettings = Field(default_factory=HighlightSettings)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            f"Invalid JSON in {path}: {exc}",
            retryable=False,
        ) from exc
    if not isinstance(data, dict):
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            f"Expected a JSON object in {path}",
            retryable=False,
        )
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_analysis_config(config_dir: Path | str) -> AnalysisConfig:
    config_dir = Path(config_dir)
    raw = _deep_merge(
        _read_json(config_dir / "analysis.json"),
        _read_json(config_dir / "local" / "analysis.json"),
    )
    try:
        return AnalysisConfig.model_validate(raw)
    except ValidationError as exc:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            f"Invalid analysis configuration: {exc}",
            retryable=False,
        ) from exc
