from __future__ import annotations

import pytest

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.providers.base import ConnectionResult
from clipping_ai.registry import PolicyConfig, ProviderConfig, RouteCandidate
from clipping_ai.schemas import TranscriptResult
from tests.helpers import (
    FakeSpeechProvider,
    FakeTextProvider,
    completion,
    make_manager,
    make_registry,
    quota_error,
    speech_model,
    text_model,
)

VALID = (
    '{"summary":"A summary","key_points":["point"],"topics":["topic"],'
    '"sentiment":"neutral","content_type":"talk","language":"en"}'
)


def provider_config(**overrides):
    data = {
        "type": "litellm",
        "enabled": True,
        "api_key_env": None,
        "max_retries": 0,
        "requests_per_minute": 0,
        "min_interval_seconds": 0,
    }
    data.update(overrides)
    return ProviderConfig.model_validate(data)


def build(tmp_path, *, models, routing, providers, policy=None):
    registry = make_registry(
        providers={pid: provider_config() for pid in providers},
        models=models,
        routing=routing,
        policy=policy or PolicyConfig(schema_validation_retries=0),
    )
    return make_manager(registry, providers, tmp_path)


def test_free_model_executes_and_usage_is_recorded(tmp_path):
    provider = FakeTextProvider("p1", [VALID])
    manager = build(
        tmp_path,
        models=[text_model("free-1", provider="p1")],
        routing={"content_analysis": [RouteCandidate(provider="p1", model="free-1")]},
        providers={"p1": provider},
    )
    result = manager.analyze("content_analysis", text="hello")
    assert result["provider"] == "p1"
    assert result["data"]["summary"] == "A summary"
    assert result["used_fallback"] is False
    assert len(provider.calls) == 1
    assert manager.usage.count_today("p1", "free-1") == 1
    assert manager.usage.totals()["successes"] == 1


def test_explicit_paid_model_is_rejected_without_provider_call(tmp_path):
    provider = FakeTextProvider("p1", [VALID])
    manager = build(
        tmp_path,
        models=[text_model("paid-1", provider="p1", free=False)],
        routing={"content_analysis": []},
        providers={"p1": provider},
    )
    with pytest.raises(AIError) as exc:
        manager.analyze("content_analysis", text="hello", model="paid-1")
    assert exc.value.category == ErrorCategory.POLICY_VIOLATION
    assert provider.calls == []


def test_rate_limit_falls_back_to_next_free_model(tmp_path):
    first = FakeTextProvider(
        "p1",
        [AIError(ErrorCategory.RATE_LIMIT_EXCEEDED, "rate limited", retryable=True)],
    )
    second = FakeTextProvider("p2", [VALID])
    manager = build(
        tmp_path,
        models=[
            text_model("free-1", provider="p1"),
            text_model("free-2", provider="p2"),
        ],
        routing={
            "content_analysis": [
                RouteCandidate(provider="p1", model="free-1"),
                RouteCandidate(provider="p2", model="free-2"),
            ]
        },
        providers={"p1": first, "p2": second},
    )
    result = manager.analyze("content_analysis", text="hello")
    assert result["used_fallback"] is True
    assert result["model"] == "free-2"
    assert len(first.calls) == 1
    assert len(second.calls) == 1


def test_fallback_never_uses_paid_candidate(tmp_path):
    first = FakeTextProvider(
        "p1",
        [AIError(ErrorCategory.RATE_LIMIT_EXCEEDED, "rate limited", retryable=True)],
    )
    paid = FakeTextProvider("p2", [VALID])
    second = FakeTextProvider("p3", [VALID])
    manager = build(
        tmp_path,
        models=[
            text_model("free-1", provider="p1"),
            text_model("paid-1", provider="p2", free=False),
            text_model("free-2", provider="p3"),
        ],
        routing={
            "content_analysis": [
                RouteCandidate(provider="p1", model="free-1"),
                RouteCandidate(provider="p2", model="paid-1"),
                RouteCandidate(provider="p3", model="free-2"),
            ]
        },
        providers={"p1": first, "p2": paid, "p3": second},
    )
    result = manager.analyze("content_analysis", text="hello")
    assert result["used_fallback"] is True
    assert result["model"] == "free-2"
    assert paid.calls == []


