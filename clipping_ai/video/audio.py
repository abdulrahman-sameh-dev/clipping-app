"""Audio extraction and timestamped chunk creation for video jobs."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from clipping_ai.audio import hash_file
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


def _chunk_plan(
    output_dir: Path, config: VideoConfig, duration: float
) -> list[tuple[int, float, float, Path]]:
    step = max(config.chunk_seconds - config.chunk_overlap_seconds, 0.1)
    extension = config.audio_extension
    planned: list[tuple[int, float, float, Path]] = []
    start = 0.0
    index = 0
    while start < duration:
        length = min(config.chunk_seconds, duration - start)
        planned.append(
            (index, start, length, output_dir / f"chunk_{index:04d}{extension}")
        )
        index += 1
        start += step
    return planned


def _chunk_config(config: VideoConfig) -> dict[str, Any]:
    return {
        "chunk_seconds": config.chunk_seconds,
        "chunk_overlap_seconds": config.chunk_overlap_seconds,
        "audio_sample_rate": config.audio_sample_rate,
        "audio_channels": config.audio_channels,
        "audio_codec": config.audio_codec,
        "audio_extension": config.audio_extension,
        "ffmpeg_binary": config.ffmpeg_binary,
    }


def _manifest_entries(
    manifest: dict[str, Any] | None,
    *,
    audio_sha256: str,
    config: VideoConfig,
    duration: float,
    output_dir: Path,
) -> dict[int, dict[str, Any]]:
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        return {}
    if manifest.get("audio_sha256") != audio_sha256:
        return {}
    if manifest.get("duration") != duration:
        return {}
    for key, value in _chunk_config(config).items():
        if manifest.get(key) != value:
            return {}
    entries: dict[int, dict[str, Any]] = {}
    root = output_dir.resolve()
    for entry in manifest.get("chunks") or []:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry["index"])
            path = Path(str(entry["path"]))
            path.resolve().relative_to(root)
        except (KeyError, TypeError, ValueError):
            continue
        entries[index] = entry
    return entries


def _reusable_chunk(
    entry: dict[str, Any] | None,
    *,
    target: Path,
    index: int,
    offset: float,
    duration: float,
) -> tuple[int, str] | None:
    if entry is None:
        return None
    try:
        if int(entry["index"]) != index:
            return None
        if Path(str(entry["path"])).resolve() != target.resolve():
            return None
        if abs(float(entry["offset"]) - offset) > 1e-6:
            return None
        if abs(float(entry["duration"]) - duration) > 1e-6:
            return None
        expected_hash = str(entry["sha256"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        if not target.is_file():
            return None
        size = target.stat().st_size
        if size == 0:
            return None
        if int(entry.get("size_bytes", -1)) != size:
            return None
        if hash_file(target) != expected_hash:
            return None
    except OSError:
        return None
    return size, expected_hash


def validate_chunk_manifest(
    manifest: dict[str, Any] | None,
    output_dir: Path,
    config: VideoConfig,
    *,
    duration: float,
    audio_sha256: str,
) -> list[AudioChunkInfo] | None:
    if duration <= 0:
        return None
    existing = _manifest_entries(
        manifest,
        audio_sha256=audio_sha256,
        config=config,
        duration=duration,
        output_dir=output_dir,
    )
    planned = _chunk_plan(output_dir, config, duration)
    if set(existing) != {index for index, _start, _length, _target in planned}:
        return None
    chunks: list[AudioChunkInfo] = []
    for index, start, length, target in planned:
        reusable = _reusable_chunk(
            existing.get(index),
            target=target,
            index=index,
            offset=start,
            duration=length,
        )
        if reusable is None:
            return None
        size, _sha256 = reusable
        chunks.append(
            AudioChunkInfo(
                index=index,
                path=target,
                offset=start,
                duration=length,
                size_bytes=size,
            )
        )
    return chunks


def _chunk_manifest(
    config: VideoConfig,
    audio_sha256: str,
    duration: float,
    chunks: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "version": 1,
        "audio_sha256": audio_sha256,
        "duration": duration,
        **_chunk_config(config),
        "chunks": chunks,
    }


def build_chunk_manifest(
    audio_path: Path,
    output_dir: Path,
    config: VideoConfig,
    *,
    duration: float,
    audio_sha256: str,
    existing_manifest: dict[str, Any] | None = None,
    on_chunk: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[AudioChunkInfo], dict[str, Any]]:
    _require_ffmpeg(config)
    if duration <= 0:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "Cannot split audio with a non-positive duration.",
            retryable=False,
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = _manifest_entries(
        existing_manifest,
        audio_sha256=audio_sha256,
        config=config,
        duration=duration,
        output_dir=output_dir,
    )
    chunks: list[AudioChunkInfo] = []
    manifest_chunks: list[dict[str, Any]] = []
    for index, start, length, target in _chunk_plan(output_dir, config, duration):
        reusable = _reusable_chunk(
            existing.get(index),
            target=target,
            index=index,
            offset=start,
            duration=length,
        )
        if reusable is None:
            temp = target.with_name(f".{target.stem}.{uuid4().hex}{target.suffix}")
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
                str(temp),
            ]
            try:
                proc = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=max(config.chunk_timeout_seconds, length + 60.0),
                )
            except subprocess.TimeoutExpired as exc:
                temp.unlink(missing_ok=True)
                raise AIError(
                    ErrorCategory.NETWORK_TIMEOUT,
                    f"Audio chunk {index} timed out.",
                    retryable=True,
                    cause=exc,
                ) from exc
            except OSError:
                temp.unlink(missing_ok=True)
                raise
            if proc.returncode != 0 or not temp.exists() or temp.stat().st_size == 0:
                temp.unlink(missing_ok=True)
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    f"ffmpeg could not create audio chunk {index}: "
                    f"{proc.stderr.strip()[:500]}",
                    retryable=False,
                )
            try:
                os.replace(temp, target)
            except OSError:
                temp.unlink(missing_ok=True)
                raise
            size = target.stat().st_size
            sha256 = hash_file(target)
        else:
            size, sha256 = reusable
        chunks.append(
            AudioChunkInfo(
                index=index,
                path=target,
                offset=start,
                duration=length,
                size_bytes=size,
            )
        )
        manifest_chunks.append(
            {
                "index": index,
                "path": str(target),
                "offset": start,
                "duration": length,
                "size_bytes": size,
                "sha256": sha256,
            }
        )
        if on_chunk is not None:
            on_chunk(_chunk_manifest(config, audio_sha256, duration, manifest_chunks))
    manifest = _chunk_manifest(config, audio_sha256, duration, manifest_chunks)
    return chunks, manifest


def split_audio_file(
    audio_path: Path,
    output_dir: Path,
    config: VideoConfig,
    *,
    duration: float,
) -> list[AudioChunkInfo]:
    chunks, _manifest = build_chunk_manifest(
        audio_path,
        output_dir,
        config,
        duration=duration,
        audio_sha256=hash_file(audio_path),
    )
    return chunks
