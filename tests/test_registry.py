from __future__ import annotations

import json
import shutil
from pathlib import Path

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.manager import ProviderManager
from clipping_ai.registry import Registry
from clipping_ai.settings import Settings
from clipping_ai.usage import UsageTracker
from tests.helpers import make_manager

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_real_config_loads_expected_registry():
    registry = Registry.load(PROJECT_ROOT / "config")
    assert len(registry.models) == 11
    assert registry.get_model("gemini-3.8-flash").free is True
    assert registry.get_model("openrouter-example-free-model").free is False
    assert registry.get_model("openrouter-example-free-model").status == "disabled"
    assert len(registry.free_approved_models()) == 10


def test_settings_reads_env_style_values(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "  test-key  ")
    monkeypatch.setenv("CLIPPING_AI_DATA_DIR", str(tmp_path / "data"))
    settings = Settings(_env_file=None)
    assert settings.get_secret("GEMINI_API_KEY") == "test-key"
    assert settings.has_secret("GEMINI_API_KEY") is True
    assert settings.data_dir == tmp_path / "data"


def test_relative_storage_paths_resolve_once_at_startup(monkeypatch, tmp_path):
    monkeypatch.delenv("CLIPPING_AI_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLIPPING_AI_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        config_dir=Path("config"),
        data_dir=Path("data"),
        _env_file=None,
    )
    assert settings.config_dir == (tmp_path / "config").resolve()
    assert settings.data_dir == (tmp_path / "data").resolve()
    monkeypatch.chdir(tmp_path.parent)
    assert settings.config_dir == (tmp_path / "config").resolve()
    assert settings.data_dir == (tmp_path / "data").resolve()


def test_verification_is_persisted_to_local_config(tmp_path, monkeypatch):
    config = tmp_path / "config"
    shutil.copytree(PROJECT_ROOT / "config", config)
    registry = Registry.load(config)

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {
                        "id": "openrouter/free",
                        "pricing": {"prompt": "0", "completion": "0"},
                    }
                ]
            }

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: FakeResponse())
    settings = Settings(
        config_dir=config,
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    manager = ProviderManager(
        settings=settings,
        registry=registry,
        usage=UsageTracker(settings.usage_db_path),
        providers={},
    )
    result = manager.verify_free("openrouter-free-router")
    assert result["verified"] is True
    assert result["supported"] is True
    local = json.loads((config / "local" / "models.json").read_text(encoding="utf-8"))
    saved = {item["id"]: item for item in local["models"]}
    assert saved["openrouter-free-router"]["free_verified"] is True


def test_unknown_model_verification_raises(tmp_path):
    registry = Registry.load(PROJECT_ROOT / "config")
    manager = make_manager(registry, {}, tmp_path)
    try:
        manager.verify_free("does-not-exist")
    except AIError as exc:
        assert exc.category == ErrorCategory.INVALID_REQUEST
    else:
        raise AssertionError("expected AIError")
