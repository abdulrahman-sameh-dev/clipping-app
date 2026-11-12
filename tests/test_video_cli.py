from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from clipping_ai.cli import build_parser, main
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.settings import Settings
from clipping_ai.video.models import (
    AudioStreamInfo,
    JobStage,
    JobStatus,
    SourceType,
    VideoJob,
    VideoMetadata,
    VideoStreamInfo,
)


def make_job(job_id: str = "a" * 32, status: JobStatus = JobStatus.READY) -> VideoJob:
    now = datetime.now(UTC).isoformat()
    return VideoJob(
        id=job_id,
        source_type=SourceType.UPLOAD,
        source_value="sample.mp4",
        input_path="/tmp/opencode/sample.mp4",
        metadata=VideoMetadata(
            duration_seconds=2.0,
            size_bytes=10,
            format_name="mp4",
            has_video=True,
            has_audio=True,
            video_streams=[
                VideoStreamInfo(index=0, codec_name="mpeg4", width=160, height=120, fps=10.0)
            ],
            audio_streams=[AudioStreamInfo(index=1, codec_name="aac", sample_rate=44100, channels=1)],
        ),
        audio_path="/tmp/opencode/audio.flac",
        status=status,
        stage=JobStage.READY if status == JobStatus.READY else JobStage.QUEUED,
        progress=1.0 if status == JobStatus.READY else 0.0,
        created_at=now,
        updated_at=now,
    )


class FakeVideoService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.job = make_job()

    def inspect(self, path):
        self.calls.append(("inspect", path))
        return self.job.metadata

    def prepare_local(self, path):
        self.calls.append(("prepare_local", path))
        return self.job

    def prepare_url(self, url):
        self.calls.append(("prepare_url", url))
        return self.job

    def get(self, job_id):
        self.calls.append(("get", job_id))
        if job_id == "missing":
            raise AIError(ErrorCategory.NOT_FOUND, "missing", retryable=False)
        return self.job

    def list(self, *, limit=20, status=None):
        self.calls.append(("list", (limit, status)))
        return [self.job]

    def retry(self, job_id):
        self.calls.append(("retry", job_id))
        return self.job

    def cancel(self, job_id):
        self.calls.append(("cancel", job_id))
        return self.job


@pytest.fixture
def cli_video_env(monkeypatch, tmp_path):
    settings = Settings(config_dir=tmp_path / "config", data_dir=tmp_path / "data", _env_file=None)
    settings.ensure_dirs()
    service = FakeVideoService()
    monkeypatch.setattr("clipping_ai.cli.get_settings", lambda: settings)
    monkeypatch.setattr("clipping_ai.cli._video_service", lambda: service)
    return service


def test_video_parser_accepts_commands():
    parser = build_parser()
    assert parser.parse_args(["prepare", "clip.mp4"]).source == "clip.mp4"
    assert parser.parse_args(["jobs", "--status", "ready"]).status == "ready"
    assert parser.parse_args(["inspect", "clip.mp4", "--json"]).json is True


def test_prepare_local_and_url(cli_video_env, capsys):
    service = cli_video_env
    assert main(["prepare", "clip.mp4", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "a" * 32
    assert service.calls[-1] == ("prepare_local", "clip.mp4")
    assert main(["prepare", "https://example.com/video"]) == 0
    assert service.calls[-1] == ("prepare_url", "https://example.com/video")
    assert "status=ready" in capsys.readouterr().out


def test_inspect_job_jobs_retry_cancel(cli_video_env, capsys):
    assert main(["inspect", "clip.mp4", "--json"]) == 0
    assert "duration_seconds" in capsys.readouterr().out
    assert main(["job", "a" * 32]) == 0
    assert "sample.mp4" in capsys.readouterr().out
    assert main(["jobs", "--status", "ready"]) == 0
    assert "ready" in capsys.readouterr().out
    assert main(["retry", "a" * 32]) == 0
    assert main(["cancel", "a" * 32]) == 0
    assert capsys.readouterr().out


def test_video_cli_reports_errors(cli_video_env, capsys):
    assert main(["job", "missing"]) == 1
    assert "missing" in capsys.readouterr().err
