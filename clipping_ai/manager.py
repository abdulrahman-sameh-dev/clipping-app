"""Central provider manager: selection, policy, retries, fallback and tracking."""

from __future__ import annotations

import logging
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from clipping_ai import __version__
from clipping_ai.audio import AudioChunk
from clipping_ai.errors import AIError, ErrorCategory, missing_key
from clipping_ai.jobs import TranscriptionCache
from clipping_ai.policy import FreeOnlyPolicy
from clipping_ai.providers import nvidia_catalog
from clipping_ai.providers.base import (
    BaseSpeechProvider,
    BaseTextProvider,
    ConnectionResult,
    TextCompletion,
)
from clipping_ai.providers.groq_speech import GroqSpeechProvider
from clipping_ai.providers.litellm_text import LitellmTextProvider
from clipping_ai.providers.openai_compatible import OpenAICompatibleProvider
from clipping_ai.providers.whisper_cpp import WhisperCppProvider
from clipping_ai.recovery.providers import ProviderRuntimeStore
from clipping_ai.registry import ModelEntry, Registry
from clipping_ai.retry import compute_delay
from clipping_ai.routing import Router, RoutingResult
from clipping_ai.schemas import (
    TEXT_TASKS,
    TranscriptResult,
    schema_for_task,
)
from clipping_ai.settings import Settings, get_settings
from clipping_ai.structured import (
    build_messages,
    parse_and_validate,
    repair_messages,
    response_format_for,
)
from clipping_ai.throttle import Throttle
from clipping_ai.usage import UsageTracker

logger = logging.getLogger(__name__)


@dataclass
class Attempt:
    provider: str
    model: str | None
    success: bool
    category: str | None = None
    message: str | None = None
    latency_ms: int | None = None


@dataclass
class TextExecution:
    task: str
    provider: str
    model: str
    data: BaseModel | str
    raw: str
    attempts: list[Attempt] = field(default_factory=list)
    used_fallback: bool = False


@dataclass
class SpeechExecution:
    task: str
    provider: str
    model: str
    result: TranscriptResult
    attempts: list[Attempt] = field(default_factory=list)
    used_fallback: bool = False


