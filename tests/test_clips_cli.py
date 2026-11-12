from __future__ import annotations

import json

import pytest

from clipping_ai.cli import build_parser, main
from clipping_ai.clips.models import ClipStatus
from clipping_ai.settings import Settings
from tests.test_clips import make_clip_job


class FakeClipService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.job = make_clip_job()

    def config_dict(self):
        return {"formats": {"vertical": {"width": 1080, "height": 1920}}}

    def create_for_job(self, job_id, **kwargs):
        self.calls.append(("create_for_job", {"job_id": job_id, **kwargs}))
        return [self.job]

    def process(self, clip_id):
        self.calls.append(("process", clip_id))
        return self.job.model_copy(update={"status": ClipStatus.READY})

    def get(self, clip_id):
        self.calls.append(("get", clip_id))
        return self.job

    def list(self, *, source_job_id=None, status=None, limit=20):
        self.calls.append(("list", (source_job_id, status, limit)))
        if status is not None:
            return [self.job.model_copy(update={"status": status})]
        return [self.job]

    def retry(self, clip_id):
        self.calls.append(("retry", clip_id))
        return self.job

    def cancel(self, clip_id):
        self.calls.append(("cancel", clip_id))
        return self.job.model_copy(update={"status": ClipStatus.CANCELLED})


@pytest.fixture
def cli_clip_env(monkeypatch, tmp_path):
    settings = Settings(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    settings.ensure_dirs()
    service = FakeClipService()
    monkeypatch.setattr("clipping_ai.cli.get_settings", lambda: settings)
    monkeypatch.setattr("clipping_ai.cli._clip_service", lambda: service)
    return service


def test_clip_parser_accepts_commands():
    parser = build_parser()
    args = parser.parse_args(
        [
            "render-clips",
            "a" * 32,
            "--format",
            "vertical",
            "--format",
            "square",
            "--captions",
            "--caption-language",
            "ar",
            "--highlight-index",
            "1",
            "--queue",
        ]
    )
    assert args.formats == ["vertical", "square"]
    assert args.captions is True
    assert args.caption_language == "ar"
    assert args.highlight_indices == [1]
    assert args.queue is True
    assert parser.parse_args(["clip-jobs", "a" * 32, "--status", "ready"]).job_id == "a" * 32
    assert parser.parse_args(["clips-config", "--json"]).json is True


def test_clip_cli_commands(cli_clip_env, capsys):
    service = cli_clip_env
    assert (
        main(
            [
                "render-clips",
                "a" * 32,
                "--format",
                "vertical",
                "--captions",
                "--queue",
                "--json",
            ]
        )
        == 0
    )
    body = json.loads(capsys.readouterr().out)
    assert body["count"] == 1
    assert body["jobs"][0]["id"] == "a" * 32
    assert service.calls[0][0] == "create_for_job"
    assert all(call[0] != "process" for call in service.calls)

    assert main(["clip-job", "a" * 32, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "a" * 32
    assert main(["clip-jobs", "a" * 32, "--status", "ready"]) == 0
    assert "ready" in capsys.readouterr().out
    assert main(["render-clip", "a" * 32]) == 0
    assert "status=ready" in capsys.readouterr().out
    assert main(["retry-clip", "a" * 32]) == 0
    assert main(["cancel-clip", "a" * 32]) == 0
    assert "cancelled" in capsys.readouterr().out
    assert main(["clips-config"]) == 0
    assert "vertical" in capsys.readouterr().out
