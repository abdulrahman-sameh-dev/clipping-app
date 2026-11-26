from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from clipping_ai.api.app import create_app
from clipping_ai.errors import ErrorCategory
from clipping_ai.manager import ProviderManager
from clipping_ai.policy import FreeOnlyPolicy
from clipping_ai.providers import nvidia_catalog
from clipping_ai.providers.base import ConnectionResult
from clipping_ai.registry import (
    ModelEntry,
    PolicyConfig,
    ProviderConfig,
    Registry,
    RouteCandidate,
)
from clipping_ai.routing import Router
from clipping_ai.schemas import TEXT_TASKS
from clipping_ai.settings import Settings
from clipping_ai.throttle import Throttle
from clipping_ai.usage import UsageTracker
from tests.helpers import FakeTextProvider, make_manager, make_registry

PROJECT_ROOT = Path(__file__).resolve().parents[1]

API_IDS = ["nvidia/llama-3-1-8b-instruct", "nvidia/kimi-k3-1-pro"]


def resource(
    name: str = "llama-3-1-8b-instruct",
    *,
    publisher: str = "nvidia",
    nim_type: str = "Free Endpoint",
    available: str | None = "true",
    general: tuple[str, ...] = ("Chat",),
    score: float = 10.0,
) -> dict:
    labels = [
        {"key": "nimType", "values": [nim_type]},
        {"key": "publisher", "values": [publisher]},
        {"key": "playgroundType", "values": list(general)},
    ]
    attributes = [] if available is None else [{"key": "AVAILABLE", "value": available}]
    return {
        "resourceId": f"resource-{name}",
        "name": name,
        "displayName": name.replace("-", " ").title(),
        "description": f"{name} description",
        "labels": labels,
        "attributes": attributes,
        "weightPopular": score,
    }


def discovered_model(**overrides) -> ModelEntry:
    data = {
        "id": "nvidia-test",
        "provider": "nvidia",
        "provider_model_id": "nvidia/test",
        "tasks": list(TEXT_TASKS),
        "free": True,
        "free_verified": False,
        "status": "enabled",
        "access_type": "free_endpoint",
        "pricing_status": "free",
        "api_available": "unknown",
        "verification_status": "unverified",
        "discovered": True,
        "catalog_resource_id": "resource-test",
    }
    data.update(overrides)
    return ModelEntry.model_validate(data)


def test_free_endpoint_requires_explicit_label_not_a_name():
    labelled = nvidia_catalog.classify_resource(resource(), API_IDS)
    assert labelled is not None
    assert labelled["free"] is True
    assert labelled["access_type"] == "free_endpoint"
    assert labelled["tasks"] == list(TEXT_TASKS)
    assert labelled["api_available"] == "unknown"
    assert labelled["verification_status"] == "unverified"

    named_free = resource(nim_type="Download Available")
    assert nvidia_catalog.classify_resource(named_free, API_IDS) is None
    partner = resource(nim_type="Partner Endpoint")
    assert nvidia_catalog.classify_resource(partner, API_IDS) is None


def test_free_endpoint_missing_from_api_is_unavailable_and_blocked():
    model = nvidia_catalog.classify_resource(resource(), ["someone-else/other-model"])
    assert model is not None
    assert model["api_available"] is False
    assert model["verification_status"] == "unavailable"

    entry = discovered_model(
        api_available=False,
        verification_status="unavailable",
        verification_message="not in API catalog",
    )
    verdict = FreeOnlyPolicy(PolicyConfig()).evaluate(entry, "content_analysis")
    assert verdict.allowed is False
    assert verdict.category == ErrorCategory.MODEL_UNAVAILABLE


def test_catalog_available_false_is_unavailable_even_when_api_listed():
    model = nvidia_catalog.classify_resource(resource(available="false"), API_IDS)
    assert model is not None
    assert model["api_available"] is False
    assert model["verification_status"] == "unavailable"
    assert "unavailable" in model["verification_message"].lower()


def test_failed_smoke_test_blocks_model():
    entry = discovered_model(verification_status="failed")
    verdict = FreeOnlyPolicy(PolicyConfig()).evaluate(entry, "content_analysis")
    assert verdict.allowed is False
    assert verdict.category == ErrorCategory.MODEL_UNAVAILABLE


