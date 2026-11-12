"""Configuration loading and the explicit free-model registry."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from clipping_ai.errors import AIError, ErrorCategory

logger = logging.getLogger(__name__)

ProviderType = Literal["litellm", "groq_speech", "whisper_cpp"]


class ConfigError(AIError):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCategory.INVALID_REQUEST, message, retryable=False)


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: ProviderType
    enabled: bool = True
    api_key_env: str | None = None
    litellm_prefix: str | None = None
    timeout_seconds: float | None = None
    max_retries: int | None = None
    min_interval_seconds: float = 0.0
    requests_per_minute: int = 0
    requests_per_day: int = 0
    max_audio_bytes: int = 25_000_000
    chunk_seconds: int = 600
    chunk_overlap_seconds: float = 0.0
    ffmpeg_binary: str = "ffmpeg"
    binary: str | None = None
    model_path: str | None = None
    extra_args: list[str] = Field(default_factory=list)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    notes: str | None = None


class ModelEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    provider: str
    provider_model_id: str
    litellm_model: str | None = None
    tasks: list[str] = Field(default_factory=list)
    free: bool = False
    free_verified: bool = False
    verified_at: str | None = None
    verification_source: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    input_modalities: list[str] = Field(default_factory=lambda: ["text"])
    output_modalities: list[str] = Field(default_factory=lambda: ["text"])
    supports_json: bool = False
    supports_word_timestamps: bool = False
    daily_request_limit: int = 0
    status: Literal["enabled", "disabled"] = "disabled"
    notes: str | None = None

    def supports_task(self, task: str) -> bool:
        return task in self.tasks

    def is_enabled(self) -> bool:
        return self.status == "enabled"

    @model_validator(mode="after")
    def _sane_limits(self) -> ModelEntry:
        if self.context_window is not None and self.context_window <= 0:
            raise ValueError("context_window must be positive")
        if self.daily_request_limit < 0:
            raise ValueError("daily_request_limit must be >= 0")
        return self


class RouteCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    provider: str
    model: str


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    require_verified_free: bool = False
    allow_unverified_free_if_manually_approved: bool = True
    fallback_on_rate_limit: bool = True
    fallback_on_quota_exhausted: bool = False
    fallback_on_provider_unavailable: bool = True
    fallback_on_model_unavailable: bool = True
    fallback_on_timeout: bool = True
    fallback_on_invalid_response: bool = True
    fallback_on_schema_validation: bool = True
    schema_validation_retries: int = 2
    max_fallback_candidates: int = 3


class RetryConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    jitter_seconds: float = 0.5
    max_retry_after_seconds: float = 60.0


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Expected a JSON object in {path}")
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class Registry:
    """Loads providers, models, routing and policy from config/ with local overrides."""

    def __init__(
        self,
        config_dir: Path,
        *,
        providers: dict[str, ProviderConfig],
        models: dict[str, ModelEntry],
        local_models: dict[str, ModelEntry],
        routing: dict[str, list[RouteCandidate]],
        policy: PolicyConfig,
        retry: RetryConfig,
    ) -> None:
        self.config_dir = config_dir
        self.providers = providers
        self.models = models
        self._local_models = local_models
        self.routing = routing
        self.policy = policy
        self.retry = retry

    @classmethod
    def load(cls, config_dir: Path, *, require_verified_free: bool | None = None) -> Registry:
        config_dir = Path(config_dir)
        local_dir = config_dir / "local"

        providers_raw = _deep_merge(
            _read_json(config_dir / "providers.json"),
            _read_json(local_dir / "providers.json"),
        )
        models_raw = _read_json(config_dir / "models.json")
        local_models_raw = _read_json(local_dir / "models.json")
        routing_raw = _deep_merge(
            _read_json(config_dir / "routing.json"),
            _read_json(local_dir / "routing.json"),
        )
        policy_raw = _deep_merge(
            _read_json(config_dir / "policy.json"),
            _read_json(local_dir / "policy.json"),
        )

        try:
            providers = {
                pid: ProviderConfig.model_validate(cfg)
                for pid, cfg in (providers_raw.get("providers") or {}).items()
            }
        except ValidationError as exc:
            raise ConfigError(f"Invalid provider configuration: {exc}") from exc

        try:
            models = {
                entry.id: entry
                for entry in (
                    ModelEntry.model_validate(item) for item in (models_raw.get("models") or [])
                )
            }
            local_models = {
                entry.id: entry
                for entry in (
                    ModelEntry.model_validate(item) for item in (local_models_raw.get("models") or [])
                )
            }
        except ValidationError as exc:
            raise ConfigError(f"Invalid model registry: {exc}") from exc

        models.update(local_models)

        try:
            routing = {
                task: [RouteCandidate.model_validate(item) for item in candidates]
                for task, candidates in (routing_raw.get("tasks") or {}).items()
            }
        except ValidationError as exc:
            raise ConfigError(f"Invalid routing configuration: {exc}") from exc

        try:
            policy = PolicyConfig.model_validate(policy_raw.get("policy") or {})
            retry = RetryConfig.model_validate(policy_raw.get("retry") or {})
        except ValidationError as exc:
            raise ConfigError(f"Invalid policy configuration: {exc}") from exc

        if require_verified_free is not None:
            policy.require_verified_free = require_verified_free

        for entry in models.values():
            if entry.provider not in providers:
                logger.warning("Model %s references unknown provider %s", entry.id, entry.provider)

        return cls(
            config_dir=config_dir,
            providers=providers,
            models=models,
            local_models=local_models,
            routing=routing,
            policy=policy,
            retry=retry,
        )

    # ------------------------------------------------------------------ queries
    def get_model(self, model_id: str) -> ModelEntry | None:
        return self.models.get(model_id)

    def require_model(self, model_id: str) -> ModelEntry:
        entry = self.get_model(model_id)
        if entry is None:
            raise ConfigError(f"Model '{model_id}' is not present in the registry.")
        return entry

    def get_provider(self, provider_id: str) -> ProviderConfig | None:
        return self.providers.get(provider_id)

    def require_provider(self, provider_id: str) -> ProviderConfig:
        cfg = self.get_provider(provider_id)
        if cfg is None:
            raise ConfigError(f"Provider '{provider_id}' is not configured.")
        return cfg

    def candidates_for_task(self, task: str) -> list[RouteCandidate]:
        return list(self.routing.get(task, []))

    def models_for_provider(self, provider_id: str) -> list[ModelEntry]:
        return [m for m in self.models.values() if m.provider == provider_id]

    def free_approved_models(self, task: str | None = None) -> list[ModelEntry]:
        result = []
        for entry in self.models.values():
            if not entry.free or not entry.is_enabled():
                continue
            if task and not entry.supports_task(task):
                continue
            result.append(entry)
        return result

    # ------------------------------------------------------- verification writes
    def update_verification(
        self,
        model_id: str,
        *,
        free_verified: bool,
        verification_source: str | None,
        verified_at: str | None = None,
        free: bool | None = None,
    ) -> ModelEntry:
        entry = self.require_model(model_id)
        if free is not None:
            entry.free = free
        entry.free_verified = free_verified
        entry.verification_source = verification_source
        entry.verified_at = verified_at or date.today().isoformat()
        self.models[model_id] = entry
        self._local_models[model_id] = entry
        self._persist_local_models()
        return entry

    def set_model_status(self, model_id: str, status: Literal["enabled", "disabled"]) -> ModelEntry:
        entry = self.require_model(model_id)
        entry.status = status
        self.models[model_id] = entry
        self._local_models[model_id] = entry
        self._persist_local_models()
        return entry

    def _persist_local_models(self) -> None:
        local_dir = self.config_dir / "local"
        local_dir.mkdir(parents=True, exist_ok=True)
        payload = {"models": [m.model_dump(mode="json") for m in self._local_models.values()]}
        with (local_dir / "models.json").open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
