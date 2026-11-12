"""Normalised ffprobe metadata extraction."""

from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.video.config import VideoConfig
from clipping_ai.video.models import AudioStreamInfo, VideoMetadata, VideoStreamInfo


def _as_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fps(value: Any) -> float | None:
    if value in (None, "", "0/0"):
        return None
    try:
        return float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return None


def _video_stream(raw: dict[str, Any]) -> VideoStreamInfo:
    return VideoStreamInfo(
        index=_as_int(raw.get("index")) or 0,
        codec_name=raw.get("codec_name"),
        codec_long_name=raw.get("codec_long_name"),
        width=_as_int(raw.get("width")),
        height=_as_int(raw.get("height")),
        fps=_fps(raw.get("avg_frame_rate") or raw.get("r_frame_rate")),
        bit_rate=_as_int(raw.get("bit_rate")),
        duration=_as_float(raw.get("duration")),
        pix_fmt=raw.get("pix_fmt"),
    )


def _audio_stream(raw: dict[str, Any]) -> AudioStreamInfo:
    return AudioStreamInfo(
        index=_as_int(raw.get("index")) or 0,
        codec_name=raw.get("codec_name"),
        codec_long_name=raw.get("codec_long_name"),
        sample_rate=_as_int(raw.get("sample_rate")),
        channels=_as_int(raw.get("channels")),
        channel_layout=raw.get("channel_layout"),
        bit_rate=_as_int(raw.get("bit_rate")),
        duration=_as_float(raw.get("duration")),
    )


def probe_video(path: Path | str, config: VideoConfig) -> VideoMetadata:
    media_path = Path(path)
    if not media_path.is_file():
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            f"Video file does not exist: {media_path}",
            retryable=False,
        )
    if shutil.which(config.ffprobe_binary) is None:
        raise AIError(
            ErrorCategory.PROVIDER_UNAVAILABLE,
            f"'{config.ffprobe_binary}' is not installed. Install ffmpeg/ffprobe.",
            retryable=False,
        )
    command = [
        config.ffprobe_binary,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(media_path),
    ]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=config.metadata_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise AIError(
            ErrorCategory.NETWORK_TIMEOUT,
            f"ffprobe timed out after {config.metadata_timeout_seconds:.0f} seconds.",
            retryable=True,
            cause=exc,
        ) from exc
    if proc.returncode != 0:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "ffprobe could not read this media file: " + proc.stderr.strip()[:500],
            retryable=False,
        )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AIError(
            ErrorCategory.INVALID_RESPONSE,
            "ffprobe returned invalid JSON.",
            retryable=False,
            cause=exc,
        ) from exc
    streams = payload.get("streams") or []
    if not isinstance(streams, list):
        raise AIError(
            ErrorCategory.INVALID_RESPONSE,
            "ffprobe returned an unexpected streams shape.",
            retryable=False,
        )
    format_info = payload.get("format") or {}
    if not isinstance(format_info, dict):
        format_info = {}

    video_streams = [
        _video_stream(stream)
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    audio_streams = [
        _audio_stream(stream)
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    if not audio_streams:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "The media file has no audio stream.",
            retryable=False,
        )

    duration = _as_float(format_info.get("duration"))
    if duration is None:
        stream_durations = [
            value
            for value in [*(s.duration for s in video_streams), *(s.duration for s in audio_streams)]
            if value is not None
        ]
        duration = max(stream_durations) if stream_durations else None
    if duration is None or duration <= 0:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "The media duration could not be determined.",
            retryable=False,
        )
    if duration > config.max_duration_seconds:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            f"Video duration {duration:.1f}s exceeds the maximum of "
            f"{config.max_duration_seconds:.0f}s.",
            retryable=False,
        )

    size_bytes = _as_int(format_info.get("size")) or media_path.stat().st_size
    if size_bytes > config.max_upload_bytes:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            f"Video size {size_bytes} bytes exceeds the maximum of "
            f"{config.max_upload_bytes} bytes.",
            retryable=False,
        )
    return VideoMetadata(
        duration_seconds=duration,
        size_bytes=size_bytes,
        format_name=format_info.get("format_name"),
        bit_rate=_as_int(format_info.get("bit_rate")),
        has_video=bool(video_streams),
        has_audio=bool(audio_streams),
        video_streams=video_streams,
        audio_streams=audio_streams,
    )
