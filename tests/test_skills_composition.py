from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipping_ai.api.app import create_app
from clipping_ai.clips.config import ClipConfig
from clipping_ai.clips.editing import EditPlan, Effect
from clipping_ai.composition.engine import build_composition_plan
from clipping_ai.composition.models import VisualSettings
from clipping_ai.composition.overlays import write_overlay_ass
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.highlights.models import Highlight
from clipping_ai.manager import ProviderManager
from clipping_ai.providers.openai_compatible import OpenAICompatibleProvider
from clipping_ai.registry import ModelEntry, ProviderConfig, Registry
from clipping_ai.settings import Settings
from clipping_ai.skills.executor import SkillExecutor
from clipping_ai.skills.registry import SkillRegistry
from clipping_ai.skills.tools import ToolSpec
from tests.helpers import make_manager, make_registry, text_model

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _highlight(title: str = "Three key steps", confidence: float = 0.9) -> Highlight:
    return Highlight(
        start_time=0.0,
        end_time=10.0,
        title=title,
        description="desc",
        selection_reason="reason",
        confidence=confidence,
        category="general",
    )


def test_visual_settings_defaults_and_intensity_scaling():
    subtle = VisualSettings(intensity="subtle")
    assert subtle.max_effects == 1
    assert subtle.intensity_scale() == pytest.approx(0.6)
    assert subtle.category_enabled("zoom") is True
    assert subtle.category_enabled("unknown") is True
    strong = VisualSettings(intensity="strong", zoom=False)
    assert strong.max_effects == 3
    assert strong.category_enabled("zoom") is False


def test_composition_plan_caps_effects_and_adds_one_callout():
    plan = build_composition_plan(
        EditPlan(
            effects=(
                Effect("zoom", 0.5, 1.5, 0.2),
                Effect("punch", 2.0, 2.4, 0.2),
                Effect("fade_in", 0.0, 0.2, 0.2),
            )
        ),
        highlight=_highlight(),
        start_time=0.0,
        end_time=10.0,
        settings=VisualSettings(intensity="subtle"),
        config=ClipConfig(),
    )
    assert [effect.kind for effect in plan.effects] == ["zoom", "fade_in"]
    assert plan.effects[0].intensity == pytest.approx(0.12)
    assert len(plan.callouts) == 1
    assert plan.callouts[0].position == "top"
    assert plan.to_dict()["settings"]["intensity"] == "subtle"


def test_disabling_visuals_keeps_empty_composition_plan():
    plan = build_composition_plan(
        EditPlan(effects=(Effect("zoom", 0.0, 1.0, 0.2),)),
        highlight=_highlight(),
        start_time=0.0,
        end_time=10.0,
        settings=VisualSettings(visual_enhancement=False),
        config=ClipConfig(),
    )
    assert plan.effects == ()
    assert plan.callouts == ()


def test_overlay_ass_uses_libass_arabic_style(tmp_path):
    settings = VisualSettings()
    plan = build_composition_plan(
        EditPlan(),
        highlight=_highlight("خطوة مهمة"),
        start_time=0.0,
        end_time=10.0,
        settings=settings,
        config=ClipConfig(),
    )
    output = write_overlay_ass(
        plan.callouts,
        output_path=tmp_path / "callouts.ass",
        width=1080,
        height=1920,
        config=ClipConfig(),
    )
    assert output is not None
    text = output.read_text(encoding="utf-8")
    assert "OverlayArabic" in text
    assert "خطوة مهمة" in text
    assert "\\an8" in text


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class _FakeClient:
    def __init__(self, calls: list[dict], payload: dict) -> None:
        self.calls = calls
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, path, *, json, headers):
        self.calls.append({"path": path, "json": json, "headers": headers})
        return _FakeResponse(self.payload)