def test_quota_falls_back_to_next_free_model_and_cools_down_provider(tmp_path):
    first = FakeTextProvider("p1", [quota_error("p1", "free-1")])
    second = FakeTextProvider("p2", [VALID, VALID])
    manager = build(
        tmp_path,
        models=[
            text_model("free-1", provider="p1"),
            text_model("free-2", provider="p2"),
        ],
        routing={
            "content_analysis": [
                RouteCandidate(provider="p1", model="free-1"),
                RouteCandidate(provider="p2", model="free-2"),
            ]
        },
        providers={"p1": first, "p2": second},
    )
    result = manager.analyze("content_analysis", text="hello")
    assert result["used_fallback"] is True
    assert result["model"] == "free-2"
    assert len(first.calls) == 1
    assert len(second.calls) == 1

    second_result = manager.analyze("content_analysis", text="again")
    assert second_result["model"] == "free-2"
    assert len(first.calls) == 1
    assert len(second.calls) == 2


def test_quota_fallback_can_be_disabled_by_policy(tmp_path):
    first = FakeTextProvider("p1", [quota_error("p1", "free-1")])
    second = FakeTextProvider("p2", [VALID])
    manager = build(
        tmp_path,
        models=[
            text_model("free-1", provider="p1"),
            text_model("free-2", provider="p2"),
        ],
        routing={
            "content_analysis": [
                RouteCandidate(provider="p1", model="free-1"),
                RouteCandidate(provider="p2", model="free-2"),
            ]
        },
        providers={"p1": first, "p2": second},
        policy=PolicyConfig(
            schema_validation_retries=0,
            fallback_on_quota_exhausted=False,
        ),
    )
    with pytest.raises(AIError) as exc:
        manager.analyze("content_analysis", text="hello")
    assert exc.value.category == ErrorCategory.FREE_QUOTA_EXHAUSTED
    assert len(first.calls) == 1
    assert second.calls == []


def test_schema_failure_retries_with_repair_prompt(tmp_path):
    provider = FakeTextProvider("p1", ["not json", VALID])
    manager = build(
        tmp_path,
        models=[text_model("free-1", provider="p1")],
        routing={"content_analysis": [RouteCandidate(provider="p1", model="free-1")]},
        providers={"p1": provider},
        policy=PolicyConfig(schema_validation_retries=1),
    )
    result = manager.analyze("content_analysis", text="hello")
    assert result["data"]["summary"] == "A summary"
    assert len(provider.calls) == 2
    assert provider.calls[1]["messages"][-1]["role"] == "user"
    assert "corrected JSON" in provider.calls[1]["messages"][-1]["content"]


def test_missing_key_is_reported_without_calling_provider(tmp_path):
    provider = FakeTextProvider(
        "p1",
        [VALID],
        available=False,
        reason="TEST_API_KEY is not set",
    )
    registry = make_registry(
        providers={"p1": provider_config(api_key_env="TEST_API_KEY")},
        models=[text_model("free-1", provider="p1")],
        routing={"content_analysis": [RouteCandidate(provider="p1", model="free-1")]},
        policy=PolicyConfig(schema_validation_retries=0),
    )
    manager = make_manager(registry, {"p1": provider}, tmp_path)
    with pytest.raises(AIError) as exc:
        manager.analyze("content_analysis", text="hello")
    assert exc.value.category == ErrorCategory.MISSING_API_KEY
    assert provider.calls == []


def test_non_json_model_cannot_run_structured_task(tmp_path):
    provider = FakeTextProvider("p1", [VALID])
    manager = build(
        tmp_path,
        models=[text_model("free-1", provider="p1", supports_json=False)],
        routing={"content_analysis": [RouteCandidate(provider="p1", model="free-1")]},
        providers={"p1": provider},
    )
    with pytest.raises(AIError) as exc:
        manager.analyze("content_analysis", text="hello")
    assert exc.value.category == ErrorCategory.UNSUPPORTED_CAPABILITY
    assert provider.calls == []


def test_speech_transcription_uses_free_speech_model(tmp_path):
    transcript = TranscriptResult(
        text="hello world",
        language="en",
        duration=2.0,
        provider="speech",
        model="speech-free",
    )
    provider = FakeSpeechProvider("speech", [transcript])
    manager = build(
        tmp_path,
        models=[speech_model("speech-free", provider="speech")],
        routing={
            "speech_to_text": [RouteCandidate(provider="speech", model="speech-free")]
        },
        providers={"speech": provider},
    )
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"not-real-audio")
    result = manager.transcribe(audio, language="en")
    assert result["transcript"]["text"] == "hello world"
    assert len(provider.calls) == 1
    assert manager.usage.count_today("speech", "speech-free") == 1


