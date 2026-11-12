"""Audio helpers for chunked speech-to-text.

FFmpeg is used only to split audio when it exceeds a provider's upload limit.
No video downloading, clip generation or editing is implemented here.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from clipping_ai.errors import AIError, ErrorCategory


@dataclass(frozen=True)
class AudioChunk:
    index: int
    path: Path
    offset: float
    duration: float | None = None


def hash_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def ffmpeg_available(binary: str = "ffmpeg") -> bool:
    return shutil.which(binary) is not None


def _ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def probe_duration(path: Path, *, ffmpeg_binary: str = "ffmpeg") -> float | None:
    if _ffprobe_available():
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode == 0:
            try:
                return float(json.loads(proc.stdout)["format"]["duration"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
    if shutil.which(ffmpeg_binary):
        proc = subprocess.run(
            [ffmpeg_binary, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
        if match:
            hours, minutes, seconds = match.groups()
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return None


def split_audio(
    path: Path,
    *,
    chunk_seconds: int,
    overlap_seconds: float = 0.0,
    ffmpeg_binary: str = "ffmpeg",
    output_dir: Path | None = None,
    force_reencode: bool = False,
) -> list[AudioChunk]:
    """Split audio into 16 kHz mono FLAC chunks. Caller owns output_dir lifetime."""
    if chunk_seconds <= 0:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "chunk_seconds must be positive.",
            retryable=False,
        )
    duration = probe_duration(path, ffmpeg_binary=ffmpeg_binary)
    if duration is None:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "Audio exceeds the provider size limit and its duration could not be read. "
            "Install ffmpeg/ffprobe or provide a smaller file.",
            retryable=False,
        )
    if duration <= chunk_seconds and not force_reencode:
        return [AudioChunk(index=0, path=path, offset=0.0, duration=duration)]
    if not shutil.which(ffmpeg_binary):
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            f"Audio exceeds the provider size limit and '{ffmpeg_binary}' is not installed. "
            "Install ffmpeg to enable automatic chunking.",
            retryable=False,
        )

    output_dir = output_dir or Path(tempfile.mkdtemp(prefix="clipping-ai-audio-"))
    output_dir.mkdir(parents=True, exist_ok=True)
    step = max(chunk_seconds - overlap_seconds, 1.0)
    chunks: list[AudioChunk] = []
    index = 0
    start = 0.0
    while start < duration:
        length = min(chunk_seconds, duration - start)
        target = output_dir / f"chunk_{index:04d}.flac"
        proc = subprocess.run(
            [
                ffmpeg_binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{start:.3f}",
                "-t",
                f"{length:.3f}",
                "-i",
                str(path),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "flac",
                str(target),
            ],
            capture_output=True,
            text=True,
            timeout=max(120, int(length) + 60),
        )
        if proc.returncode != 0 or not target.exists():
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"ffmpeg failed to split audio: {proc.stderr.strip()[:500]}",
                retryable=False,
            )
        chunks.append(AudioChunk(index=index, path=target, offset=start, duration=length))
        index += 1
        start += step
    return chunks