def test_openai_compatible_provider_posts_to_configured_gateway():
    calls: list[dict] = []
    config = ProviderConfig(
        type="openai_compatible",
        enabled=True,
        base_url="https://gateway.example/v1/",
        api_key_env="GATEWAY_API_KEY",
    )
    provider = OpenAICompatibleProvider(
        "gateway",
        config,
        secret_getter=lambda name: "secret-key",
        client_factory=lambda **kwargs: _FakeClient(
            calls,
            {
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        ),
    )
    entry = ModelEntry(
        id="free-1",
        provider="gateway",
        provider_model_id="free-1",
        free=True,
        status="enabled",
    )
    result = provider.complete(entry, [{"role": "user", "content": "hi"}])
    assert result.text == "OK"
    assert result.input_tokens == 3
    assert calls[0]["path"] == "/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer secret-key"
    assert calls[0]["json"]["model"] == "free-1"


def test_openai_compatible_provider_requires_base_url():
    provider = OpenAICompatibleProvider(
        "gateway",
        ProviderConfig(type="openai_compatible", enabled=True),
        secret_getter=lambda name: None,
    )
    assert provider.available() == (False, "base_url is not configured")
    entry = ModelEntry(
        id="free-1",
        provider="gateway",
        provider_model_id="free-1",
        free=True,
        status="enabled",
    )
    with pytest.raises(AIError) as excinfo:
        provider.complete(entry, [{"role": "user", "content": "hi"}])
    assert excinfo.value.category == ErrorCategory.PROVIDER_UNAVAILABLE


def _external_manifest(skill_id: str = "external-skill") -> dict:
    return {
        "id": skill_id,
        "name": "External",
        "description": "data only",
        "tools": ["composition.callout_plan"],
        "enabled_by_default": True,
    }


def test_external_skills_require_review_before_enable(tmp_path):
    config_dir = tmp_path / "config"
    shutil.copytree(PROJECT_ROOT / "config", config_dir)
    external = config_dir / "skills"
    external.mkdir()
    (external / "external.json").write_text(json.dumps(_external_manifest()), encoding="utf-8")
    registry = SkillRegistry(config_dir)
    record = registry.require("external-skill")
    assert record.source == "external"
    assert record.enabled is False
    assert record.reviewed is False
    with pytest.raises(AIError) as excinfo:
        registry.set_enabled("external-skill", True)
    assert excinfo.value.category == ErrorCategory.POLICY_VIOLATION
    registry.review("external-skill")
    registry.set_enabled("external-skill", True)
    reloaded = SkillRegistry(config_dir).require("external-skill")
    assert reloaded.enabled is True
    assert reloaded.reviewed is True


def test_skill_executor_runs_declared_builtin_tool(tmp_path):
    config_dir = tmp_path / "config"
    shutil.copytree(PROJECT_ROOT / "config", config_dir)
    registry = SkillRegistry(config_dir)
    result = SkillExecutor(registry).execute("smart-captions", {"target_language": "ar"})
    assert result["skill"] == "smart-captions"
    assert result["results"][0]["target_language"] == "ar"


def test_skill_executor_enforces_timeout(tmp_path):
    import time

    config_dir = tmp_path / "config"
    shutil.copytree(PROJECT_ROOT / "config", config_dir)
    external = config_dir / "skills"
    external.mkdir(exist_ok=True)
    manifest = _external_manifest("slow-skill")
    manifest["tools"] = ["custom.slow"]
    manifest["timeout_seconds"] = 0.05
    (external / "slow.json").write_text(json.dumps(manifest), encoding="utf-8")

    def slow(_context):
        time.sleep(0.3)
        return {}

    registry = SkillRegistry(config_dir)
    registry.tools.register(ToolSpec("custom.slow", "slow", slow))
    registry.review("slow-skill")
    registry.set_enabled("slow-skill", True)
    with pytest.raises(AIError) as excinfo:
        SkillExecutor(registry).execute("slow-skill", {})
    assert excinfo.value.category == ErrorCategory.NETWORK_TIMEOUT


def test_provider_toggle_persists_to_local_config(tmp_path):
    config_dir = tmp_path / "config"
    shutil.copytree(PROJECT_ROOT / "config", config_dir)
    registry = Registry.load(config_dir)
    registry.set_provider_enabled("gemini", False)
    assert Registry.load(config_dir).get_provider("gemini").enabled is False


def test_skills_api_lists_and_toggles_builtin(tmp_path):
    registry = make_registry(
        providers={"p1": ProviderConfig(type="litellm", enabled=True)},
        models=[text_model("free-1", provider="p1")],
        routing={"content_analysis": []},
    )
    manager = make_manager(registry, {}, tmp_path)
    client = TestClient(create_app(manager, start_worker=False))
    listed = client.get("/api/skills")
    assert listed.status_code == 200
    ids = {skill["id"] for skill in listed.json()["skills"]}
    assert {"smart-captions", "moment-emphasis", "visual-callouts"} <= ids
    toggled = client.patch("/api/skills/smart-captions", json={"enabled": False})
    assert toggled.status_code == 200
    assert toggled.json()["skill"]["enabled"] is False
    executed = client.post("/api/skills/visual-callouts/execute", json={"context": {}})
    assert executed.status_code == 200
    assert executed.json()["results"][0]["tool"] == "composition.callout_plan"


def test_provider_toggle_api_persists_enablement(tmp_path):
    config_dir = tmp_path / "config"
    shutil.copytree(PROJECT_ROOT / "config", config_dir)
    settings = Settings(config_dir=config_dir, data_dir=tmp_path / "data", _env_file=None)
    settings.ensure_dirs()
    manager = ProviderManager.from_config(settings)
    client = TestClient(create_app(manager, start_worker=False))
    response = client.patch("/api/ai/providers/gemini", json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["provider"]["enabled"] is False
    assert Registry.load(config_dir).get_provider("gemini").enabled is False