def test_partner_download_and_unknown_models_are_never_free_endpoints():
    assert nvidia_catalog.classify_resource(resource(nim_type="Partner Endpoint"), API_IDS) is None
    assert (
        nvidia_catalog.classify_resource(resource(nim_type="Download Available"), API_IDS) is None
    )
    assert nvidia_catalog.classify_resource(resource(nim_type="Something Else"), API_IDS) is None


def test_match_requires_owner_match_and_avoids_ambiguous_names():
    unmatched_owner = resource(publisher="other-publisher")
    assert nvidia_catalog.match_api_model(unmatched_owner, API_IDS) is None

    ambiguous = ["nvidia/llama-3-1-8b-instruct", "meta/llama-3-1-8b-instruct"]
    assert nvidia_catalog.match_api_model(resource(publisher=""), ambiguous) is None


def test_non_text_free_endpoint_is_not_routable():
    model = nvidia_catalog.classify_resource(
        resource(name="flux-image", general=("Image Generation",)), API_IDS
    )
    assert model is not None
    assert model["tasks"] == []
    assert model["status"] == "disabled"


def test_discovery_writes_cache_and_serves_fresh_cache(monkeypatch, tmp_path):
    calls = {"catalog": 0, "api": 0}

    def fake_catalog(**_kwargs):
        calls["catalog"] += 1
        return [resource(), resource(name="kimi-k3-1-pro", score=20.0)]

    def fake_models(**_kwargs):
        calls["api"] += 1
        return list(API_IDS)

    monkeypatch.setattr(nvidia_catalog, "fetch_catalog", fake_catalog)
    monkeypatch.setattr(nvidia_catalog, "fetch_api_models", fake_models)

    first = nvidia_catalog.discover(tmp_path, force=True)
    assert first["ok"] is True
    assert first["models"] == 2
    assert first["stats"]["free_endpoints"] == 2
    assert (tmp_path / "local" / nvidia_catalog.CACHE_FILENAME).exists()

    monkeypatch.setattr(
        nvidia_catalog,
        "fetch_catalog",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("network call")),
    )
    second = nvidia_catalog.discover(tmp_path)
    assert second["cached"] is True
    assert calls == {"catalog": 1, "api": 1}


