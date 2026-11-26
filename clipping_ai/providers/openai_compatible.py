"""OpenAI-compatible chat provider for local gateways and free API proxies."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from clipping_ai.errors import AIError, ErrorCategory, policy_violation
from clipping_ai.providers.base import BaseTextProvider, ConnectionResult, TextCompletion
from clipping_ai.providers.error_mapping import map_provider_exception
from clipping_ai.registry import ModelEntry


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text") or item.get("content")
                if isinstance(value, str):
                    parts.append(value)
        return "".join(parts)
    return ""


class OpenAICompatibleProvider(BaseTextProvider):
    """Calls a configured ``/chat/completions`` endpoint with a bearer key.

    The adapter is generic on purpose: it can point at a local gateway or any
    other OpenAI-compatible free endpoint. It never infers a URL or a model;
    both must come from the explicit local configuration.
    """

    def __init__(
        self,
        provider_id: str,
        config: Any,
        *,
        secret_getter: Callable[[str], str | None],
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(provider_id, config)
        self._secret_getter = secret_getter
        self._client_factory = client_factory or httpx.Client

    def _base_url(self) -> str | None:
        value = (self.config.base_url or "").strip()
        return value.rstrip("/") or None

    def _api_key(self) -> str | None:
        env_var = self.config.api_key_env
        if not env_var:
            return None
        return self._secret_getter(env_var)

    def available(self) -> tuple[bool, str]:
        if not self.config.enabled:
            return False, "provider disabled in configuration"
        if not self._base_url():
            return False, "base_url is not configured"
        if self.config.api_key_env and not self._api_key():
            return False, f"{self.config.api_key_env} is not set"
        return True, "ok"

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
        if not entry.free or not entry.is_enabled():
            raise policy_violation(
                "Blocked: only explicitly approved, enabled free models may execute.",
                provider=entry.provider,
                model=entry.id,
            )
        base_url = self._base_url()
        if not base_url:
            raise AIError(
                ErrorCategory.PROVIDER_UNAVAILABLE,
                f"Provider '{self.provider_id}' has no base_url configured.",
                provider=self.provider_id,
                model=entry.id,
                retryable=False,
            )

        payload: dict[str, Any] = {
            "model": entry.provider_model_id,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if response_format:
            payload["response_format"] = response_format

        headers = {"Content-Type": "application/json", **dict(self.config.extra_headers)}
        api_key = self._api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        started = time.perf_counter()
        try:
            with self._client_factory(
                base_url=base_url, timeout=timeout or self.config.timeout_seconds or 120.0
            ) as client:
                response = client.post("/chat/completions", json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
        except BaseException as exc:  # noqa: BLE001 - normalised below
            raise map_provider_exception(exc, provider=self.provider_id, model=entry.id) from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        try:
            choice = data["choices"][0]
            text = _content_to_text(choice["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise AIError(
                ErrorCategory.INVALID_RESPONSE,
                "Provider returned an unexpected response shape.",
                provider=self.provider_id,
                model=entry.id,
                retryable=True,
                cause=exc,
            ) from exc
        if not text or not text.strip():
            raise AIError(
                ErrorCategory.INVALID_RESPONSE,
                "Provider returned an empty completion.",
                provider=self.provider_id,
                model=entry.id,
                retryable=True,
            )

        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        return TextCompletion(
            text=text.strip(),
            provider=self.provider_id,
            model=entry.id,
            input_tokens=input_tokens if isinstance(input_tokens, int) else None,
            output_tokens=output_tokens if isinstance(output_tokens, int) else None,
            latency_ms=latency_ms,
            raw=data,
        )

    def test_connection(self, entry: ModelEntry) -> ConnectionResult:
        started = time.perf_counter()
        try:
            result = self.complete(
                entry,
                [{"role": "user", "content": "Reply with the single word OK."}],
                temperature=0.0,
                max_tokens=64,
                timeout=min(self.config.timeout_seconds or 30.0, 30.0),
            )
        except AIError as exc:
            return ConnectionResult(
                ok=False,
                provider=self.provider_id,
                model=entry.id,
                message=exc.message,
                latency_ms=int((time.perf_counter() - started) * 1000),
                details={"category": exc.category.value},
            )
        return ConnectionResult(
            ok=True,
            provider=self.provider_id,
            model=entry.id,
            message=f"Connected; model replied '{result.text[:40]}'",
            latency_ms=result.latency_ms,
            details={
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            },
        )
