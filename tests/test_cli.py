from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from clipping_ai.cli import _read_json_arg, _read_text, build_parser, main
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.settings import Settings


class FakeManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.usage = SimpleNamespace(
            totals=lambda: {"requests": 1, "successes": 1},
            summary=lambda days: [
                {
                    "provider": "p",
                    "model": "m",
                    "task": "content_analysis",
                    "requests": 1,
                    "failures": 0,
                    "audio_seconds": 0.0,
                }
            ],
            daily_counts=lambda days: [],
            recent=lambda limit: [],
        )

    def status(self, include_health=False):
        return {
            "version": "0.1.0",
            "ready": True,
            "local_only": True,
            "models": {"free_approved": 1, "enabled": 1, "total": 1},
            "providers": [
                {
                    "id": "p",
                    "configured": True,
                    "key_present": True,
                    "models": 1,
                    "reason": "ok",
                }
            ],
            "policy": {"free_only": True},
            "usage": {"requests": 1},
        }

    def list_providers(self):
        return [
            {
                "id": "p",
                "configured": True,
                "enabled": True,
                "models": 1,
                "key_present": True,
                "reason": "ok",
            }
        ]

    def list_models(self):
        return [
            {
                "id": "m",
                "provider": "p",
                "free": True,
                "status": "enabled",
                "free_verified": True,
                "tasks": ["content_analysis"],
            },
            {
                "id": "speech",
                "provider": "speech",
                "free": True,
                "status": "enabled",
                "free_verified": True,
                "tasks": ["speech_to_text"],
            },
        ]

    def analyze(self, task, **kwargs):
        self.calls.append(("analyze", {"task": task, **kwargs}))
        return {"data": {"summary": "ok"}}

    def transcribe(self, audio_path, **kwargs):
        self.calls.append(("transcribe", {"audio_path": audio_path, **kwargs}))
        return {"transcript": {"text": "hello"}}

    def test_connection(self, provider, model=None):
        self.calls.append(("test_connection", {"provider": provider, "model": model}))
        return {"ok": True}

    def verify_free(self, model_id):
        self.calls.append(("verify_free", {"model_id": model_id}))
        return {"supported": False}


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    settings = Settings(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    settings.ensure_dirs()
    manager = FakeManager()
    monkeypatch.setattr("clipping_ai.cli.get_settings", lambda: settings)
    monkeypatch.setattr("clipping_ai.cli._manager", lambda: manager)
    return settings, manager


def test_parser_accepts_all_tasks():
    parser = build_parser()
    args = parser.parse_args(["models", "--task", "speech_to_text", "--json"])
    assert args.task == "speech_to_text"
    assert args.json is True


def test_status_command_emits_json(cli_env, capsys):
    code = main(["status", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["local_only"] is True
    assert "providers" in payload


def test_models_command_filters_by_task(cli_env, capsys):
    code = main(["models", "--task", "speech_to_text", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in payload["models"]] == ["speech"]


def test_human_readable_commands(cli_env, capsys):
    assert main(["status"]) == 0
    assert "Clipping AI" in capsys.readouterr().out
    assert main(["providers"]) == 0
    assert "configured=True" in capsys.readouterr().out
    assert main(["models"]) == 0
    assert "free=True" in capsys.readouterr().out
    assert main(["usage"]) == 0
    assert "By provider/model/task" in capsys.readouterr().out


def test_usage_command_handles_empty_summary(cli_env, monkeypatch, capsys):
    manager = cli_env[1]
    manager.usage.summary = lambda days: []
    assert main(["usage"]) == 0
    assert "No usage recorded yet." in capsys.readouterr().out


def test_analyze_command_merges_payload(cli_env, tmp_path, capsys):
    manager = cli_env[1]
    payload_file = tmp_path / "payload.json"
    payload_file.write_text('{"extra": true}', encoding="utf-8")
    code = main(
        [
            "analyze",
            "content_analysis",
            "--text",
            "hello",
            "--payload-file",
            str(payload_file),
            "--provider",
            "p",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["data"]["summary"] == "ok"
    assert manager.calls[0][1]["payload"] == {"extra": True, "text": "hello"}


def test_transcribe_and_connection_commands(cli_env, capsys):
    manager = cli_env[1]
    assert main(["transcribe", "audio.mp3", "--no-word-timestamps"]) == 0
    assert manager.calls[0][1]["word_timestamps"] is False
    assert main(["test-connection", "p", "--model", "m"]) == 0
    assert main(["verify-free", "m"]) == 0
    assert "supported" in capsys.readouterr().out


def test_cli_reports_ai_errors(cli_env, monkeypatch, capsys):
    manager = cli_env[1]

    def fail(include_health=False):
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "bad request",
            details={"field": "x"},
        )

    manager.status = fail
    assert main(["status"]) == 1
    captured = capsys.readouterr()
    assert "bad request" in captured.err
    assert '"field": "x"' in captured.err


def test_cli_reports_os_and_json_errors(cli_env, monkeypatch, capsys):
    monkeypatch.setattr(
        "clipping_ai.cli.run",
        lambda args: (_ for _ in ()).throw(json.JSONDecodeError("bad", "", 0)),
    )
    assert main(["status"]) == 1
    assert "bad" in capsys.readouterr().err


def test_read_json_arg_and_read_text_errors(tmp_path):
    path = tmp_path / "data.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    assert _read_json_arg(None, str(path)) == {"a": 1}
    assert _read_json_arg('{"b": 2}', None) == {"b": 2}
    with pytest.raises(SystemExit):
        _read_json_arg("{}", str(path))
    with pytest.raises(SystemExit):
        _read_json_arg("[1]", None)
    assert _read_text("hello", None) == "hello"
    text_path = tmp_path / "text.txt"
    text_path.write_text("from file", encoding="utf-8")
    assert _read_text(None, str(text_path)) == "from file"
    with pytest.raises(SystemExit):
        _read_text("hello", str(text_path))
    with pytest.raises(SystemExit):
        _read_text(None, None)


def test_serve_command_uses_configured_host_and_port(cli_env, monkeypatch):
    settings = cli_env[0]
    captured = {}
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: captured.update(kwargs))
    assert main(["serve", "--host", "127.0.0.1", "--port", "9999"]) == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9999
    assert captured["reload"] is False
    assert settings.log_level
