"""Configuration for local, FFmpeg-based clip rendering."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from clipping_ai.clips.models import ClipFit, ClipFormat, ClipQuality
from clipping_ai.errors import AIError, ErrorCategory

_AUDIO_BITRATE_RE = re.compile(r"^[1-9][0-9]*k$")


class ClipFormatProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    width: int = Field(gt=0, le=4096)
    height: int = Field(gt=0, le=4096)

    @model_validator(mode="after")
    def _even_dimensions(self) -> ClipFormatProfile:
        if self.width % 2 or self.height % 2:
            raise ValueError("clip dimensions must be even for yuv420p output")
        return self


class ClipQualityProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    crf: int = Field(ge=0, le=51)
    preset: str = "medium"
    audio_bitrate: str = "192k"

    @field_validator("preset")
    @classmethod
    def _normalise_preset(cls, value: str) -> str:
        preset = value.strip().lower()
        if not preset:
            raise ValueError("preset cannot be empty")
        return preset

    @field_validator("audio_bitrate")
    @classmethod
    def _normalise_bitrate(cls, value: str) -> str:
        bitrate = value.strip().lower()
        if not _AUDIO_BITRATE_RE.fullmatch(bitrate):
            raise ValueError("audio_bitrate must look like '128k' or '192k'")
        return bitrate


def _default_formats() -> dict[str, ClipFormatProfile]:
    return {
        ClipFormat.VERTICAL.value: ClipFormatProfile(width=1080, height=1920),
        ClipFormat.SQUARE.value: ClipFormatProfile(width=1080, height=1080),
        ClipFormat.LANDSCAPE.value: ClipFormatProfile(width=1920, height=1080),
    }


def _default_qualities() -> dict[str, ClipQualityProfile]:
    return {
        ClipQuality.DRAFT.value: ClipQualityProfile(
            crf=28, preset="veryfast", audio_bitrate="128k"
        ),
        ClipQuality.STANDARD.value: ClipQualityProfile(
            crf=23, preset="medium", audio_bitrate="192k"
        ),
        ClipQuality.HIGH.value: ClipQualityProfile(
            crf=18, preset="slow", audio_bitrate="256k"
        ),
    }


class ClipConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    ffmpeg_binary: str = "ffmpeg"
    ffprobe_binary: str = "ffprobe"
    formats: dict[str, ClipFormatProfile] = Field(default_factory=_default_formats)
    qualities: dict[str, ClipQualityProfile] = Field(default_factory=_default_qualities)
    default_formats: list[ClipFormat] = Field(default_factory=lambda: [ClipFormat.VERTICAL])
    default_quality: ClipQuality = ClipQuality.STANDARD
    default_fit: ClipFit = ClipFit.CROP
    default_fps: float = Field(default=30.0, gt=0, le=120)
    min_clip_seconds: float = Field(default=1.0, gt=0)
    max_clip_seconds: float = Field(default=300.0, gt=0)
    render_timeout_seconds: float = Field(default=1800.0, gt=0)
    ffprobe_timeout_seconds: float = Field(default=60.0, gt=0)
    min_free_disk_bytes: int = Field(default=1_000_000_000, ge=0)
    x264_threads: int = Field(default=4, ge=1, le=64)
    pixel_format: str = "yuv420p"
    audio_codec: str = "aac"
    audio_channels: int | None = Field(default=None, gt=0)
    audio_sample_rate: int | None = Field(default=None, gt=0)
    faststart: bool = True
    keep_failed_artifacts: bool = False
    worker_poll_seconds: float = Field(default=1.0, gt=0)
    blur_sigma: float = Field(default=20.0, gt=0)
    blur_steps: int = Field(default=2, ge=1, le=10)
    caption_font_arabic: str = "Noto Sans Arabic"
    caption_font_latin: str = "Noto Sans"
    caption_font_size: int | None = Field(default=None, gt=0)
    caption_font_size_ratio: float = Field(default=0.062, gt=0, le=0.2)
    caption_margin_v_ratio: float = Field(default=0.055, ge=0, le=0.3)
    caption_outline: float = Field(default=3.0, ge=0, le=20)
    caption_shadow: float = Field(default=1.0, ge=0, le=20)
    caption_border_style: int = Field(default=3, ge=1, le=4)
    caption_box_padding_ratio: float = Field(default=0.008, ge=0, le=0.05)
    caption_encoding: int = Field(default=-1, ge=-1, le=255)
    caption_max_chars_per_line: int = Field(default=30, ge=8, le=120)
    caption_max_lines: int = Field(default=2, ge=1, le=6)
    caption_min_duration: float = Field(default=0.6, gt=0)
    caption_max_duration: float = Field(default=6.0, gt=0)
    caption_primary_colour: str = "&H00FFFFFF"
    caption_outline_colour: str = "&H00000000"
    caption_back_colour: str = "&H50000000"
    caption_accent_colour: str = "&H0000E5FF"
    caption_emphasis_colour: str = "&H0000D7FF"
    caption_emphasis_scale: int = Field(default=112, ge=100, le=130)
    caption_safe_area_ratio: float = Field(default=0.1, ge=0, le=0.3)
    caption_shaping: str = "complex"
    dynamic_captions: bool = True
    caption_phrase_max_words: int = Field(default=4, ge=1, le=12)
    caption_phrase_max_chars: int = Field(default=20, ge=8, le=120)
    caption_phrase_max_seconds: float = Field(default=3.2, gt=0, le=15)
    caption_break_gap_seconds: float = Field(default=0.6, ge=0, le=5)
    caption_importance_threshold: float = Field(default=0.5, ge=0, le=1)
    effects_enabled: bool = True
    effects_min_importance: float = Field(default=0.45, ge=0, le=1)
    effects_max_visual: int = Field(default=2, ge=0, le=4)
    effects_zoom_min_duration: float = Field(default=6.0, ge=0)
    effects_zoom_duration: float = Field(default=4.0, gt=0)
    effects_zoom_intensity: float = Field(default=0.09, ge=0, le=0.2)
    effects_punch_intensity: float = Field(default=0.12, ge=0, le=0.2)
    effects_punch_duration: float = Field(default=0.9, gt=0, le=5)
    effects_emphasis_intensity: float = Field(default=0.06, ge=0, le=0.2)
    effects_emphasis_duration: float = Field(default=1.0, gt=0, le=5)
    effects_min_gap_seconds: float = Field(default=0.6, ge=0, le=5)
    effects_transitions: bool = True
    effects_transition_duration: float = Field(default=0.3, gt=0, le=2)
    auto_vertical_fit: bool = True
    vertical_landscape_fit: ClipFit = ClipFit.BLUR

    @field_validator("pixel_format", "audio_codec", "caption_shaping")
    @classmethod
    def _normalise_token(cls, value: str) -> str:
        token = value.strip()
        if not token:
            raise ValueError("value cannot be empty")
        return token

    @model_validator(mode="after")
    def _validate_profiles(self) -> ClipConfig:
        if self.min_clip_seconds > self.max_clip_seconds:
            raise ValueError("min_clip_seconds must be <= max_clip_seconds")
        if self.caption_min_duration > self.caption_max_duration:
            raise ValueError("caption_min_duration must be <= caption_max_duration")
        if self.effects_zoom_duration > self.max_clip_seconds:
            raise ValueError("effects_zoom_duration must be <= max_clip_seconds")
        for fmt in ClipFormat:
            if fmt.value not in self.formats:
                raise ValueError(f"formats must include '{fmt.value}'")
        for quality in ClipQuality:
            if quality.value not in self.qualities:
                raise ValueError(f"qualities must include '{quality.value}'")
        if not self.default_formats:
            raise ValueError("default_formats cannot be empty")
        for fmt in self.default_formats:
            if fmt.value not in self.formats:
                raise ValueError(f"default format '{fmt.value}' has no profile")
        if self.default_quality.value not in self.qualities:
            raise ValueError("default_quality has no profile")
        return self

    def format_profile(self, fmt: ClipFormat) -> ClipFormatProfile:
        try:
            return self.formats[fmt.value]
        except KeyError as exc:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Clip format '{fmt.value}' has no configured profile.",
                retryable=False,
            ) from exc

    def quality_profile(self, quality: ClipQuality) -> ClipQualityProfile:
        try:
            return self.qualities[quality.value]
        except KeyError as exc:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Clip quality '{quality.value}' has no configured profile.",
                retryable=False,
            ) from exc


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


def load_clip_config(config_dir: Path | str) -> ClipConfig:
    config_dir = Path(config_dir)
    raw = _deep_merge(
        _read_json(config_dir / "clips.json"),
        _read_json(config_dir / "local" / "clips.json"),
    )
    try:
        return ClipConfig.model_validate(raw.get("clips") or raw)
    except ValidationError as exc:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            f"Invalid clip configuration: {exc}",
            retryable=False,
        ) from exc
