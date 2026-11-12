from __future__ import annotations

import subprocess
from io import BytesIO
from pathlib import Path

import pytest

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.video.config import VideoConfig
from clipping_ai.video.ingest import (
    YtDlpDownloader,
    copy_stream_to_file,
    extension_for,
    is_safe_job_id,
    redact_url,
    sanitize_original_filename,
    validate_url,
)


def test_filename_and_extension_validation():
    assert sanitize_original_filename("/tmp/../clip.mp4") == "clip.mp4"
    with pytest.raises(AIError):
        sanitize_original_filename("")
    with pytest.raises(AIError):
        sanitize_original_filename("..")
    config = VideoConfig()
    assert extension_for("clip.MP4", config) == ".mp4"
    with pytest.raises(AIError):
        extension_for("clip.txt", config)
    with pytest.raises(AIError):
        extension_for("clip", config)


def test_url_validation_and_redaction():
    config = VideoConfig()
    assert validate_url(" https://example.com/watch?v=1 ", config) == "https://example.com/watch?v=1"
    with pytest.raises(AIError):
        validate_url("file:///etc/passwd", config)
    with pytest.raises(AIError):
        validate_url("https://", config)
    with pytest.raises(AIError):
        validate_url("https://example.com/a b", config)
    with pytest.raises(AIError):
        validate_url("https://example.com/" + "a" * 5000, config)
    assert redact_url("https://user:pass@example.com/path?token=secret#frag") == (
        "https://example.com/path"
    )


def test_copy_stream_enforces_size_and_hashes(tmp_path):
    destination = tmp_path / "input.part"
    size, digest = copy_stream_to_file(BytesIO(b"hello"), destination, max_bytes=10)
    assert size == 5
    assert len(digest) == 64
    assert destination.read_bytes() == b"hello"
    with pytest.raises(AIError) as exc_info:
        copy_stream_to_file(BytesIO(b"x" * 11), tmp_path / "big.part", max_bytes=10)
    assert exc_info.value.category == ErrorCategory.INVALID_REQUEST
    with pytest.raises(AIError):
        copy_stream_to_file(BytesIO(b""), tmp_path / "empty.part", max_bytes=10)


def test_job_id_validation():
    assert is_safe_job_id("a" * 32)
    assert not is_safe_job_id("../" + "a" * 32)
    assert not is_safe_job_id("A" * 32)
    assert not is_safe_job_id("a" * 31)


def test_yt_dlp_success(monkeypatch, tmp_path):
    config = VideoConfig(max_upload_bytes=1000)

    def fake_run(command, **kwargs):
        destination = Path(command[command.index("--output") + 1]).parent
        (destination / "source.webm").write_bytes(b"video")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("clipping_ai.video.ingest.subprocess.run", fake_run)
    result = YtDlpDownloader(config).download("https://example.com/v", tmp_path)
    assert result.path.name == "source.webm"
    assert result.path.read_bytes() == b"video"


def test_yt_dlp_failure_timeout_and_bad_output(monkeypatch, tmp_path):
    config = VideoConfig(max_upload_bytes=1000, download_timeout_seconds=1)

    def fail(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "failed https://example.com/v?token=x")

    monkeypatch.setattr("clipping_ai.video.ingest.subprocess.run", fail)
    with pytest.raises(AIError) as exc_info:
        YtDlpDownloader(config).download("https://example.com/v?token=x", tmp_path / "a")
    assert "token" not in exc_info.value.message

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr("clipping_ai.video.ingest.subprocess.run", timeout)
    with pytest.raises(AIError) as timeout_info:
        YtDlpDownloader(config).download("https://example.com/v", tmp_path / "b")
    assert timeout_info.value.category == ErrorCategory.NETWORK_TIMEOUT

    def no_output(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("clipping_ai.video.ingest.subprocess.run", no_output)
    with pytest.raises(AIError):
        YtDlpDownloader(config).download("https://example.com/v", tmp_path / "c")


def test_yt_dlp_rejects_unsupported_or_large_output(monkeypatch, tmp_path):
    config = VideoConfig(max_upload_bytes=10)

    def unsupported(command, **kwargs):
        destination = Path(command[command.index("--output") + 1]).parent
        (destination / "source.exe").write_bytes(b"x")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("clipping_ai.video.ingest.subprocess.run", unsupported)
    with pytest.raises(AIError):
        YtDlpDownloader(config).download("https://example.com/v", tmp_path / "a")

    def large(command, **kwargs):
        destination = Path(command[command.index("--output") + 1]).parent
        (destination / "source.mp4").write_bytes(b"x" * 20)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("clipping_ai.video.ingest.subprocess.run", large)
    with pytest.raises(AIError):
        YtDlpDownloader(config).download("https://example.com/v", tmp_path / "b")
