"""Audio extraction and timestamped chunk creation for video jobs."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.video.config import VideoConfig
from clipping_ai.video.models import AudioChunkInfo

_CODEC_ARGS: dict[str, list[str]] = {
    "flac": ["-c:a", "flac"],
    "wav": ["-c:a", "pcm_s16le"],
    "pcm_s16le": ["-c:a", "pcm_s16le"],
    "mp3": ["-c:a", "libmp3lame"],
    "libmp3lame": ["-c:a", "libmp3lame"],
    "aac": ["-c:a", "aac"],
    "m4a": ["-c:a", "aac"],
    "opus": ["-c:a", "libopus"],
    "libopus": ["-c:a", "libopus"],
}


def _require_ffmpeg(config: VideoConfig) -> None:
    if shutil.which(config.ffmpeg_binary) is None:
        raise AIError(
            ErrorCategory.PROVIDER_UNAVAILABLE,
            f"'{config.ffmpeg_binary}' is not installed. Install ffmpeg.",
            retryable=False,
        )


def _codec_args(config: VideoConfig) -> list[str]:
    args = _CODEC_ARGS.get(config.audio_codec)
    if args is None:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            f"Unsupported audio codec '{config.audio_codec}'.",
            retryable=False,
        )
    return args


def extract_audio(
    input_path: Path,
    output_path: Path,
    config: VideoConfig,
) -> Path:
    _require_ffmpeg(config)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        config.ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ar",
        str(config.audio_sample_rate),
        "-ac",
        str(config.audio_channels),
        *_codec_args(config),
        str(output_path),
    ]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=config.extract_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise AIError(
            ErrorCategory.NETWORK_TIMEOUT,
            f"Audio extraction timed out after {config.extract_timeout_seconds:.0f} seconds.",
            retryable=True,
            cause=exc,
        ) from exc
    if proc.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "ffmpeg could not extract audio: " + proc.stderr.strip()[:500],
            retryable=False,
        )
    return output_path


def split_audio_file(
    audio_path: Path,
    output_dir: Path,
    config: VideoConfig,
    *,
    duration: float,
) -> list[AudioChunkInfo]:
    _require_ffmpeg(config)
    if duration <= 0:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "Cannot split audio with a non-positive duration.",
            retryable=False,
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    step = max(config.chunk_seconds - config.chunk_overlap_seconds, 0.1)
    extension = config.audio_extension
    chunks: list[AudioChunkInfo] = []
    start = 0.0
    index = 0
    while start < duration:
        length = min(config.chunk_seconds, duration - start)
        target = output_dir / f"chunk_{index:04d}{extension}"
        command = [
            config.ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{length:.3f}",
            "-i",
            str(audio_path),
            "-ar",
            str(config.audio_sample_rate),
            "-ac",
            str(config.audio_channels),
            *_codec_args(config),
            str(target),
        ]
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=max(config.chunk_timeout_seconds, length + 60.0),
            )
        except subprocess.TimeoutExpired as exc:
            raise AIError(
                ErrorCategory.NETWORK_TIMEOUT,
                f"Audio chunk {index} timed out.",
                retryable=True,
                cause=exc,
            ) from exc
        if proc.returncode != 0 or not target.exists() or target.stat().st_size == 0:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"ffmpeg could not create audio chunk {index}: {proc.stderr.strip()[:500]}",
                retryable=False,
            )
        chunks.append(
            AudioChunkInfo(
                index=index,
                path=target,
                offset=start,
                duration=length,
                size_bytes=target.stat().st_size,
            )
        )
        index += 1
        start += step
    return chunks
