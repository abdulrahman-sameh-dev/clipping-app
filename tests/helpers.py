"""Shared test doubles and factories."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from clipping_ai.errors import AIError
from clipping_ai.manager import ProviderManager
from clipping_ai.providers.base import (
    BaseSpeechProvider,
    BaseTextProvider,
    ConnectionResult,
    TextCompletion,
)
from clipping_ai.registry import (
    ModelEntry,
    PolicyConfig,
    ProviderConfig,
    Registry,
    RetryConfig,
    RouteCandidate,
)
from clipping_ai.schemas import TranscriptResult
from clipping_ai.settings import Settings
from clipping_ai.throttle import Throttle
from clipping_ai.usage import UsageTracker


class FakeTextProvider(BaseTextProvider):
    def __init__(
        self,
        provider_id: str,
        responses: list[Any] | None = None,
        *,
        available: bool = True,
        reason: str = "ok",
        config: ProviderConfig | None = None,
    ) -> None:
        super().__init__(
            provider_id,
            config
            or ProviderConfig(
                type="litellm",
                api_key_env=None,
                max_retries=0,
                requests_per_minute=0,
                min_interval_seconds=0,
            ),
        )
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self._available = available
        self._reason = reason

    def available(self) -> tuple[bool, str]:
        return self._available, self._reason

    def complete(
        self,
        entry: ModelEntry,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> TextCompletion:
        self.calls.append(
            {
                "entry": entry,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": response_format,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError(f"unexpected completion call to {self.provider_id}")
        item = self.responses.pop(0)
        if callable(item):
            item = item(entry, messages)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, TextCompletion):
            return item
        return TextCompletion(
            text=str(item),
            provider=self.provider_id,
            model=entry.id,
            input_tokens=3,
            output_tokens=4,
            latency_ms=1,
        )

    def test_connection(self, entry: ModelEntry) -> ConnectionResult:
        return ConnectionResult(
            ok=True,
            provider=self.provider_id,
            model=entry.id,
            message="fake ok",
            latency_ms=1,
            details={"input_tokens": 1, "output_tokens": 1, "reported_cost": 0.0},
        )


class FakeSpeechProvider(BaseSpeechProvider):
    def __init__(
        self,
        provider_id: str,
        responses: list[Any] | None = None,
        *,
        available: bool = True,
        reason: str = "ok",
        config: ProviderConfig | None = None,
    ) -> None:
        super().__init__(
            provider_id,
            config
            or ProviderConfig(
                type="groq_speech",
                api_key_env=None,
                max_retries=0,
                requests_per_minute=0,
                min_interval_seconds=0,
            ),
        )
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self._available = available
        self._reason = reason

    def available(self) -> tuple[bool, str]:
        return self._available, self._reason

    def transcribe(
        self,
        entry: ModelEntry,
        audio_path: Path,
        *,
        language: str | None = None,
        word_timestamps: bool = True,
        prompt: str | None = None,
        timeout: float | None = None,
    ) -> TranscriptResult:
        self.calls.append(
            {
                "entry": entry,
                "audio_path": audio_path,
                "language": language,
                "word_timestamps": word_timestamps,
                "prompt": prompt,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError(f"unexpected transcription call to {self.provider_id}")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def test_connection(self, entry: ModelEntry | None = None) -> ConnectionResult:
        return ConnectionResult(
            ok=True,
            provider=self.provider_id,
            model=entry.id if entry else None,
            message="fake ok",
            latency_ms=1,
        )


def make_registry(
    *,
    providers: dict[str, ProviderConfig],
    models: list[ModelEntry],
    routing: dict[str, list[RouteCandidate]],
    policy: PolicyConfig | None = None,
    retry: RetryConfig | None = None,
    config_dir: Path | None = None,
) -> Registry:
    return Registry(
        config_dir=config_dir or Path("/tmp/opencode/clipping-ai-test-config"),
        providers=providers,
        models={model.id: model for model in models},
        local_models={},
        routing=routing,
        policy=policy or PolicyConfig(),
        retry=retry or RetryConfig(),
    )


def make_manager(
    registry: Registry,
    providers: dict[str, BaseTextProvider | BaseSpeechProvider],
    tmp_path: Path,
) -> ProviderManager:
    settings = Settings(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    settings.ensure_dirs()
    return ProviderManager(
        settings=settings,
        registry=registry,
        usage=UsageTracker(settings.usage_db_path),
        providers=providers,
        throttle=Throttle(),
        sleep_fn=lambda _: None,
        rng=random.Random(0),
    )


def text_model(
    model_id: str,
    *,
    provider: str = "fake",
    free: bool = True,
    status: str = "enabled",
    tasks: list[str] | None = None,
    supports_json: bool = True,
) -> ModelEntry:
    return ModelEntry(
        id=model_id,
        provider=provider,
        provider_model_id=model_id,
        tasks=tasks or ["content_analysis"],
        free=free,
        free_verified=True,
        supports_json=supports_json,
        status=status,
    )


def speech_model(
    model_id: str,
    *,
    provider: str = "fake_speech",
    free: bool = True,
    status: str = "enabled",
) -> ModelEntry:
    return ModelEntry(
        id=model_id,
        provider=provider,
        provider_model_id=model_id,
        tasks=["speech_to_text"],
        free=free,
        free_verified=True,
        supports_json=True,
        supports_word_timestamps=True,
        status=status,
    )


def completion(text: str, *, provider: str = "fake", model: str = "fake-model") -> TextCompletion:
    return TextCompletion(
        text=text,
        provider=provider,
        model=model,
        input_tokens=3,
        output_tokens=4,
        latency_ms=1,
    )


def quota_error(provider: str = "fake", model: str = "fake-model") -> AIError:
    from clipping_ai.errors import ErrorCategory

    return AIError(
        ErrorCategory.FREE_QUOTA_EXHAUSTED,
        "free quota exhausted",
        provider=provider,
        model=model,
        retryable=False,
    )
