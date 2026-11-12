from __future__ import annotations

from types import SimpleNamespace

import pytest

from clipping_ai.audio import AudioChunk
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.jobs import TranscriptionCache
from clipping_ai.providers.groq_speech import GroqSpeechProvider
from clipping_ai.providers.litellm_text import LitellmTextProvider, _content_to_text
from clipping_ai.providers.whisper_cpp import WhisperCppProvider
from clipping_ai.registry import ProviderConfig
from tests.helpers import speech_model, text_model


class FakeTranscriptions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, file, **kwargs):
        self.calls.append({"file": file, "kwargs": kwargs})
        name = file[0]
        return {
            "text": f"text from {name}",
            "language": "en",
            "duration": 1.5,
            "segments": [{"start": 0.0, "end": 1.5, "text": f"segment {name}"}],
            "words": [{"word": "hello", "start": 0.0, "end": 0.5, "probability": 0.9}],
        }


class FakeModels:
    def __init__(self) -> None:
        self.data = [
            SimpleNamespace(id="whisper-large-v3-turbo"),
            SimpleNamespace(id="llama-3.3-70b-versatile"),
        ]

    def list(self):
        return self


class FakeGroqClient:
    def __init__(self) -> None:
        self.audio = SimpleNamespace(transcriptions=FakeTranscriptions())
        self.models = FakeModels()


def groq_provider(tmp_path, *, max_audio_bytes=25_000_000, client=None):
    client = client or FakeGroqClient()
    config = ProviderConfig(
        type="groq_speech",
        enabled=True,
        api_key_env="GROQ_API_KEY",
        max_audio_bytes=max_audio_bytes,
        chunk_seconds=600,
        timeout_seconds=30,
    )
    provider = GroqSpeechProvider(
        "groq_speech",
        config,
        secret_getter=lambda _: "test-key",
        cache=TranscriptionCache(tmp_path / "jobs.db"),
        client_factory=lambda _: client,
    )
    return provider, client


def test_groq_transcription_parses_timestamps_and_caches(tmp_path):
    provider, client = groq_provider(tmp_path)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake audio")
    model = speech_model("groq-whisper-turbo", provider="groq_speech")

    first = provider.transcribe(model, audio, language="en")
    assert first.text == "text from audio.mp3"
    assert first.segments[0].start == 0.0
    assert first.words[0].word == "hello"
    assert len(client.audio.transcriptions.calls) == 1

    second = provider.transcribe(model, audio, language="en")
    assert second.text == first.text
    assert len(client.audio.transcriptions.calls) == 1


def test_groq_transcription_chunks_and_offsets(tmp_path, monkeypatch):
    provider, client = groq_provider(tmp_path, max_audio_bytes=1)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake audio")
    chunk_a = tmp_path / "a.flac"
    chunk_b = tmp_path / "b.flac"
    chunk_a.write_bytes(b"a")
    chunk_b.write_bytes(b"b")

    monkeypatch.setattr(
        "clipping_ai.providers.groq_speech.ffmpeg_available", lambda _: True
    )
    monkeypatch.setattr(
        "clipping_ai.providers.groq_speech.split_audio",
        lambda *args, **kwargs: [
            AudioChunk(index=0, path=chunk_a, offset=0.0, duration=1.0),
            AudioChunk(index=1, path=chunk_b, offset=10.0, duration=1.0),
        ],
    )

    model = speech_model("groq-whisper-turbo", provider="groq_speech")
    result = provider.transcribe(model, audio)
    assert result.text == "text from a.flac text from b.flac"
    assert result.segments[1].start == 10.0
    assert len(client.audio.transcriptions.calls) == 2


def test_groq_connection_lists_whisper_models(tmp_path):
    provider, _ = groq_provider(tmp_path)
    result = provider.test_connection(speech_model("groq-whisper-turbo", provider="groq_speech"))
    assert result.ok is True
    assert result.details["whisper_models"] == ["whisper-large-v3-turbo"]


