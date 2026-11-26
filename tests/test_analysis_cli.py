from __future__ import annotations

import json

import pytest

from clipping_ai.cli import build_parser, main
from clipping_ai.highlights.models import ContentType, Highlight, HighlightResult
from clipping_ai.schemas import TranscriptResult, TranscriptSegment
from clipping_ai.settings import Settings
from tests.test_video_cli import make_job


class FakeAnalysisService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.with_manager_calls: list[bool] = []
        self.transcript = TranscriptResult(
            text="hello world",
            language="en",
            duration=2.0,
            segments=[TranscriptSegment(id=0, start=0.0, end=2.0, text="hello world")],
            provider="whisper_cpp",
            model="tiny.en",
        )
        self.job = make_job().model_copy(
            update={
                "transcript": self.transcript,
                "highlight_result": HighlightResult(
                    job_id="a" * 32,
                    content_type=ContentType.PODCAST,
                    language="en",
                    duration=2.0,
                    highlights=[
                        Highlight(
                            start_time=0.0,
                            end_time=2.0,
                            title="Intro",
                            description="Opening",
                            selection_reason="Test",
                            confidence=0.8,
                            category="topic",
                        )
                    ],
                ),
            }
        )

    def config_dict(self):
        return {"transcription": {"default_language": None}, "highlights": {"enabled": True}}

    def get_transcript(self, job_id):
        self.calls.append(("get_transcript", job_id))
        return self.transcript

    def transcribe(self, job_id, **kwargs):
        self.calls.append(("transcribe", {"job_id": job_id, **kwargs}))
        return self.job

    def highlights(self, job_id, **kwargs):
        self.calls.append(("highlights", {"job_id": job_id, **kwargs}))
        return self.job

    def analyze(self, job_id, **kwargs):
        self.calls.append(("analyze", {"job_id": job_id, **kwargs}))
        return self.job


@pytest.fixture
def cli_analysis_env(monkeypatch, tmp_path):
    settings = Settings(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    settings.ensure_dirs()
    service = FakeAnalysisService()
    monkeypatch.setattr("clipping_ai.cli.get_settings", lambda: settings)
    monkeypatch.setattr(
        "clipping_ai.cli._analysis_service",
        lambda *, with_manager: service.with_manager_calls.append(with_manager) or service,
    )
    return service


def test_analysis_parser_accepts_commands():
    parser = build_parser()
    assert parser.parse_args(["transcribe-job", "abc", "--language", "en"]).language == "en"
    assert parser.parse_args(["transcript", "abc", "--json"]).json is True
    assert parser.parse_args(["highlights", "abc", "--content-type", "gaming"]).content_type == "gaming"
    assert parser.parse_args(["analyze-job", "abc", "--no-llm"]).no_llm is True
    assert parser.parse_args(["analysis-config", "--json"]).json is True


def test_analysis_config_and_transcript_commands(cli_analysis_env, capsys):
    service = cli_analysis_env
    assert main(["analysis-config"]) == 0
    assert "default_language" in capsys.readouterr().out

    assert main(["transcript", "a" * 32]) == 0
    assert "hello world" in capsys.readouterr().out

    assert main(["transcript", "a" * 32, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["transcript"]["text"] == "hello world"
    assert service.calls[-1] == ("get_transcript", "a" * 32)


def test_analysis_commands_pass_options(cli_analysis_env, capsys):
    service = cli_analysis_env
    assert main(["transcribe-job", "a" * 32, "--language", "ar", "--no-word-timestamps"]) == 0
    assert service.with_manager_calls[-1] is True
    assert service.calls[-1] == (
        "transcribe",
        {
            "job_id": "a" * 32,
            "provider": None,
            "model": None,
            "language": "ar",
            "word_timestamps": False,
            "prompt": None,
            "refresh": False,
        },
    )
    assert "transcript: 1 segments" in capsys.readouterr().out

    assert main(
        ["highlights", "a" * 32, "--content-type", "gaming", "--no-llm", "--refresh"]
    ) == 0
    assert service.calls[-1] == (
        "highlights",
        {
            "job_id": "a" * 32,
            "content_type": ContentType.GAMING,
            "provider": None,
            "model": None,
            "use_llm": False,
            "refresh": True,
            "multi_pass": None,
            "window_seconds": None,
            "overlap_seconds": None,
            "max_highlights": None,
        },
    )
    assert "highlights: 1" in capsys.readouterr().out

    assert main(
        ["analyze-job", "a" * 32, "--language", "ar", "--content-type", "podcast"]
    ) == 0
    assert service.calls[-1] == (
        "analyze",
        {
            "job_id": "a" * 32,
            "provider": None,
            "model": None,
            "language": "ar",
            "content_type": ContentType.PODCAST,
            "use_llm": None,
            "refresh": False,
            "multi_pass": None,
            "window_seconds": None,
            "overlap_seconds": None,
            "max_highlights": None,
        },
    )


def test_transcript_command_handles_missing_transcript(cli_analysis_env, capsys):
    cli_analysis_env.transcript = None
    assert main(["transcript", "a" * 32]) == 0
    assert "No transcript stored" in capsys.readouterr().out
