from __future__ import annotations

from fastapi.testclient import TestClient

from clipping_ai.api.app import create_app
from clipping_ai.registry import PolicyConfig, RouteCandidate
from clipping_ai.schemas import TranscriptResult
from tests.helpers import (
    FakeSpeechProvider,
    FakeTextProvider,
    make_manager,
    make_registry,
    speech_model,
    text_model,
)

VALID = (
    '{"summary":"A summary","key_points":["point"],"topics":["topic"],'
    '"sentiment":"neutral","content_type":"talk","language":"en"}'
)


def app_for(tmp_path, *, text_provider=None, speech_provider=None):
    text_provider = text_provider or FakeTextProvider("p1", [VALID])
    registry = make_registry(
        providers={
            "p1": type(text_provider.config).model_validate(
                {"type": "litellm", "enabled": True, "api_key_env": "GEMINI_API_KEY"}
            )
        },
        models=[text_model("free-1", provider="p1")],
        routing={"content_analysis": [RouteCandidate(provider="p1", model="free-1")]},
        policy=PolicyConfig(schema_validation_retries=0),
    )
    manager = make_manager(registry, {"p1": text_provider}, tmp_path)
    return create_app(manager)


def test_read_only_endpoints(tmp_path):
    client = TestClient(app_for(tmp_path))
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/api/ai/providers").status_code == 200
    assert client.get("/api/ai/models").status_code == 200
    assert client.get("/api/ai/status").json()["local_only"] is True
    assert client.get("/api/ai/usage").status_code == 200


def test_analyze_endpoint_returns_structured_data(tmp_path):
    client = TestClient(app_for(tmp_path))
    response = client.post(
        "/api/ai/analyze",
        json={"task": "content_analysis", "text": "hello"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["summary"] == "A summary"
    assert body["provider"] == "p1"


def test_analyze_endpoint_maps_policy_error_to_403(tmp_path):
    provider = FakeTextProvider("p1", [VALID])
    client = TestClient(app_for(tmp_path, text_provider=provider))
    response = client.post(
        "/api/ai/analyze",
        json={"task": "content_analysis", "text": "hello", "model": "paid"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["category"] == "policy_violation"


def test_analyze_endpoint_maps_missing_key_to_503(tmp_path):
    provider = FakeTextProvider(
        "p1",
        [VALID],
        available=False,
        reason="GEMINI_API_KEY is not set",
    )
    client = TestClient(app_for(tmp_path, text_provider=provider))
    response = client.post(
        "/api/ai/analyze",
        json={"task": "content_analysis", "text": "hello"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["category"] == "missing_api_key"


def test_models_endpoint_filters_by_task(tmp_path):
    client = TestClient(app_for(tmp_path))
    response = client.get("/api/ai/models", params={"task": "translation"})
    assert response.status_code == 200
    assert response.json()["models"] == []


def test_transcribe_endpoint(tmp_path):
    transcript = TranscriptResult(
        text="hello",
        language="en",
        duration=1.0,
        provider="speech",
        model="speech-free",
    )
    speech = FakeSpeechProvider("speech", [transcript])
    registry = make_registry(
        providers={
            "speech": speech.config,
        },
        models=[speech_model("speech-free", provider="speech")],
        routing={"speech_to_text": [RouteCandidate(provider="speech", model="speech-free")]},
        policy=PolicyConfig(schema_validation_retries=0),
    )
    manager = make_manager(registry, {"speech": speech}, tmp_path)
    client = TestClient(create_app(manager))
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"fake")
    response = client.post(
        "/api/ai/transcribe",
        json={"audio_path": str(audio), "language": "en"},
    )
    assert response.status_code == 200
    assert response.json()["transcript"]["text"] == "hello"


def test_verify_free_non_openrouter_is_supported_false(tmp_path):
    client = TestClient(app_for(tmp_path))
    response = client.post("/api/ai/verify-free/free-1")
    assert response.status_code == 200
    assert response.json()["supported"] is False


def test_test_connection_endpoint(tmp_path):
    client = TestClient(app_for(tmp_path))
    response = client.post(
        "/api/ai/test-connection",
        json={"provider": "p1", "model": "free-1"},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