def test_whisper_cpp_parse_offsets_and_tokens():
    config = ProviderConfig(type="whisper_cpp", enabled=True)
    provider = WhisperCppProvider("whisper_cpp", config)
    payload = {
        "transcription": [
            {
                "offsets": {"from": 0, "to": 1500},
                "text": " hello",
                "tokens": [
                    {
                        "text": "[_BEG_]",
                        "offsets": {"from": 0, "to": 0},
                        "p": 0.8,
                    },
                    {
                        "text": " hello",
                        "offsets": {"from": 0, "to": 500},
                        "p": 0.9,
                    },
                ],
            }
        ],
        "result": {"language": "en"},
    }
    model = speech_model("local-whisper", provider="whisper_cpp")
    result = provider._parse(payload, model)
    assert result.text == "hello"
    assert result.language == "en"
    assert result.segments[0].start == 0.0
    assert result.segments[0].end == 1.5
    assert result.words[0].word == "hello"


def test_litellm_provider_passes_api_key_explicitly():
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    provider = LitellmTextProvider(
        "gemini",
        ProviderConfig(type="litellm", api_key_env="GEMINI_API_KEY"),
        secret_getter=lambda _: "test-key",
        completion_fn=fake_completion,
    )
    model = text_model("gemini-test", provider="gemini")
    result = provider.complete(model, [{"role": "user", "content": "hello"}])
    assert result.text == "OK"
    assert captured["api_key"] == "test-key"
    assert captured["model"] == "gemini-test"


def test_litellm_availability_and_model_resolution():
    config = ProviderConfig(
        type="litellm",
        api_key_env="GEMINI_API_KEY",
        litellm_prefix="gemini/",
    )
    missing = LitellmTextProvider("gemini", config, secret_getter=lambda _: None)
    assert missing.available() == (False, "GEMINI_API_KEY is not set")

    provider = LitellmTextProvider("gemini", config, secret_getter=lambda _: "key")
    assert provider.available() == (True, "ok")
    entry = text_model("gemini-test")
    assert provider.litellm_model(entry) == "gemini/gemini-test"
    entry.litellm_model = "custom/model"
    assert provider.litellm_model(entry) == "custom/model"

    disabled = LitellmTextProvider(
        "gemini",
        ProviderConfig(type="litellm", enabled=False),
        secret_getter=lambda _: "key",
    )
    assert disabled.available() == (False, "provider disabled in configuration")


def test_content_to_text_handles_strings_dicts_and_objects():
    parts = [
        "a",
        {"text": "b"},
        {"content": "c"},
        SimpleNamespace(text="d"),
        SimpleNamespace(other="ignored"),
        {"text": 5},
    ]
    assert _content_to_text(parts) == "abcd"
    assert _content_to_text("plain") == "plain"
    assert _content_to_text(None) == ""


def test_litellm_completion_extracts_content_and_reported_cost():
    def fake_completion(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=[
                            {"text": "A"},
                            {"content": "B"},
                            "C",
                            SimpleNamespace(text="D"),
                        ]
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3, cost=None),
            _hidden_params={"response_cost": 0.001},
        )

    provider = LitellmTextProvider(
        "gemini",
        ProviderConfig(type="litellm", api_key_env=None),
        secret_getter=lambda _: None,
        completion_fn=fake_completion,
    )
    result = provider.complete(
        text_model("m"), [{"role": "user", "content": "hi"}], max_tokens=5
    )
    assert result.text == "ABCD"
    assert result.input_tokens == 2
    assert result.output_tokens == 3
    assert result.cost == pytest.approx(0.001)


def test_litellm_rejects_unapproved_model():
    provider = LitellmTextProvider(
        "gemini",
        ProviderConfig(type="litellm", api_key_env=None),
        secret_getter=lambda _: None,
        completion_fn=lambda **_: None,
    )
    with pytest.raises(AIError) as exc:
        provider.complete(text_model("m", free=False), [])
    assert exc.value.category == ErrorCategory.POLICY_VIOLATION


def test_litellm_rejects_empty_or_malformed_response():
    empty = LitellmTextProvider(
        "gemini",
        ProviderConfig(type="litellm", api_key_env=None),
        secret_getter=lambda _: None,
        completion_fn=lambda **_: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="  "))]
        ),
    )
    with pytest.raises(AIError) as exc:
        empty.complete(text_model("m"), [])
    assert exc.value.category == ErrorCategory.INVALID_RESPONSE

    malformed = LitellmTextProvider(
        "gemini",
        ProviderConfig(type="litellm", api_key_env=None),
        secret_getter=lambda _: None,
        completion_fn=lambda **_: SimpleNamespace(),
    )
    with pytest.raises(AIError) as exc:
        malformed.complete(text_model("m"), [])
    assert exc.value.category == ErrorCategory.INVALID_RESPONSE


