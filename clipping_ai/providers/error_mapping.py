"""Shared mapping from provider SDK exceptions to the internal error taxonomy."""

from __future__ import annotations

import re
from typing import Any

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.providers.base import sanitize_message

_QUOTA_MARKERS = (
    "insufficient_quota",
    "insufficient quota",
    "exceeded your current quota",
    "quota exceeded",
    "free tier limit",
    "free quota",
    "credit balance",
    "out of credits",
    "billing hard limit",
    "spend limit",
    "payment required",
)


def parse_retry_after(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    total = 0.0
    matched = False
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", text):
        matched = True
        number = float(amount)
        if unit == "ms":
            total += number / 1000.0
        elif unit == "h":
            total += number * 3600.0
        elif unit == "m":
            total += number * 60.0
        else:
            total += number
    return total if matched else None


def status_code(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def retry_after(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        for key in ("retry-after", "Retry-After", "x-ratelimit-reset-requests"):
            try:
                value = headers.get(key)
            except Exception:
                value = None
            parsed = parse_retry_after(value)
            if parsed is not None:
                return parsed
    return parse_retry_after(getattr(exc, "retry_after", None))


def map_provider_exception(
    exc: BaseException, *, provider: str, model: str | None = None, task: str | None = None
) -> AIError:
    if isinstance(exc, AIError):
        return exc

    message = sanitize_message(str(exc))
    lowered = message.lower()
    name = type(exc).__name__.lower()
    status = status_code(exc)
    wait = retry_after(exc)

    if any(marker in lowered for marker in _QUOTA_MARKERS):
        return AIError(
            ErrorCategory.FREE_QUOTA_EXHAUSTED,
            f"Free quota appears to be exhausted for {provider}: {message}",
            provider=provider,
            model=model,
            task=task,
            retryable=False,
            retry_after=wait,
        )

    if status == 429 or "ratelimit" in name or "rate limit" in lowered:
        return AIError(
            ErrorCategory.RATE_LIMIT_EXCEEDED,
            f"Rate limit exceeded for {provider}: {message}",
            provider=provider,
            model=model,
            task=task,
            retryable=True,
            retry_after=wait,
        )

    if status in (401, 403) or "authentication" in name or "permission" in name:
        if "model" in lowered and ("not found" in lowered or "not available" in lowered):
            category = ErrorCategory.MODEL_UNAVAILABLE
        else:
            category = ErrorCategory.AUTHENTICATION_FAILED
        return AIError(
            category,
            f"Authentication/permission problem for {provider}: {message}",
            provider=provider,
            model=model,
            task=task,
            retryable=False,
        )

    if "apikey" in name or (
        "api key" in lowered and ("not set" in lowered or "missing" in lowered or "no api" in lowered)
    ):
        return AIError(
            ErrorCategory.MISSING_API_KEY,
            f"Provider {provider} has no usable API key: {message}",
            provider=provider,
            model=model,
            task=task,
            retryable=False,
        )

    if status == 404 or "notfound" in name or "model_not_found" in lowered:
        return AIError(
            ErrorCategory.MODEL_UNAVAILABLE,
            f"Model is unavailable on {provider}: {message}",
            provider=provider,
            model=model,
            task=task,
            retryable=False,
        )

    if "jsonschema" in name or ("schema" in name and "validation" in lowered):
        return AIError(
            ErrorCategory.SCHEMA_VALIDATION_FAILED,
            f"Provider rejected the JSON schema: {message}",
            provider=provider,
            model=model,
            task=task,
            retryable=True,
        )

    if "timeout" in name or status in (408, 504):
        return AIError(
            ErrorCategory.NETWORK_TIMEOUT,
            f"Request to {provider} timed out: {message}",
            provider=provider,
            model=model,
            task=task,
            retryable=True,
        )

    if "connection" in name or "serviceunavailable" in name or (status is not None and status >= 500):
        return AIError(
            ErrorCategory.PROVIDER_UNAVAILABLE,
            f"Provider {provider} is unavailable: {message}",
            provider=provider,
            model=model,
            task=task,
            retryable=True,
            retry_after=wait,
        )

    if status in (400, 422) or "badrequest" in name or "contextwindow" in name:
        return AIError(
            ErrorCategory.INVALID_REQUEST,
            f"Invalid request to {provider}: {message}",
            provider=provider,
            model=model,
            task=task,
            retryable=False,
        )

    return AIError(
        ErrorCategory.UNKNOWN_PROVIDER_ERROR,
        f"Unknown {provider} error: {message}",
        provider=provider,
        model=model,
        task=task,
        retryable=True,
        retry_after=wait,
    )
