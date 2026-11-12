from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from clipping_ai.settings import Settings
from clipping_ai.video.config import VideoConfig
from clipping_ai.video.ingest import DownloadedSource
from clipping_ai.video.service import VideoService

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
requires_ffmpeg = pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe not installed")


def make_test_video(path: Path, *, duration: float = 2.0, with_audio: bool = True) -> Path:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size=160x120:rate=10:duration={duration}",
    ]
    if with_audio:
        command.extend(["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"])
    command.extend(["-shortest", "-c:v", "mpeg4"])
    if with_audio:
        command.extend(["-c:a", "aac"])
    command.append(str(path))
    subprocess.run(command, check=True)
    return path


def make_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    settings.ensure_dirs()
    return settings


def make_service(
    tmp_path: Path,
    *,
    config: VideoConfig | None = None,
    downloader: object | None = None,
) -> VideoService:
    settings = make_settings(tmp_path)
    return VideoService(
        settings,
        config=config or VideoConfig(chunk_seconds=1, max_upload_bytes=10_000_000),
        downloader=downloader,  # type: ignore[arg-type]
    )


class FakeDownloader:
    def __init__(self, source: Path, title: str | None = "fake title") -> None:
        self.source = source
        self.title = title
        self.calls: list[tuple[str, Path]] = []

    def download(self, url: str, destination: Path) -> DownloadedSource:
        self.calls.append((url, destination))
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / f"source{self.source.suffix}"
        shutil.copyfile(self.source, target)
        return DownloadedSource(path=target, title=self.title)