def test_litellm_maps_provider_exception():
    provider = LitellmTextProvider(
        "gemini",
        ProviderConfig(type="litellm", api_key_env=None),
        secret_getter=lambda _: None,
        completion_fn=lambda **_: (_ for _ in ()).throw(ValueError("boom")),
    )
    with pytest.raises(AIError):
        provider.complete(text_model("m"), [])


def test_litellm_test_connection_success_and_failure():
    def good_completion(**kwargs):
        assert kwargs["max_tokens"] >= 64
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, cost=0.0),
        )

    good = LitellmTextProvider(
        "gemini",
        ProviderConfig(type="litellm", api_key_env=None),
        secret_getter=lambda _: None,
        completion_fn=good_completion,
    )
    result = good.test_connection(text_model("m"))
    assert result.ok is True
    assert "OK" in result.message

    bad = LitellmTextProvider(
        "gemini",
        ProviderConfig(type="litellm", api_key_env=None),
        secret_getter=lambda _: None,
        completion_fn=lambda **_: (_ for _ in ()).throw(ValueError("boom")),
    )
    result = bad.test_connection(text_model("m"))
    assert result.ok is False
    assert result.details["category"]


def test_whisper_cpp_is_unavailable_without_binary_and_model(tmp_path):
    config = ProviderConfig(
        type="whisper_cpp",
        enabled=True,
        binary=str(tmp_path / "missing"),
        model_path=str(tmp_path / "missing.bin"),
    )
    provider = WhisperCppProvider("whisper_cpp", config)
    available, reason = provider.available()
    assert available is False
    assert "binary not found" in reason


def test_whisper_cpp_health_distinguishes_setup_states(tmp_path):
    missing_binary = ProviderConfig(
        type="whisper_cpp",
        enabled=True,
        binary="clipping-ai-definitely-missing-whisper",
        model_path=str(tmp_path / "model.bin"),
    )
    result = WhisperCppProvider("whisper_cpp", missing_binary).inspect()
    assert result.ok is False
    assert result.details["status"] == "not_installed"

    fake_binary = tmp_path / "whisper-cli"
    fake_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_binary.chmod(0o755)
    missing_model = ProviderConfig(
        type="whisper_cpp",
        enabled=True,
        binary=str(fake_binary),
        model_path=str(tmp_path / "missing.bin"),
    )
    result = WhisperCppProvider("whisper_cpp", missing_model).inspect()
    assert result.ok is False
    assert result.details["status"] == "model_missing"

    model = tmp_path / "model.bin"
    model.write_text("model", encoding="utf-8")
    ready = ProviderConfig(
        type="whisper_cpp",
        enabled=True,
        binary=str(fake_binary),
        model_path=str(model),
    )
    provider = WhisperCppProvider("whisper_cpp", ready)
    assert provider.inspect().details["status"] == "ready"
    health = provider.health(None)
    assert health.ok is True
    assert health.details["status"] == "ready"

    misconfigured = ProviderConfig(
        type="whisper_cpp",
        enabled=True,
        binary=str(tmp_path / "missing-binary"),
        model_path=str(model),
    )
    result = WhisperCppProvider("whisper_cpp", misconfigured).inspect()
    assert result.details["status"] == "misconfigured"

    disabled = ProviderConfig(
        type="whisper_cpp",
        enabled=False,
        binary=str(fake_binary),
        model_path=str(model),
    )
    result = WhisperCppProvider("whisper_cpp", disabled).inspect()
    assert result.details["status"] == "disabled"


def test_whisper_cpp_health_reports_execution_error(tmp_path):
    binary = tmp_path / "whisper-cli"
    binary.write_text("#!/bin/sh\necho broken >&2\nexit 2\n", encoding="utf-8")
    binary.chmod(0o755)
    model = tmp_path / "model.bin"
    model.write_text("model", encoding="utf-8")
    config = ProviderConfig(
        type="whisper_cpp",
        enabled=True,
        binary=str(binary),
        model_path=str(model),
    )
    result = WhisperCppProvider("whisper_cpp", config).health(None)
    assert result.ok is False
    assert result.details["status"] == "error"
    assert result.details["help_exit_code"] == 2
