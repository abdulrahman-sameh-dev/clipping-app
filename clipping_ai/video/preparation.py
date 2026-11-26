"""Fingerprints and checkpoint validation for resumable video preparation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clipping_ai.audio import hash_file
from clipping_ai.recovery.idempotency import stable_fingerprint
from clipping_ai.video.audio import validate_chunk_manifest
from clipping_ai.video.config import VideoConfig
from clipping_ai.video.models import AudioChunkInfo, VideoJob, VideoMetadata

STAGE_SOURCE = "source"
STAGE_PROBE = "probe"
STAGE_AUDIO = "audio"
STAGE_CHUNKS = "chunks"

_PROBE_VERSION = 1
_AUDIO_VERSION = 1
_CHUNK_VERSION = 1
_CHECKPOINT_VERSION = 1


@dataclass(frozen=True)
class SourceArtifact:
    path: Path
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class AudioArtifact:
    path: Path
    sha256: str
    size_bytes: int


def _identity(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_fingerprint(job: VideoJob) -> str:
    return stable_fingerprint(
        scope="video.source",
        version=_CHECKPOINT_VERSION,
        source_type=job.source_type.value,
        source_identity=_identity(job.source_value),
    )


def probe_fingerprint(source_sha256: str, config: VideoConfig) -> str:
    return stable_fingerprint(
        scope="video.probe",
        version=_PROBE_VERSION,
        source_sha256=source_sha256,
        ffprobe_binary=config.ffprobe_binary,
        max_duration_seconds=config.max_duration_seconds,
        max_upload_bytes=config.max_upload_bytes,
    )


def audio_config(config: VideoConfig) -> dict[str, Any]:
    return {
        "audio_codec": config.audio_codec,
        "audio_sample_rate": config.audio_sample_rate,
        "audio_channels": config.audio_channels,
        "audio_extension": config.audio_extension,
        "ffmpeg_binary": config.ffmpeg_binary,
    }


def audio_fingerprint(source_sha256: str, config: VideoConfig) -> str:
    return stable_fingerprint(
        scope="video.audio",
        version=_AUDIO_VERSION,
        source_sha256=source_sha256,
        **audio_config(config),
    )


def chunk_fingerprint(audio_sha256: str, duration: float, config: VideoConfig) -> str:
    return stable_fingerprint(
        scope="video.chunks",
        version=_CHUNK_VERSION,
        audio_sha256=audio_sha256,
        duration=duration,
        chunk_seconds=config.chunk_seconds,
        chunk_overlap_seconds=config.chunk_overlap_seconds,
        audio_sample_rate=config.audio_sample_rate,
        audio_channels=config.audio_channels,
        audio_codec=config.audio_codec,
        audio_extension=config.audio_extension,
        ffmpeg_binary=config.ffmpeg_binary,
    )


def source_checkpoint(
    *, source_type: str, path: Path, source_sha256: str, size_bytes: int
) -> dict[str, Any]:
    return {
        "version": _CHECKPOINT_VERSION,
        "source_type": source_type,
        "path": str(path),
        "source_sha256": source_sha256,
        "size_bytes": size_bytes,
    }


def source_sha256_from_checkpoint(checkpoint: Any) -> str | None:
    if not isinstance(checkpoint, dict) or checkpoint.get("version") != _CHECKPOINT_VERSION:
        return None
    value = checkpoint.get("source_sha256")
    return str(value) if value else None


def metadata_checkpoint(
    metadata: VideoMetadata, *, source_sha256: str
) -> dict[str, Any]:
    return {
        "version": _CHECKPOINT_VERSION,
        "source_sha256": source_sha256,
        "metadata": metadata.model_dump(mode="json"),
    }


def metadata_from_checkpoint(
    checkpoint: Any, *, source_sha256: str
) -> VideoMetadata | None:
    if not isinstance(checkpoint, dict) or checkpoint.get("version") != _CHECKPOINT_VERSION:
        return None
    if checkpoint.get("source_sha256") != source_sha256:
        return None
    raw = checkpoint.get("metadata")
    if not isinstance(raw, dict):
        return None
    try:
        metadata = VideoMetadata.model_validate(raw)
    except Exception:
        return None
    if not _valid_metadata(metadata):
        return None
    return metadata


def _valid_metadata(metadata: VideoMetadata) -> bool:
    return bool(
        metadata.duration_seconds > 0
        and metadata.size_bytes > 0
        and metadata.has_audio
        and metadata.audio_streams
    )


def audio_checkpoint(
    artifact: AudioArtifact, *, source_sha256: str, config: VideoConfig
) -> dict[str, Any]:
    return {
        "version": _CHECKPOINT_VERSION,
        "source_sha256": source_sha256,
        "path": str(artifact.path),
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "config": audio_config(config),
    }


def audio_from_checkpoint(
    checkpoint: Any,
    *,
    expected_path: Path,
    source_sha256: str,
    config: VideoConfig,
) -> AudioArtifact | None:
    if not isinstance(checkpoint, dict) or checkpoint.get("version") != _CHECKPOINT_VERSION:
        return None
    if checkpoint.get("source_sha256") != source_sha256:
        return None
    if checkpoint.get("config") != audio_config(config):
        return None
    try:
        path = Path(str(checkpoint["path"]))
        expected_hash = str(checkpoint["sha256"])
        expected_size = int(checkpoint["size_bytes"])
    except (KeyError, TypeError, ValueError):
        return None
    if path.resolve() != expected_path.resolve():
        return None
    if not path.is_file() or path.stat().st_size != expected_size or expected_size <= 0:
        return None
    try:
        if hash_file(path) != expected_hash:
            return None
    except OSError:
        return None
    return AudioArtifact(path=path, sha256=expected_hash, size_bytes=expected_size)


def chunks_from_checkpoint(
    checkpoint: Any,
    *,
    output_dir: Path,
    audio_sha256: str,
    duration: float,
    config: VideoConfig,
) -> list[AudioChunkInfo] | None:
    if not isinstance(checkpoint, dict):
        return None
    return validate_chunk_manifest(
        checkpoint,
        output_dir,
        config,
        duration=duration,
        audio_sha256=audio_sha256,
    )
