"""Source validation, safe upload copying, and optional yt-dlp downloading."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit, urlunsplit

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.video.config import VideoConfig

_MAX_FILENAME_LENGTH = 255
_URL_MAX_LENGTH = 4096


@dataclass(frozen=True)
class DownloadedSource:
    path: Path
    title: str | None = None


def sanitize_original_filename(filename: str | None) -> str:
    if not filename:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "The uploaded file has no filename.",
            retryable=False,
        )
    name = Path(filename).name.strip()
    if not name or name in {".", ".."}:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "The uploaded filename is invalid.",
            retryable=False,
        )
    if len(name) > _MAX_FILENAME_LENGTH:
        name = name[-_MAX_FILENAME_LENGTH:]
    return name


def extension_for(filename: str, config: VideoConfig) -> str:
    extension = Path(filename).suffix.lower()
    if not extension:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "The file has no extension. Use a supported video format: "
            + ", ".join(config.allowed_extensions),
            retryable=False,
        )
    if not config.allows_extension(extension):
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            f"Unsupported video format '{extension}'. Supported: "
            + ", ".join(config.allowed_extensions),
            retryable=False,
        )
    return extension


def validate_url(url: str, config: VideoConfig) -> str:
    candidate = url.strip()
    if not candidate:
        raise AIError(ErrorCategory.INVALID_REQUEST, "A URL is required.", retryable=False)
    if len(candidate) > _URL_MAX_LENGTH:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            f"URL is longer than {_URL_MAX_LENGTH} characters.",
            retryable=False,
        )
    if any(char.isspace() for char in candidate):
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "URL must not contain whitespace.",
            retryable=False,
        )
    parts = urlsplit(candidate)
    if parts.scheme.lower() not in config.url_schemes:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "Only " + ", ".join(config.url_schemes) + " URLs are supported.",
            retryable=False,
        )
    if not parts.hostname:
        raise AIError(ErrorCategory.INVALID_REQUEST, "URL has no host.", retryable=False)
    return candidate


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _redact_text(text: str, url: str) -> str:
    redacted = redact_url(url)
    return text.replace(url, redacted)


def copy_stream_to_file(
    stream: BinaryIO,
    destination: Path,
    *,
    max_bytes: int,
    chunk_size: int = 1024 * 1024,
) -> tuple[int, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    with destination.open("wb") as fh:
        while True:
            block = stream.read(chunk_size)
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    f"Upload exceeds the maximum size of {max_bytes} bytes.",
                    retryable=False,
                    details={"max_upload_bytes": max_bytes},
                )
            digest.update(block)
            fh.write(block)
    if total == 0:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "The uploaded file is empty.",
            retryable=False,
        )
    return total, digest.hexdigest()


def _candidate_downloads(directory: Path) -> list[Path]:
    files = []
    for path in directory.glob("source.*"):
        if not path.is_file():
            continue
        if path.name.endswith((".part", ".ytdl")):
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.stat().st_size, reverse=True)


class YtDlpDownloader:
    def __init__(self, config: VideoConfig) -> None:
        self.config = config

    def download(self, url: str, destination: Path) -> DownloadedSource:
        if shutil.which("yt-dlp") is None and not self._module_available():
            raise AIError(
                ErrorCategory.PROVIDER_UNAVAILABLE,
                "yt-dlp is not installed. Install it with 'uv sync'.",
                retryable=False,
            )
        destination.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--no-progress",
            "--no-warnings",
            "--quiet",
            "--no-part",
            "--restrict-filenames",
            "--max-filesize",
            str(self.config.max_upload_bytes),
            "--socket-timeout",
            "30",
            "--retries",
            "2",
            "--fragment-retries",
            "2",
            "--format",
            self.config.yt_dlp_format,
            "--output",
            str(destination / "source.%(ext)s"),
            url,
        ]
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.config.download_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise AIError(
                ErrorCategory.NETWORK_TIMEOUT,
                f"Download timed out after {self.config.download_timeout_seconds:.0f} seconds.",
                retryable=True,
                details={"source": redact_url(url)},
                cause=exc,
            ) from exc
        if proc.returncode != 0:
            detail = _redact_text(proc.stderr.strip()[-800:], url)
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "yt-dlp could not download this URL." + (f" {detail}" if detail else ""),
                retryable=False,
                details={"source": redact_url(url)},
            )
        candidates = _candidate_downloads(destination)
        if not candidates:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "yt-dlp finished without producing a media file.",
                retryable=False,
                details={"source": redact_url(url)},
            )
        path = candidates[0]
        extension = path.suffix.lower()
        if not self.config.allows_extension(extension):
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Downloaded format '{extension}' is not supported. Supported: "
                + ", ".join(self.config.allowed_extensions),
                retryable=False,
            )
        if path.stat().st_size > self.config.max_upload_bytes:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Downloaded file exceeds the maximum size of "
                f"{self.config.max_upload_bytes} bytes.",
                retryable=False,
            )
        return DownloadedSource(path=path, title=None)

    @staticmethod
    def _module_available() -> bool:
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            return False
        return True


def is_safe_job_id(job_id: str) -> bool:
    return re.fullmatch(r"[a-f0-9]{32}", job_id) is not None
