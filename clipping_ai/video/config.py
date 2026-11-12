"""Video ingestion and preparation configuration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from clipping_ai.errors import AIError, ErrorCategory

DEFAULT_EXTENSIONS = [
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
    ".avi",
    ".m4v",
    ".mpeg",
    ".mpg",
    ".ts",
    ".flv",
    ".wmv",
]


class VideoConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    max_upload_bytes: int = 2_147_483_648
    max_duration_seconds: float = 21_600.0
    allowed_extensions: list[str] = Field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    duplicate_detection: bool = True
    ffmpeg_binary: str = "ffmpeg"
    ffprobe_binary: str = "ffprobe"
    metadata_timeout_seconds: float = 60.0
    download_timeout_seconds: float = 1800.0
    extract_timeout_seconds: float = 3600.0
    chunk_timeout_seconds: float = 300.0
    audio_sample_rate: int = 16_000
    audio_channels: int = 1
    audio_codec: str = "flac"
    chunk_seconds: float = 600.0
    chunk_overlap_seconds: float = 0.0
    yt_dlp_format: str = "bv*[height<=1080]+ba/b[height<=1080]/b"
    url_schemes: list[str] = Field(default_factory=lambda: ["http", "https"])
    cleanup_temp: bool = True
    worker_poll_seconds: float = 1.0

    @field_validator("allowed_extensions")
    @classmethod
    def _normalise_extensions(cls, value: list[str]) -> list[str]:
        extensions: list[str] = []
        for item in value:
            extension = item.strip().lower()
            if not extension:
                continue
            if not extension.startswith("."):
                extension = f".{extension}"
            if extension not in extensions:
                extensions.append(extension)
        if not extensions:
            raise ValueError("allowed_extensions cannot be empty")
        return extensions

    @field_validator("url_schemes")
    @classmethod
    def _normalise_schemes(cls, value: list[str]) -> list[str]:
        schemes = [item.strip().lower() for item in value if item.strip()]
        if not schemes:
            raise ValueError("url_schemes cannot be empty")
        return schemes

    @field_validator("audio_codec")
    @classmethod
    def _normalise_codec(cls, value: str) -> str:
        codec = value.strip().lower()
        if not codec:
            raise ValueError("audio_codec cannot be empty")
        return codec

    @model_validator(mode="after")
    def _sane_limits(self) -> VideoConfig:
        if self.max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes must be positive")
        if self.max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive")
        if self.metadata_timeout_seconds <= 0:
            raise ValueError("metadata_timeout_seconds must be positive")
        if self.download_timeout_seconds <= 0:
            raise ValueError("download_timeout_seconds must be positive")
        if self.extract_timeout_seconds <= 0:
            raise ValueError("extract_timeout_seconds must be positive")
        if self.chunk_timeout_seconds <= 0:
            raise ValueError("chunk_timeout_seconds must be positive")
        if self.audio_sample_rate <= 0:
            raise ValueError("audio_sample_rate must be positive")
        if self.audio_channels <= 0:
            raise ValueError("audio_channels must be positive")
        if self.chunk_seconds <= 0:
            raise ValueError("chunk_seconds must be positive")
        if self.chunk_overlap_seconds < 0:
            raise ValueError("chunk_overlap_seconds must be >= 0")
        if self.chunk_overlap_seconds >= self.chunk_seconds:
            raise ValueError("chunk_overlap_seconds must be smaller than chunk_seconds")
        if self.worker_poll_seconds <= 0:
            raise ValueError("worker_poll_seconds must be positive")
        return self

    @property
    def audio_extension(self) -> str:
        return {
            "flac": ".flac",
            "wav": ".wav",
            "pcm_s16le": ".wav",
            "mp3": ".mp3",
            "libmp3lame": ".mp3",
            "aac": ".m4a",
            "m4a": ".m4a",
            "opus": ".opus",
            "libopus": ".opus",
        }.get(self.audio_codec, f".{self.audio_codec}")

    def allows_extension(self, extension: str) -> bool:
        return extension.lower() in self.allowed_extensions


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


def load_video_config(config_dir: Path | str) -> VideoConfig:
    config_dir = Path(config_dir)
    raw = _deep_merge(
        _read_json(config_dir / "video.json"),
        _read_json(config_dir / "local" / "video.json"),
    )
    try:
        return VideoConfig.model_validate(raw.get("video") or raw)
    except ValidationError as exc:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            f"Invalid video configuration: {exc}",
            retryable=False,
        ) from exc
