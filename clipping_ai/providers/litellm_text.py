"""Generic LiteLLM-backed text provider.

One adapter serves Gemini, OpenRouter and Groq text models. The model string is
always derived from a registry entry that was approved as free; callers cannot
pass an arbitrary paid model through this class.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from clipping_ai.errors import AIError, ErrorCategory, policy_violation
from clipping_ai.providers.base import BaseTextProvider, ConnectionResult, TextCompletion
from clipping_ai.providers.error_mapping import map_provider_exception
from clipping_ai.registry import ModelEntry

logger = logging.getLogger(__name__)

_CONFIGURED = False


def configure_litellm() -> None:
    """Disable telemetry/verbose logging and never configure paid fallbacks."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    try:
        import litellm

        litellm.suppress_debug_info = True
        litellm.set_verbose = False
        litellm.telemetry = False
        litellm.turn_off_message_logging = True
        litellm.drop_params = False
        for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
            log = logging.getLogger(name)
            log.handlers.clear()
            log.propagate = True
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not apply LiteLLM global settings", exc_info=True)
    _CONFIGURED = True


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
            else:
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


class LitellmTextProvider(BaseTextProvider):
    def __init__(
        self,
        provider_id: str,
        config: Any,
        *,
        secret_getter: Callable[[str], str | None],
        completion_fn: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(provider_id, config)
        self._secret_getter = secret_getter
        self._completion_fn = completion_fn

    def _completion(self, **kwargs: Any) -> Any:
        if self._completion_fn is None:
            configure_litellm()
            from litellm import completion

            self._completion_fn = completion
        return self._completion_fn(**kwargs)

    def available(self) -> tuple[bool, str]:
        if not self.config.enabled:
            return False, "provider disabled in configuration"
        env_var = self.config.api_key_env
        if env_var and not self._secret_getter(env_var):
            return False, f"{env_var} is not set"
        return True, "ok"

    def litellm_model(self, entry: ModelEntry) -> str:
        if entry.litellm_model:
            return entry.litellm_model
        prefix = self.config.litellm_prefix or ""
        return f"{prefix}{entry.provider_model_id}"

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

        model = self.litellm_model(entry)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "num_retries": 0,
            "timeout": timeout or self.config.timeout_seconds or 120.0,
        }
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if response_format:
            kwargs["response_format"] = response_format
        if self.config.extra_headers:
            kwargs["extra_headers"] = dict(self.config.extra_headers)
        if self.config.api_key_env:
            api_key = self._secret_getter(self.config.api_key_env)
            if api_key:
                kwargs["api_key"] = api_key

        started = time.perf_counter()
        try:
            response = self._completion(**kwargs)
        except BaseException as exc:  # noqa: BLE001 - normalised below
            raise map_provider_exception(exc, provider=self.provider_id, model=entry.id) from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        try:
            choice = response.choices[0]
            text = _content_to_text(choice.message.content)
        except Exception as exc:  # noqa: BLE001
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

        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", None) if usage else None
        output_tokens = getattr(usage, "completion_tokens", None) if usage else None
        cost = getattr(usage, "cost", None) if usage else None
        hidden = getattr(response, "_hidden_params", None) or {}
        if cost is None and isinstance(hidden, dict):
            cost = hidden.get("response_cost")

        return TextCompletion(
            text=text.strip(),
            provider=self.provider_id,
            model=entry.id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=float(cost) if isinstance(cost, (int, float)) else None,
            latency_ms=latency_ms,
            raw=response,
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
                "reported_cost": result.cost,
            },
        )