def test_manager_discovers_and_registry_merges_catalog_models(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    shutil.copytree(PROJECT_ROOT / "config", config_dir)
    settings = Settings(
        config_dir=config_dir,
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    settings.ensure_dirs()
    manager = ProviderManager(
        settings=settings,
        registry=Registry.load(config_dir),
        usage=UsageTracker(settings.usage_db_path),
        providers={},
        throttle=Throttle(),
        sleep_fn=lambda _: None,
    )
    monkeypatch.setattr(nvidia_catalog, "fetch_catalog", lambda **_kwargs: [resource()])
    monkeypatch.setattr(nvidia_catalog, "fetch_api_models", lambda **_kwargs: list(API_IDS))

    summary = manager.discover_nvidia_models(force=True)
    assert summary["ok"] is True
    assert summary["models"] == 1
    assert manager.registry.get_model("nvidia-nvidia-llama-3-1-8b-instruct") is not None
    nvidia_provider = next(
        provider for provider in manager.list_providers() if provider["id"] == "nvidia"
    )
    assert nvidia_provider["catalog"]["models"] == 1

    models = manager.list_models()
    nvidia = next(model for model in models if model["provider"] == "nvidia")
    assert nvidia["ready"] is False
    assert nvidia["api_available"] == "unknown"
    assert nvidia["verification_status"] == "unverified"

    model_id = "nvidia-nvidia-llama-3-1-8b-instruct"
    manager.registry.update_verification(
        model_id,
        free_verified=True,
        verification_source="test-smoke",
        api_available=True,
        verification_status="verified",
    )
    manager.discover_nvidia_models(force=True)
    refreshed = manager.registry.require_model(model_id)
    assert refreshed.free_verified is True
    assert refreshed.api_available is True
    assert refreshed.verification_status == "verified"
    assert refreshed.verification_source == "test-smoke"


def test_manager_verifies_discovered_model_after_smoke_test(tmp_path):
    registry = make_registry(
        providers={"nvidia": ProviderConfig(type="openai_compatible", enabled=True)},
        models=[discovered_model()],
        routing={},
        config_dir=tmp_path / "config",
    )
    manager = make_manager(registry, {"nvidia": FakeTextProvider("nvidia")}, tmp_path)

    result = manager.verify_free("nvidia-test")
    assert result["verified"] is True
    assert result["api_available"] is True
    assert result["verification_status"] == "verified"
    updated = registry.require_model("nvidia-test")
    assert updated.free_verified is True
    assert updated.verification_status == "verified"


def test_manager_marks_failed_nvidia_smoke_test_unavailable(tmp_path):
    class FailingNvidia(FakeTextProvider):
        def test_connection(self, entry: ModelEntry) -> ConnectionResult:
            return ConnectionResult(
                ok=False,
                provider="nvidia",
                model=entry.id,
                message="model not found",
                details={"category": ErrorCategory.MODEL_UNAVAILABLE.value},
            )

    registry = make_registry(
        providers={"nvidia": ProviderConfig(type="openai_compatible", enabled=True)},
        models=[discovered_model()],
        routing={},
        config_dir=tmp_path / "config",
    )
    manager = make_manager(registry, {"nvidia": FailingNvidia("nvidia")}, tmp_path)

    result = manager.verify_free("nvidia-test")
    assert result["verified"] is False
    updated = registry.require_model("nvidia-test")
    assert updated.api_available is False
    assert updated.verification_status == "unavailable"


def test_router_appends_discovered_nvidia_models_after_configured_routes():
    providers = {
        "p1": ProviderConfig(type="litellm", enabled=True),
        "nvidia": ProviderConfig(type="openai_compatible", enabled=True),
    }
    models = [
        ModelEntry(
            id="free-1",
            provider="p1",
            provider_model_id="free-1",
            tasks=["content_analysis"],
            free=True,
            free_verified=True,
            status="enabled",
        ),
        discovered_model(
            id="nvidia-a",
            provider_model_id="nvidia/a",
            free_verified=True,
            catalog_score=10,
        ),
        discovered_model(
            id="nvidia-b",
            provider_model_id="nvidia/b",
            api_available=False,
            verification_status="unavailable",
        ),
    ]
    registry = make_registry(
        providers=providers,
        models=models,
        routing={"content_analysis": [RouteCandidate(provider="p1", model="free-1")]},
        policy=PolicyConfig(max_fallback_candidates=10),
    )
    result = Router(registry, FreeOnlyPolicy(registry.policy)).resolve("content_analysis")
    assert [candidate.entry.id for candidate in result.candidates] == ["free-1", "nvidia-a"]


def test_discovery_endpoint_is_nvidia_only(tmp_path):
    registry = make_registry(
        providers={"p1": ProviderConfig(type="litellm", enabled=True)},
        models=[text_model_for_api()],
        routing={},
    )
    manager = make_manager(registry, {"p1": FakeTextProvider("p1")}, tmp_path)
    client = TestClient(create_app(manager))

    response = client.post("/api/ai/providers/p1/discover")
    assert response.status_code == 400
    assert response.json()["error"]["category"] == "invalid_request"


def test_discovery_endpoint_invokes_manager(tmp_path, monkeypatch):
    registry = make_registry(
        providers={"p1": ProviderConfig(type="litellm", enabled=True)},
        models=[text_model_for_api()],
        routing={},
    )
    manager = make_manager(registry, {"p1": FakeTextProvider("p1")}, tmp_path)
    monkeypatch.setattr(
        manager,
        "discover_nvidia_models",
        lambda *, force=False: {"ok": True, "force": force, "models": 3},
    )
    client = TestClient(create_app(manager))

    response = client.post("/api/ai/providers/nvidia/discover", params={"force": False})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "force": False, "models": 3}


def test_free_llm_api_is_not_in_the_bundled_provider_list():
    providers = json.loads((PROJECT_ROOT / "config" / "providers.json").read_text())
    assert "freellmapi" not in providers["providers"]
    assert providers["providers"]["nvidia"]["type"] == "openai_compatible"


def text_model_for_api():
    return ModelEntry(
        id="free-1",
        provider="p1",
        provider_model_id="free-1",
        tasks=["content_analysis"],
        free=True,
        free_verified=True,
        status="enabled",
    )