def test_connection_test_uses_policy_and_records_usage(tmp_path):
    provider = FakeTextProvider("p1", [completion("OK")])
    manager = build(
        tmp_path,
        models=[text_model("free-1", provider="p1")],
        routing={"content_analysis": [RouteCandidate(provider="p1", model="free-1")]},
        providers={"p1": provider},
    )
    result = manager.test_connection("p1", "free-1")
    assert result["ok"] is True
    assert result["model"] == "free-1"
    assert manager.usage.count_today("p1", "free-1") == 1


def test_status_and_lists_are_safe_to_serialize(tmp_path):
    provider = FakeTextProvider("p1", [VALID])
    manager = build(
        tmp_path,
        models=[text_model("free-1", provider="p1")],
        routing={"content_analysis": [RouteCandidate(provider="p1", model="free-1")]},
        providers={"p1": provider},
    )
    status = manager.status()
    assert status["local_only"] is True
    assert status["models"]["free_approved"] == 1
    assert manager.list_providers()[0]["id"] == "p1"
    assert manager.list_models()[0]["id"] == "free-1"


def test_unknown_openrouter_pricing_does_not_mark_model_verified(tmp_path, monkeypatch):
    provider = FakeTextProvider("openrouter", [VALID])
    model = text_model("free-router", provider="openrouter")
    model.free_verified = False
    registry = make_registry(
        providers={"openrouter": provider_config()},
        models=[model],
        routing={"content_analysis": []},
        policy=PolicyConfig(schema_validation_retries=0),
        config_dir=tmp_path / "config",
    )
    manager = make_manager(registry, {"openrouter": provider}, tmp_path)

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "free-router", "pricing": {"prompt": "0"}}]}

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: FakeResponse())

    result = manager.verify_free("free-router")
    assert result["verified"] is False
    assert registry.get_model("free-router").free_verified is False
    assert not (tmp_path / "config" / "local" / "models.json").exists()


def test_nonzero_openrouter_pricing_revokes_free_and_blocks_execution(tmp_path, monkeypatch):
    provider = FakeTextProvider("openrouter", [VALID])
    registry = make_registry(
        providers={"openrouter": provider_config()},
        models=[text_model("free-router", provider="openrouter")],
        routing={"content_analysis": []},
        policy=PolicyConfig(schema_validation_retries=0),
        config_dir=tmp_path / "config",
    )
    manager = make_manager(registry, {"openrouter": provider}, tmp_path)

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {
                        "id": "free-router",
                        "pricing": {"prompt": "0.000001", "completion": "0"},
                    }
                ]
            }

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: FakeResponse())

    result = manager.verify_free("free-router")
    assert result["verified"] is False
    assert result["free_revoked"] is True
    entry = registry.get_model("free-router")
    assert entry.free is False
    assert entry.free_verified is False

    with pytest.raises(AIError) as exc:
        manager.analyze(
            "content_analysis",
            text="hello",
            provider="openrouter",
            model="free-router",
        )
    assert exc.value.category == ErrorCategory.POLICY_VIOLATION
    assert provider.calls == []


def test_health_check_skips_policy_ineligible_model(tmp_path, monkeypatch):
    provider = FakeTextProvider("p1", [VALID])
    model = text_model("free-1", provider="p1")
    model.free_verified = False
    registry = make_registry(
        providers={"p1": provider_config()},
        models=[model],
        routing={"content_analysis": []},
        policy=PolicyConfig(require_verified_free=True, schema_validation_retries=0),
        config_dir=tmp_path / "config",
    )
    manager = make_manager(registry, {"p1": provider}, tmp_path)
    called: list[str] = []

    def spy(entry):
        called.append(entry.id)
        return ConnectionResult(ok=True, provider="p1", model=entry.id, message="ok")

    monkeypatch.setattr(provider, "test_connection", spy)

    result = manager.health_check()["p1"]
    assert result.ok is False
    assert result.details == {"category": "policy_violation"}
    assert called == []