class ProviderManager:
    def __init__(
        self,
        *,
        settings: Settings,
        registry: Registry,
        usage: UsageTracker,
        providers: dict[str, BaseTextProvider | BaseSpeechProvider],
        throttle: Throttle | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        runtime_store: ProviderRuntimeStore | None = None,
    ) -> None:
        self.settings = settings
        self.runtime = runtime_store or ProviderRuntimeStore(settings.jobs_db_path)
        self.registry = registry
        self.usage = usage
        self.providers = providers
        self.policy = FreeOnlyPolicy(registry.policy)
        self.router = Router(registry, self.policy)
        self.throttle = throttle or Throttle()
        self._sleep = sleep_fn
        self._rng = rng or random.Random()
        self._health: dict[str, ConnectionResult] = {}
        self._quota_blocked_until: dict[str, float] = {}
        self._provider_events: dict[str, dict[str, Any]] = {}

    # --------------------------------------------------------------- construction
    @classmethod
    def from_config(
        cls,
        settings: Settings | None = None,
        *,
        registry: Registry | None = None,
        usage: UsageTracker | None = None,
        provider_overrides: dict[str, BaseTextProvider | BaseSpeechProvider] | None = None,
        throttle: Throttle | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> ProviderManager:
        settings = settings or get_settings()
        settings.ensure_dirs()
        registry = registry or Registry.load(
            settings.config_dir, require_verified_free=settings.require_verified_free
        )
        usage = usage or UsageTracker(settings.usage_db_path)
        cache = TranscriptionCache(settings.jobs_db_path)

        providers: dict[str, BaseTextProvider | BaseSpeechProvider] = {}
        for provider_id, config in registry.providers.items():
            if config.type == "litellm":
                providers[provider_id] = LitellmTextProvider(
                    provider_id, config, secret_getter=settings.get_secret
                )
            elif config.type == "openai_compatible":
                providers[provider_id] = OpenAICompatibleProvider(
                    provider_id, config, secret_getter=settings.get_secret
                )
            elif config.type == "groq_speech":
                providers[provider_id] = GroqSpeechProvider(
                    provider_id, config, secret_getter=settings.get_secret, cache=cache
                )
            elif config.type == "whisper_cpp":
                if settings.whisper_cpp_binary and not config.binary:
                    config.binary = settings.whisper_cpp_binary
                if settings.whisper_cpp_model_path and not config.model_path:
                    config.model_path = settings.whisper_cpp_model_path
                providers[provider_id] = WhisperCppProvider(provider_id, config)

        if provider_overrides:
            providers.update(provider_overrides)

        return cls(
            settings=settings,
            registry=registry,
            usage=usage,
            providers=providers,
            throttle=throttle,
            sleep_fn=sleep_fn,
            rng=rng,
        )

    def reload(self) -> None:
        """Reload registry/routing/policy in place (providers are rebuilt)."""
        fresh = ProviderManager.from_config(
            self.settings,
            throttle=self.throttle,
            sleep_fn=self._sleep,
            rng=self._rng,
        )
        blocked = self._quota_blocked_until
        events = self._provider_events
        runtime = self.runtime
        self.__dict__.update(fresh.__dict__)
        self._quota_blocked_until = blocked
        self._provider_events = events
        self.runtime = runtime

    # ----------------------------------------------------------- quota cooldown
    @staticmethod
    def _quota_block_seconds(error: AIError) -> float:
        if error.retry_after is not None and error.retry_after > 0:
            return max(1.0, min(float(error.retry_after), 86400.0))
        message = error.message.lower()
        daily_markers = (
            "per day",
            "daily",
            "day limit",
            "requests per day",
            "rpd",
        )
        if any(marker in message for marker in daily_markers):
            now = datetime.now(UTC)
            reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return max(1.0, (reset - now).total_seconds())
        return 3600.0

    def _block_provider_quota(self, provider: str, error: AIError) -> None:
        until = time.time() + self._quota_block_seconds(error)
        self._quota_blocked_until[provider] = max(
            until, self._quota_blocked_until.get(provider, 0.0)
        )
        self.runtime.set_cooldown(
            provider,
            until=self._quota_blocked_until[provider],
            reason="free_quota_exhausted",
            category=error.category.value,
            quota_reset_at=until,
        )

    def _provider_cooldown(self, provider: str) -> float:
        until = self._quota_blocked_until.get(provider, 0.0)
        remaining = until - time.time()
        if remaining <= 0:
            self._quota_blocked_until.pop(provider, None)
            persisted = self.runtime.cooldown_remaining(provider)
            if persisted > 0:
                self._quota_blocked_until[provider] = time.time() + persisted
                return persisted
            return 0.0
        return remaining

    def _candidate_cooldown(self, entry: ModelEntry) -> tuple[str, float]:
        """Return the blocking provider and remaining cooldown for an entry.

        Gateway models can declare an underlying direct provider. If that
        provider is cooling down, the gateway route is skipped conservatively
        because the gateway would likely hit the same free quota.
        """
        remaining = self._provider_cooldown(entry.provider)
        if remaining > 0:
            return entry.provider, remaining
        underlying = entry.underlying_provider
        if underlying:
            remaining = self._provider_cooldown(underlying)
            if remaining > 0:
                return underlying, remaining
        return "", 0.0

    def _record_provider_event(
        self,
        provider: str,
        *,
        success: bool,
        category: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        event = self._provider_events.setdefault(provider, {})
        stamp = datetime.now(UTC).isoformat()
        if success:
            event["last_success"] = stamp
            self.runtime.record_success(provider)
        else:
            event["last_failure"] = stamp
            event["last_error_category"] = category
            if category is not None:
                self.runtime.record_failure(
                    provider,
                    category=category,
                    retry_after_seconds=retry_after_seconds,
                )

    # ------------------------------------------------------------------- discovery
    def list_providers(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        cached_nvidia = nvidia_catalog.load_cache(self.settings.config_dir)
        for provider_id, config in self.registry.providers.items():
            provider = self.providers.get(provider_id)
            if provider is None:
                available, reason, status = False, "adapter missing", "unavailable"
            elif isinstance(provider, WhisperCppProvider):
                health = provider.inspect()
                available = health.ok
                reason = health.message
                status = str(health.details.get("status", "unavailable"))
            else:
                available, reason = provider.available()
                status = "ready" if available else "unavailable"
            if not config.enabled:
                status = "disabled"
            env_var = config.api_key_env
            free_models = [m for m in self.registry.models_for_provider(provider_id) if m.free]
            free_enabled = [m for m in free_models if m.is_enabled()]
            event = self._provider_events.get(provider_id, {})
            persisted = self.runtime.get(provider_id)
            item = {
                "id": provider_id,
                "type": config.type,
                "enabled": config.enabled,
                "gateway": config.gateway,
                "base_url_configured": bool(config.base_url),
                "free_models": len(free_models),
                "free_models_enabled": len(free_enabled),
                "cooldown_seconds": round(self._provider_cooldown(provider_id), 1),
                "last_success": event.get("last_success")
                or (persisted.last_success_at if persisted else None),
                "last_failure": event.get("last_failure")
                or (persisted.last_failure_at if persisted else None),
                "last_error_category": event.get("last_error_category")
                or (persisted.last_failure_category if persisted else None),
                "configured": available,
                "status": status,
                "key_present": bool(env_var and self.settings.has_secret(env_var)),
                "api_key_env": env_var,
                "capabilities": _capabilities_for_type(config.type),
                "models": len(self.registry.models_for_provider(provider_id)),
                "reason": reason,
                "last_health": _connection_dict(self._health.get(provider_id)),
            }
            if provider_id == "nvidia":
                item["catalog"] = {
                    "updated_at": cached_nvidia.get("updated_at") if cached_nvidia else None,
                    "models": len(cached_nvidia.get("models") or []) if cached_nvidia else 0,
                    "stats": cached_nvidia.get("stats") if cached_nvidia else {},
                }
            result.append(item)
        return result

    def list_models(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for entry in self.registry.models.values():
            provider = self.providers.get(entry.provider)
            available, reason = provider.available() if provider else (False, "adapter missing")
            eligibility = {
                task: {
                    "allowed": (verdict := self.policy.evaluate(entry, task)).allowed,
                    "reason": verdict.reason,
                }
                for task in entry.tasks
            }
            ready = (
                entry.free
                and entry.is_enabled()
                and available
                and entry.api_available is not False
                and entry.verification_status == "verified"
            )
            result.append(
                {
                    **entry.model_dump(mode="json"),
                    "provider_available": available,
                    "provider_reason": reason,
                    "free_approved": entry.free and entry.is_enabled(),
                    "ready": ready,
                    "eligibility": eligibility,
                }
            )
        return result

    def status(self, *, include_health: bool = False) -> dict[str, Any]:
        if include_health:
            self.health_check()
        providers = self.list_providers()
        free_models = [m for m in self.registry.models.values() if m.free and m.is_enabled()]
        ready = any(p["configured"] and p["models"] > 0 for p in providers) and bool(free_models)
        return {
            "ready": ready,
            "version": __version__,
            "local_only": True,
            "policy": self.policy.describe(),
            "providers": providers,
            "models": {
                "total": len(self.registry.models),
                "free_approved": len(free_models),
                "enabled": sum(1 for m in self.registry.models.values() if m.is_enabled()),
            },
            "usage": self.usage.totals(),
            "paths": {
                "config_dir": str(self.settings.config_dir),
                "data_dir": str(self.settings.data_dir),
            },
            "notes": [
                "Local usage counts are not provider-reported remaining quota.",
                "Only models explicitly approved as free can execute.",
            ],
        }

    def health_check(self) -> dict[str, ConnectionResult]:
        for provider_id, _config in self.registry.providers.items():
            provider = self.providers.get(provider_id)
            if provider is None:
                continue
            if isinstance(provider, WhisperCppProvider):
                self._health[provider_id] = provider.health(self._first_free_model(provider_id))
                continue
            available, reason = provider.available()
            if not available:
                self._health[provider_id] = ConnectionResult(
                    ok=False,
                    provider=provider_id,
                    model=None,
                    message=reason,
                    details={"category": ErrorCategory.PROVIDER_UNAVAILABLE.value},
                )
                continue
            entry = self._first_free_model(provider_id)
            if entry is None:
                self._health[provider_id] = ConnectionResult(
                    ok=False,
                    provider=provider_id,
                    model=None,
                    message="no free model registered for this provider",
                    details={"category": ErrorCategory.POLICY_VIOLATION.value},
                )
                continue
            try:
                self.policy.assert_allowed(
                    entry, entry.tasks[0] if entry.tasks else "content_analysis"
                )
                self.policy.assert_free(entry)
            except AIError as exc:
                self._health[provider_id] = ConnectionResult(
                    ok=False,
                    provider=provider_id,
                    model=entry.id,
                    message=exc.message,
                    details={"category": exc.category.value},
                )
                continue
            self._health[provider_id] = provider.test_connection(entry)
        return dict(self._health)

    # -------------------------------------------------------------- connection test
    def test_connection(self, provider_id: str, model_id: str | None = None) -> dict[str, Any]:
        config = self.registry.get_provider(provider_id)
        if config is None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Unknown provider '{provider_id}'.",
                retryable=False,
            )
        provider = self.providers.get(provider_id)
        if provider is None:
            raise AIError(
                ErrorCategory.PROVIDER_UNAVAILABLE,
                f"Provider '{provider_id}' has no adapter.",
                provider=provider_id,
                retryable=False,
            )

        if isinstance(provider, WhisperCppProvider):
            entry = self._resolve_entry(provider_id, model_id)
            if entry is None:
                entry = self._first_free_model(provider_id)
            result = provider.health(entry)
            self._health[provider_id] = result
            return _connection_dict(result) or {}

        available, reason = provider.available()
        if not available:
            env_var = config.api_key_env or ""
            category = (
                ErrorCategory.MISSING_API_KEY
                if env_var and not self.settings.has_secret(env_var)
                else ErrorCategory.PROVIDER_UNAVAILABLE
            )
            result = ConnectionResult(
                ok=False,
                provider=provider_id,
                model=model_id,
                message=reason,
                details={"category": category.value},
            )
            self._health[provider_id] = result
            return _connection_dict(result) or {}

        entry = self._resolve_entry(provider_id, model_id)
        if entry is None:
            entry = self._first_free_model(provider_id)
        if entry is None:
            result = ConnectionResult(
                ok=False,
                provider=provider_id,
                model=model_id,
                message="No free model is registered for this provider; nothing was called.",
                details={"category": ErrorCategory.POLICY_VIOLATION.value},
            )
            self._health[provider_id] = result
            return _connection_dict(result) or {}

        self.policy.assert_allowed(entry, entry.tasks[0] if entry.tasks else "content_analysis")
        self.policy.assert_free(entry)

        started = time.perf_counter()
        try:
            result = provider.test_connection(entry)
        except AIError as exc:
            self._record(
                entry,
                task=entry.tasks[0] if entry.tasks else None,
                request_kind="connection",
                success=False,
                error_category=exc.category.value,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            self._health[provider_id] = ConnectionResult(
                ok=False,
                provider=provider_id,
                model=entry.id,
                message=exc.message,
                details={"category": exc.category.value},
            )
            if entry.provider == "nvidia":
                self._record_nvidia_verification(entry, self._health[provider_id])
            return _connection_dict(self._health[provider_id]) or {}

        details = result.details or {}
        self._record(
            entry,
            task=entry.tasks[0] if entry.tasks else None,
            request_kind="connection",
            success=result.ok,
            error_category=None if result.ok else details.get("category"),
            input_tokens=details.get("input_tokens"),
            output_tokens=details.get("output_tokens"),
            cost=details.get("reported_cost"),
            latency_ms=result.latency_ms,
        )
        self._health[provider_id] = result
        if entry.provider == "nvidia":
            self._record_nvidia_verification(entry, result)
        return _connection_dict(result) or {}

    # ------------------------------------------------------------------ text tasks
    def analyze(
        self,
        task: str,
        *,
        text: str,
        instructions: str | None = None,
        context: str | None = None,
        target_language: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        payload: dict[str, Any] | None = None,
        schema: type[BaseModel] | None = None,
    ) -> dict[str, Any]:
        if task not in TEXT_TASKS:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Task '{task}' is not a text task. Supported: {', '.join(TEXT_TASKS)}.",
                retryable=False,
            )
        if task == "translation" and not target_language:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "task='translation' requires target_language.",
                retryable=False,
            )
        body: dict[str, Any] = dict(payload or {})
        body.setdefault("text", text)
        if context:
            body.setdefault("context", context)
        if target_language:
            body.setdefault("target_language", target_language)

        execution = self.execute_text(
            task,
            body,
            instructions=instructions,
            provider=provider,
            model=model,
            schema=schema,
        )
        data = (
            execution.data.model_dump(mode="json")
            if isinstance(execution.data, BaseModel)
            else execution.data
        )
        return {
            "task": task,
            "provider": execution.provider,
            "model": execution.model,
            "used_fallback": execution.used_fallback,
            "data": data,
            "attempts": [attempt.__dict__ for attempt in execution.attempts],
        }

    def execute_text(
        self,
        task: str,
        payload: dict[str, Any],
        *,
        instructions: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        schema: type[BaseModel] | None = None,
    ) -> TextExecution:
        schema = schema or schema_for_task(task)
        routing = self.router.resolve(task, provider=provider, model=model)
        self._raise_if_no_candidates(routing, task, explicit=bool(provider or model))

        messages = build_messages(task, payload, schema=schema, instructions=instructions)
        response_format = None
        attempts: list[Attempt] = []
        last_error: AIError | None = None

        for index, candidate in enumerate(routing.candidates):
            entry = candidate.entry
            blocking_provider, cooldown = self._candidate_cooldown(entry)
            if cooldown > 0:
                last_error = AIError(
                    ErrorCategory.FREE_QUOTA_EXHAUSTED,
                    f"Provider '{blocking_provider}' is cooling down after free-quota "
                    f"exhaustion; retry in about {math.ceil(cooldown)}s.",
                    provider=entry.provider,
                    model=entry.id,
                    task=task,
                    retryable=False,
                    retry_after=cooldown,
                )
                attempts.append(
                    Attempt(
                        entry.provider,
                        entry.id,
                        False,
                        last_error.category.value,
                        last_error.message,
                    )
                )
                if provider or model:
                    raise last_error
                continue

            provider_obj = self.providers.get(entry.provider)
            if not isinstance(provider_obj, BaseTextProvider):
                last_error = AIError(
                    ErrorCategory.UNSUPPORTED_CAPABILITY,
                    f"Provider '{entry.provider}' is not a text provider.",
                    provider=entry.provider,
                    model=entry.id,
                    task=task,
                    retryable=False,
                )
                attempts.append(
                    Attempt(
                        entry.provider,
                        entry.id,
                        False,
                        last_error.category.value,
                        last_error.message,
                    )
                )
                if not self.policy.can_fallback(last_error):
                    raise last_error
                continue

            available, reason = provider_obj.available()
            if not available:
                last_error = _availability_error(entry.provider, reason, self.registry)
                attempts.append(
                    Attempt(
                        entry.provider,
                        entry.id,
                        False,
                        last_error.category.value,
                        last_error.message,
                    )
                )
                if provider or model:
                    raise last_error
                continue

            if schema is not None and not entry.supports_json:
                last_error = AIError(
                    ErrorCategory.UNSUPPORTED_CAPABILITY,
                    "Model does not support JSON output required by this task.",
                    provider=entry.provider,
                    model=entry.id,
                    task=task,
                    retryable=False,
                )
                attempts.append(
                    Attempt(
                        entry.provider,
                        entry.id,
                        False,
                        last_error.category.value,
                        last_error.message,
                    )
                )
                if not self.policy.can_fallback(last_error):
                    raise last_error
                continue

            response_format = response_format_for(schema, entry.supports_json)
            try:
                execution = self._run_text_candidate(
                    entry,
                    provider_obj,
                    task,
                    messages,
                    schema=schema,
                    response_format=response_format,
                    attempts=attempts,
                )
            except AIError as exc:
                last_error = exc
                if exc.category == ErrorCategory.FREE_QUOTA_EXHAUSTED:
                    self._block_provider_quota(entry.provider, exc)
                    if entry.underlying_provider:
                        self._block_provider_quota(entry.underlying_provider, exc)
                self._record_provider_event(
                    entry.provider,
                    success=False,
                    category=exc.category.value,
                    retry_after_seconds=exc.retry_after,
                )
                if index == len(routing.candidates) - 1 or not self.policy.can_fallback(exc):
                    raise
                logger.warning(
                    "Falling back after %s from %s/%s", exc.category.value, entry.provider, entry.id
                )
                continue
            execution.used_fallback = index > 0
            self._record_provider_event(entry.provider, success=True)
            return execution

        raise last_error or _no_candidate_error(routing, task)

    def _run_text_candidate(
        self,
        entry: ModelEntry,
        provider: BaseTextProvider,
        task: str,
        messages: list[dict[str, Any]],
        *,
        schema: type[BaseModel] | None,
        response_format: dict[str, Any] | None,
        attempts: list[Attempt],
    ) -> TextExecution:
        schema_failures = 0
        schema_retries = self.registry.policy.schema_validation_retries
        current_messages = messages
        total_retries = 0
        while True:
            completion, provider_retries = self._call_text_with_retries(
                entry, provider, current_messages, task=task, response_format=response_format
            )
            total_retries += provider_retries
            if schema is None:
                self._record_completion(entry, task, completion, retry_count=total_retries)
                attempts.append(
                    Attempt(entry.provider, entry.id, True, latency_ms=completion.latency_ms)
                )
                return TextExecution(
                    task=task,
                    provider=entry.provider,
                    model=entry.id,
                    data=completion.text,
                    raw=completion.text,
                    attempts=attempts,
                )

            try:
                data = parse_and_validate(completion.text, schema)
            except AIError as exc:
                self._record(
                    entry,
                    task=task,
                    request_kind="text",
                    success=False,
                    error_category=exc.category.value,
                    input_tokens=completion.input_tokens,
                    output_tokens=completion.output_tokens,
                    cost=completion.cost,
                    retry_count=total_retries,
                    latency_ms=completion.latency_ms,
                )
                attempts.append(
                    Attempt(
                        entry.provider,
                        entry.id,
                        False,
                        exc.category.value,
                        exc.message,
                        completion.latency_ms,
                    )
                )
                schema_failures += 1
                if schema_failures <= schema_retries:
                    logger.info(
                        "Schema validation failed for %s/%s; retrying (%d/%d)",
                        entry.provider,
                        entry.id,
                        schema_failures,
                        schema_retries,
                    )
                    current_messages = repair_messages(messages, completion.text, exc.message)
                    total_retries += 1
                    continue
                raise

            self._record_completion(entry, task, completion, retry_count=total_retries)
            attempts.append(
                Attempt(entry.provider, entry.id, True, latency_ms=completion.latency_ms)
            )
            return TextExecution(
                task=task,
                provider=entry.provider,
                model=entry.id,
                data=data,
                raw=completion.text,
                attempts=attempts,
            )

    def _call_text_with_retries(
        self,
        entry: ModelEntry,
        provider: BaseTextProvider,
        messages: list[dict[str, Any]],
        *,
        task: str,
        response_format: dict[str, Any] | None,
    ) -> tuple[TextCompletion, int]:
        config = provider.config
        max_retries = (
            config.max_retries if config.max_retries is not None else self.settings.max_retries
        )
        attempt = 0
        while True:
            self._check_local_limits(entry)
            self.throttle.acquire(
                f"{entry.provider}:{entry.id}",
                requests_per_minute=config.requests_per_minute,
                min_interval_seconds=config.min_interval_seconds,
                provider=entry.provider,
                model=entry.id,
            )
            try:
                self.policy.assert_free(entry)
                completion = provider.complete(
                    entry,
                    messages,
                    temperature=0.1 if response_format else 0.2,
                    max_tokens=entry.max_output_tokens,
                    response_format=response_format,
                    timeout=config.timeout_seconds or self.settings.timeout_seconds,
                )
                return completion, attempt
            except AIError as exc:
                self._record(
                    entry,
                    task=task,
                    request_kind="text",
                    success=False,
                    error_category=exc.category.value,
                    retry_count=attempt,
                    quota_error=exc.category == ErrorCategory.FREE_QUOTA_EXHAUSTED,
                )
                if exc.category == ErrorCategory.FREE_QUOTA_EXHAUSTED:
                    raise
                if not exc.retryable or attempt >= max_retries:
                    raise
                if (
                    exc.retry_after is not None
                    and exc.retry_after > self.registry.retry.max_retry_after_seconds
                ):
                    raise
                delay = compute_delay(
                    attempt + 1,
                    self.registry.retry,
                    retry_after=exc.retry_after,
                    rng=self._rng,
                )
                logger.info(
                    "Retrying %s/%s after %.1fs (%s)",
                    entry.provider,
                    entry.id,
                    delay,
                    exc.category.value,
                )
                self._sleep(delay)
                attempt += 1

    # ----------------------------------------------------------------- speech tasks
    def can_transcribe_prepared_chunks(
        self, *, provider: str | None = None, model: str | None = None
    ) -> bool:
        """Whether the selected free speech route accepts prepared chunks."""
        routing = self.router.resolve("speech_to_text", provider=provider, model=model)
        for candidate in routing.candidates:
            provider_obj = self.providers.get(candidate.entry.provider)
            if not isinstance(provider_obj, BaseSpeechProvider):
                continue
            if not provider_obj.supports_prepared_chunks:
                continue
            _, cooldown = self._candidate_cooldown(candidate.entry)
            if cooldown > 0:
                continue
            available, _ = provider_obj.available()
            if available:
                return True
        return False

    def transcribe_chunk(
        self,
        chunk: AudioChunk,
        *,
        provider: str | None = None,
        model: str | None = None,
        language: str | None = None,
        word_timestamps: bool = True,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        """Transcribe one prepared chunk through the normal routing/retry path."""
        task = "speech_to_text"
        routing = self.router.resolve(task, provider=provider, model=model)
        self._raise_if_no_candidates(routing, task, explicit=bool(provider or model))
        attempts: list[Attempt] = []
        last_error: AIError | None = None

        for index, candidate in enumerate(routing.candidates):
            entry = candidate.entry
            blocking_provider, cooldown = self._candidate_cooldown(entry)
            if cooldown > 0:
                last_error = AIError(
                    ErrorCategory.FREE_QUOTA_EXHAUSTED,
                    f"Provider '{blocking_provider}' is cooling down after free-quota "
                    f"exhaustion; retry in about {math.ceil(cooldown)}s.",
                    provider=entry.provider,
                    model=entry.id,
                    task=task,
                    retryable=False,
                    retry_after=cooldown,
                )
                attempts.append(
                    Attempt(
                        entry.provider,
                        entry.id,
                        False,
                        last_error.category.value,
                        last_error.message,
                    )
                )
                if provider or model:
                    raise last_error
                continue

            provider_obj = self.providers.get(entry.provider)
            if not isinstance(provider_obj, BaseSpeechProvider):
                last_error = AIError(
                    ErrorCategory.UNSUPPORTED_CAPABILITY,
                    f"Provider '{entry.provider}' is not a speech provider.",
                    provider=entry.provider,
                    model=entry.id,
                    task=task,
                    retryable=False,
                )
                attempts.append(
                    Attempt(
                        entry.provider,
                        entry.id,
                        False,
                        last_error.category.value,
                        last_error.message,
                    )
                )
                if provider or model:
                    raise last_error
                continue
            if not provider_obj.supports_prepared_chunks:
                last_error = AIError(
                    ErrorCategory.UNSUPPORTED_CAPABILITY,
                    f"Provider '{entry.provider}' cannot transcribe an externally prepared "
                    "audio chunk safely.",
                    provider=entry.provider,
                    model=entry.id,
                    task=task,
                    retryable=False,
                    details={"reason": "prepared_chunks_unsupported"},
                )
                attempts.append(
                    Attempt(
                        entry.provider,
                        entry.id,
                        False,
                        last_error.category.value,
                        last_error.message,
                    )
                )
                if provider or model:
                    raise last_error
                continue

            available, reason = provider_obj.available()
            if not available:
                last_error = _availability_error(entry.provider, reason, self.registry)
                attempts.append(
                    Attempt(
                        entry.provider,
                        entry.id,
                        False,
                        last_error.category.value,
                        last_error.message,
                    )
                )
                if provider or model:
                    raise last_error
                continue

            try:
                result = self._run_speech_candidate(
                    entry,
                    task=task,
                    attempts=attempts,
                    config=provider_obj.config,
                    call=partial(
                        provider_obj.transcribe_chunk,
                        entry,
                        chunk,
                        language=language,
                        word_timestamps=word_timestamps,
                        prompt=prompt,
                    ),
                )
            except AIError as exc:
                last_error = exc
                if exc.category == ErrorCategory.FREE_QUOTA_EXHAUSTED:
                    self._block_provider_quota(entry.provider, exc)
                    if entry.underlying_provider:
                        self._block_provider_quota(entry.underlying_provider, exc)
                self._record_provider_event(
                    entry.provider,
                    success=False,
                    category=exc.category.value,
                    retry_after_seconds=exc.retry_after,
                )
                if index == len(routing.candidates) - 1 or not self.policy.can_fallback(exc):
                    raise
                logger.warning(
                    "Falling back after %s from %s/%s for chunk %s",
                    exc.category.value,
                    entry.provider,
                    entry.id,
                    chunk.index,
                )
                continue
            execution = SpeechExecution(
                task=task,
                provider=entry.provider,
                model=entry.id,
                result=result,
                attempts=attempts,
                used_fallback=index > 0,
            )
            self._record_provider_event(entry.provider, success=True)
            return {
                "task": task,
                "provider": execution.provider,
                "model": execution.model,
                "used_fallback": execution.used_fallback,
                "transcript": result.model_dump(mode="json"),
                "attempts": [attempt.__dict__ for attempt in execution.attempts],
            }

        raise last_error or _no_candidate_error(routing, task)

    def transcribe(
        self,
        audio_path: Path | str,
        *,
        provider: str | None = None,
        model: str | None = None,
        language: str | None = None,
        word_timestamps: bool = True,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        task = "speech_to_text"
        routing = self.router.resolve(task, provider=provider, model=model)
        self._raise_if_no_candidates(routing, task, explicit=bool(provider or model))
        attempts: list[Attempt] = []
        last_error: AIError | None = None

        for index, candidate in enumerate(routing.candidates):
            entry = candidate.entry
            blocking_provider, cooldown = self._candidate_cooldown(entry)
            if cooldown > 0:
                last_error = AIError(
                    ErrorCategory.FREE_QUOTA_EXHAUSTED,
                    f"Provider '{blocking_provider}' is cooling down after free-quota "
                    f"exhaustion; retry in about {math.ceil(cooldown)}s.",
                    provider=entry.provider,
                    model=entry.id,
                    task=task,
                    retryable=False,
                    retry_after=cooldown,
                )
                attempts.append(
                    Attempt(
                        entry.provider,
                        entry.id,
                        False,
                        last_error.category.value,
                        last_error.message,
                    )
                )
                if provider or model:
                    raise last_error
                continue

            provider_obj = self.providers.get(entry.provider)
            if not isinstance(provider_obj, BaseSpeechProvider):
                last_error = AIError(
                    ErrorCategory.UNSUPPORTED_CAPABILITY,
                    f"Provider '{entry.provider}' is not a speech provider.",
                    provider=entry.provider,
                    model=entry.id,
                    task=task,
                    retryable=False,
                )
                attempts.append(
                    Attempt(
                        entry.provider,
                        entry.id,
                        False,
                        last_error.category.value,
                        last_error.message,
                    )
                )
                if not self.policy.can_fallback(last_error):
                    raise last_error
                continue

            available, reason = provider_obj.available()
            if not available:
                last_error = _availability_error(entry.provider, reason, self.registry)
                attempts.append(
                    Attempt(
                        entry.provider,
                        entry.id,
                        False,
                        last_error.category.value,
                        last_error.message,
                    )
                )
                if provider or model:
                    raise last_error
                continue

            try:
                result = self._run_speech_candidate(
                    entry,
                    task=task,
                    attempts=attempts,
                    config=provider_obj.config,
                    call=partial(
                        provider_obj.transcribe,
                        entry,
                        Path(audio_path),
                        language=language,
                        word_timestamps=word_timestamps,
                        prompt=prompt,
                    ),
                )
            except AIError as exc:
                last_error = exc
                if exc.category == ErrorCategory.FREE_QUOTA_EXHAUSTED:
                    self._block_provider_quota(entry.provider, exc)
                    if entry.underlying_provider:
                        self._block_provider_quota(entry.underlying_provider, exc)
                self._record_provider_event(
                    entry.provider,
                    success=False,
                    category=exc.category.value,
                    retry_after_seconds=exc.retry_after,
                )
                if index == len(routing.candidates) - 1 or not self.policy.can_fallback(exc):
                    raise
                logger.warning(
                    "Falling back after %s from %s/%s", exc.category.value, entry.provider, entry.id
                )
                continue
            execution = SpeechExecution(
                task=task,
                provider=entry.provider,
                model=entry.id,
                result=result,
                attempts=attempts,
                used_fallback=index > 0,
            )
            self._record_provider_event(entry.provider, success=True)
            return {
                "task": task,
                "provider": execution.provider,
                "model": execution.model,
                "used_fallback": execution.used_fallback,
                "transcript": result.model_dump(mode="json"),
                "attempts": [attempt.__dict__ for attempt in execution.attempts],
            }

        raise last_error or _no_candidate_error(routing, task)

    def _run_speech_candidate(
        self,
        entry: ModelEntry,
        *,
        task: str,
        attempts: list[Attempt],
        config: Any,
        call: Callable[..., TranscriptResult],
    ) -> TranscriptResult:
        max_retries = (
            config.max_retries if config.max_retries is not None else self.settings.max_retries
        )
        attempt = 0
        while True:
            self._check_local_limits(entry)
            self.throttle.acquire(
                f"{entry.provider}:{entry.id}",
                requests_per_minute=config.requests_per_minute,
                min_interval_seconds=config.min_interval_seconds,
                provider=entry.provider,
                model=entry.id,
            )
            started = time.perf_counter()
            try:
                self.policy.assert_free(entry)
                result = call(timeout=config.timeout_seconds or self.settings.timeout_seconds)
            except AIError as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                self._record(
                    entry,
                    task=task,
                    request_kind="speech",
                    success=False,
                    error_category=exc.category.value,
                    retry_count=attempt,
                    quota_error=exc.category == ErrorCategory.FREE_QUOTA_EXHAUSTED,
                    latency_ms=latency_ms,
                )
                attempts.append(
                    Attempt(
                        entry.provider, entry.id, False, exc.category.value, exc.message, latency_ms
                    )
                )
                if exc.category == ErrorCategory.FREE_QUOTA_EXHAUSTED:
                    raise
                if not exc.retryable or attempt >= max_retries:
                    raise
                if (
                    exc.retry_after is not None
                    and exc.retry_after > self.registry.retry.max_retry_after_seconds
                ):
                    raise
                delay = compute_delay(
                    attempt + 1,
                    self.registry.retry,
                    retry_after=exc.retry_after,
                    rng=self._rng,
                )
                self._sleep(delay)
                attempt += 1
                continue

            latency_ms = int((time.perf_counter() - started) * 1000)
            self._record(
                entry,
                task=task,
                request_kind="speech",
                success=True,
                audio_seconds=result.duration,
                retry_count=attempt,
                latency_ms=latency_ms,
            )
            attempts.append(Attempt(entry.provider, entry.id, True, latency_ms=latency_ms))
            return result

    # ------------------------------------------------------------------ free verify
    def discover_nvidia_models(self, *, force: bool = False) -> dict[str, Any]:
        """Discover NVIDIA Free Endpoint models from structured catalog metadata."""
        if self.registry.get_provider("nvidia") is None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "NVIDIA provider is not configured.",
                retryable=False,
            )
        try:
            summary = nvidia_catalog.discover(self.settings.config_dir, force=force)
        except nvidia_catalog.NvidiaCatalogError as exc:
            cached = nvidia_catalog.load_cache(self.settings.config_dir)
            return {
                "ok": False,
                "cached": cached is not None,
                "models": len(cached.get("models") or []) if cached else 0,
                "updated_at": cached.get("updated_at") if cached else None,
                "stats": cached.get("stats") if cached else {},
                "error": str(exc),
            }
        self.reload()
        return summary

    def _record_nvidia_verification(self, entry: ModelEntry, result: ConnectionResult) -> None:
        if entry.provider != "nvidia":
            return
        category = str((result.details or {}).get("category") or "")
        if result.ok:
            self.registry.update_verification(
                entry.id,
                free_verified=True,
                verification_source=nvidia_catalog.CATALOG_SOURCE,
                api_available=True,
                verification_status="verified",
                verification_message="NVIDIA endpoint responded to a bounded smoke test.",
            )
            return
        if category in {
            ErrorCategory.MODEL_UNAVAILABLE.value,
            ErrorCategory.UNSUPPORTED_CAPABILITY.value,
            ErrorCategory.INVALID_RESPONSE.value,
        }:
            self.registry.update_verification(
                entry.id,
                free_verified=False,
                verification_source=nvidia_catalog.CATALOG_SOURCE,
                api_available=False,
                verification_status="unavailable",
                verification_message=result.message,
            )
            return
        if category in {
            ErrorCategory.FREE_QUOTA_EXHAUSTED.value,
            ErrorCategory.RATE_LIMIT_EXCEEDED.value,
        }:
            message = f"Endpoint is callable but currently rate limited: {result.message}"
        else:
            message = result.message
        self.registry.update_verification(
            entry.id,
            free_verified=False,
            verification_source=nvidia_catalog.CATALOG_SOURCE,
            api_available="unknown",
            verification_status="unverified",
            verification_message=message,
        )

    def verify_free(self, model_id: str) -> dict[str, Any]:
        entry = self.registry.get_model(model_id)
        if entry is None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Unknown model '{model_id}'.",
                retryable=False,
            )
        if entry.provider == "nvidia":
            return self._verify_nvidia(entry)
        if entry.provider != "openrouter":
            return {
                "model": entry.id,
                "verified": entry.free_verified,
                "supported": False,
                "reason": "Automatic pricing verification is implemented for OpenRouter only. "
                "Gemini and Groq free eligibility is verified against their pricing pages manually.",
            }
        return self._verify_openrouter(entry)

    def _verify_nvidia(self, entry: ModelEntry) -> dict[str, Any]:
        if entry.access_type != "free_endpoint":
            self.registry.update_verification(
                entry.id,
                free=True,
                free_verified=False,
                verification_source=nvidia_catalog.CATALOG_SOURCE,
                verification_status="failed",
                verification_message="NVIDIA catalog does not list this as a Free Endpoint.",
            )
            return {
                "model": entry.id,
                "verified": False,
                "supported": True,
                "reason": "NVIDIA catalog does not list this model as a Free Endpoint.",
            }
        if entry.api_available is False:
            return {
                "model": entry.id,
                "verified": False,
                "supported": True,
                "reason": entry.verification_message
                or "Model is not listed by NVIDIA's public API catalog.",
            }

        provider = self.providers.get("nvidia")
        if provider is None:
            return {
                "model": entry.id,
                "verified": False,
                "supported": True,
                "reason": "NVIDIA provider adapter is not available.",
            }
        available, reason = provider.available()
        if not available:
            return {
                "model": entry.id,
                "verified": False,
                "supported": True,
                "reason": f"Bounded NVIDIA smoke test was not run: {reason}",
            }

        result = provider.test_connection(entry)
        self._health["nvidia"] = result
        self._record_nvidia_verification(entry, result)
        updated = self.registry.require_model(entry.id)
        return {
            "model": updated.id,
            "verified": updated.free_verified,
            "supported": True,
            "api_available": updated.api_available,
            "verification_status": updated.verification_status,
            "reason": updated.verification_message or result.message,
        }

    def _verify_openrouter(self, entry: ModelEntry) -> dict[str, Any]:
        import httpx

        try:
            response = httpx.get("https://openrouter.ai/api/v1/models", timeout=20.0)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            return {
                "model": entry.id,
                "verified": entry.free_verified,
                "supported": True,
                "reason": f"Could not fetch OpenRouter pricing metadata: {exc}",
            }
        models = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            return {
                "model": entry.id,
                "verified": entry.free_verified,
                "supported": True,
                "reason": "OpenRouter models response had an unexpected shape.",
            }
        match = next((m for m in models if m.get("id") == entry.provider_model_id), None)
        if match is None:
            return {
                "model": entry.id,
                "verified": entry.free_verified,
                "supported": True,
                "reason": (
                    f"'{entry.provider_model_id}' is not a concrete model in the pricing list "
                    "(routing aliases cannot be verified automatically)."
                ),
            }
        pricing = match.get("pricing")
        if not isinstance(pricing, dict):
            return {
                "model": entry.id,
                "verified": entry.free_verified,
                "supported": True,
                "reason": "OpenRouter returned no pricing metadata; free status was not changed.",
            }
        prompt_raw = pricing.get("prompt")
        completion_raw = pricing.get("completion")
        if prompt_raw is None or completion_raw is None:
            return {
                "model": entry.id,
                "verified": entry.free_verified,
                "supported": True,
                "reason": "OpenRouter pricing metadata is incomplete; free status was not changed.",
            }
        try:
            prompt_price = float(prompt_raw)
            completion_price = float(completion_raw)
        except (TypeError, ValueError):
            return {
                "model": entry.id,
                "verified": entry.free_verified,
                "supported": True,
                "reason": "OpenRouter pricing metadata is not numeric; free status was not changed.",
            }
        if (
            not math.isfinite(prompt_price)
            or not math.isfinite(completion_price)
            or prompt_price < 0
            or completion_price < 0
        ):
            return {
                "model": entry.id,
                "verified": entry.free_verified,
                "supported": True,
                "reason": "OpenRouter pricing metadata is invalid; free status was not changed.",
            }
        is_free = prompt_price == 0.0 and completion_price == 0.0
        was_free = entry.free
        self.registry.update_verification(
            entry.id,
            free_verified=is_free,
            verification_source="https://openrouter.ai/api/v1/models",
            free=False if not is_free else None,
        )
        return {
            "model": entry.id,
            "verified": is_free,
            "supported": True,
            "pricing": {"prompt": prompt_price, "completion": completion_price},
            "free_revoked": was_free and not is_free,
            "reason": (
                "Zero pricing confirmed."
                if is_free
                else "Model has non-zero pricing; free approval was revoked."
            ),
        }

    # --------------------------------------------------------------------- helpers
    def _resolve_entry(self, provider_id: str, model_id: str | None) -> ModelEntry | None:
        if model_id:
            entry = self.registry.get_model(model_id)
            if entry is not None and entry.provider == provider_id:
                return entry
            matches = [
                m
                for m in self.registry.models_for_provider(provider_id)
                if m.provider_model_id == model_id
            ]
            return matches[0] if len(matches) == 1 else None
        return None

    def _first_free_model(self, provider_id: str) -> ModelEntry | None:
        for entry in self.registry.models_for_provider(provider_id):
            if entry.free and entry.is_enabled():
                return entry
        return None

    def _check_local_limits(self, entry: ModelEntry) -> None:
        config = self.registry.get_provider(entry.provider)
        provider_limit = config.requests_per_day if config else 0
        model_limit = entry.daily_request_limit or 0
        if model_limit > 0:
            used = self.usage.count_today(entry.provider, entry.id)
            if used >= model_limit:
                raise AIError(
                    ErrorCategory.RATE_LIMIT_EXCEEDED,
                    f"Local daily request limit reached for {entry.id} ({used}/{model_limit}). "
                    "This is a locally tracked guard, not a provider quota report. "
                    "Try another free model or resume after the UTC day rolls over.",
                    provider=entry.provider,
                    model=entry.id,
                    retryable=False,
                    details={"source": "local_daily_limit", "used": used, "limit": model_limit},
                )
        if provider_limit > 0:
            used = self.usage.count_today(entry.provider)
            if used >= provider_limit:
                raise AIError(
                    ErrorCategory.RATE_LIMIT_EXCEEDED,
                    f"Local daily request limit reached for provider {entry.provider} "
                    f"({used}/{provider_limit}). This is a locally tracked guard, not a "
                    "provider quota report.",
                    provider=entry.provider,
                    model=entry.id,
                    retryable=False,
                    details={"source": "local_daily_limit", "used": used, "limit": provider_limit},
                )

    def _record_completion(
        self, entry: ModelEntry, task: str, completion: TextCompletion, *, retry_count: int
    ) -> None:
        self._record(
            entry,
            task=task,
            request_kind="text",
            success=True,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cost=completion.cost,
            retry_count=retry_count,
            latency_ms=completion.latency_ms,
        )

    def _record(
        self,
        entry: ModelEntry,
        *,
        task: str | None,
        request_kind: str,
        success: bool,
        error_category: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        audio_seconds: float | None = None,
        retry_count: int = 0,
        cost: float | None = None,
        quota_error: bool = False,
        latency_ms: int | None = None,
    ) -> None:
        try:
            self.usage.record(
                provider=entry.provider,
                model=entry.id,
                task=task,
                request_kind=request_kind,
                success=success,
                error_category=error_category,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                audio_seconds=audio_seconds,
                retry_count=retry_count,
                cost=cost,
                quota_error=quota_error,
                latency_ms=latency_ms,
            )
        except Exception:  # pragma: no cover - usage must never break requests
            logger.exception("Failed to record usage event")

    def _raise_if_no_candidates(self, routing: RoutingResult, task: str, *, explicit: bool) -> None:
        if routing.candidates:
            return
        if explicit:
            skipped = routing.skipped
            if skipped and skipped[0].category == ErrorCategory.POLICY_VIOLATION:
                raise AIError(
                    ErrorCategory.POLICY_VIOLATION,
                    f"Requested model is not an approved free model for task '{task}': "
                    + "; ".join(s.reason for s in skipped),
                    task=task,
                    retryable=False,
                    details={"skipped": [s.__dict__ for s in skipped]},
                )
        raise _no_candidate_error(routing, task)


# ---------------------------------------------------------------------- utilities
def _capabilities_for_type(provider_type: str) -> list[str]:
    if provider_type in ("litellm", "openai_compatible"):
        return ["text"]
    if provider_type in ("groq_speech", "whisper_cpp"):
        return ["speech_to_text"]
    return []


def _connection_dict(result: ConnectionResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "ok": result.ok,
        "provider": result.provider,
        "model": result.model,
        "message": result.message,
        "latency_ms": result.latency_ms,
        "details": result.details,
    }


def _availability_error(provider_id: str, reason: str, registry: Registry) -> AIError:
    config = registry.get_provider(provider_id)
    env_var = config.api_key_env if config else None
    if env_var and "not set" in reason:
        return missing_key(provider_id, env_var)
    return AIError(
        ErrorCategory.PROVIDER_UNAVAILABLE,
        f"Provider '{provider_id}' is unavailable: {reason}",
        provider=provider_id,
        retryable=False,
    )


def _no_candidate_error(routing: RoutingResult, task: str) -> AIError:
    skipped = [s.__dict__ for s in routing.skipped]
    if not skipped:
        return AIError(
            ErrorCategory.INVALID_REQUEST,
            f"No routing candidates configured for task '{task}'.",
            task=task,
            retryable=False,
        )
    categories = {s.category for s in routing.skipped}
    if ErrorCategory.MISSING_API_KEY in categories:
        category = ErrorCategory.MISSING_API_KEY
    elif categories <= {ErrorCategory.POLICY_VIOLATION, ErrorCategory.UNSUPPORTED_CAPABILITY}:
        category = ErrorCategory.POLICY_VIOLATION
    else:
        category = ErrorCategory.PROVIDER_UNAVAILABLE
    return AIError(
        category,
        f"No eligible free provider/model is available for task '{task}'. "
        + " | ".join(f"{s.provider}/{s.model}: {s.reason}" for s in routing.skipped),
        task=task,
        retryable=False,
        details={"skipped": skipped},
    )
