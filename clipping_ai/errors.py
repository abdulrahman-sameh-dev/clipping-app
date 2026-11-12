"""Typed error taxonomy shared by every provider, the manager and the API."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    MISSING_API_KEY = "missing_api_key"
    AUTHENTICATION_FAILED = "authentication_failed"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    FREE_QUOTA_EXHAUSTED = "free_quota_exhausted"
    MODEL_UNAVAILABLE = "model_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    NETWORK_TIMEOUT = "network_timeout"
    INVALID_RESPONSE = "invalid_response"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    INVALID_REQUEST = "invalid_request"
    POLICY_VIOLATION = "policy_violation"
    NOT_FOUND = "not_found"
    INTERNAL_ERROR = "internal_error"
    UNKNOWN_PROVIDER_ERROR = "unknown_provider_error"


# Errors that are worth retrying against the *same* provider/model.
RETRYABLE_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {
        ErrorCategory.RATE_LIMIT_EXCEEDED,
        ErrorCategory.PROVIDER_UNAVAILABLE,
        ErrorCategory.NETWORK_TIMEOUT,
        ErrorCategory.INVALID_RESPONSE,
        ErrorCategory.UNKNOWN_PROVIDER_ERROR,
    }
)

# Errors where switching to another *free* provider may be legitimate.
# Authentication, missing keys, invalid input and policy violations are excluded:
# they need to be reported to the operator rather than hidden by a fallback.
FALLBACK_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {
        ErrorCategory.RATE_LIMIT_EXCEEDED,
        ErrorCategory.MODEL_UNAVAILABLE,
        ErrorCategory.PROVIDER_UNAVAILABLE,
        ErrorCategory.NETWORK_TIMEOUT,
        ErrorCategory.INVALID_RESPONSE,
        ErrorCategory.SCHEMA_VALIDATION_FAILED,
    }
)

# Never fall back for these, even if a free alternative exists.
NON_FALLBACK_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {
        ErrorCategory.MISSING_API_KEY,
        ErrorCategory.AUTHENTICATION_FAILED,
        ErrorCategory.INVALID_REQUEST,
        ErrorCategory.UNSUPPORTED_CAPABILITY,
        ErrorCategory.POLICY_VIOLATION,
        ErrorCategory.FREE_QUOTA_EXHAUSTED,
    }
)

_HTTP_STATUS: dict[ErrorCategory, int] = {
    ErrorCategory.MISSING_API_KEY: 503,
    ErrorCategory.AUTHENTICATION_FAILED: 502,
    ErrorCategory.RATE_LIMIT_EXCEEDED: 429,
    ErrorCategory.FREE_QUOTA_EXHAUSTED: 429,
    ErrorCategory.MODEL_UNAVAILABLE: 404,
    ErrorCategory.PROVIDER_UNAVAILABLE: 503,
    ErrorCategory.NETWORK_TIMEOUT: 504,
    ErrorCategory.INVALID_RESPONSE: 502,
    ErrorCategory.SCHEMA_VALIDATION_FAILED: 502,
    ErrorCategory.UNSUPPORTED_CAPABILITY: 422,
    ErrorCategory.INVALID_REQUEST: 400,
    ErrorCategory.POLICY_VIOLATION: 403,
    ErrorCategory.NOT_FOUND: 404,
    ErrorCategory.INTERNAL_ERROR: 500,
    ErrorCategory.UNKNOWN_PROVIDER_ERROR: 502,
}


class AIError(Exception):
    """A provider-independent error with enough context for the UI and logs."""

    def __init__(
        self,
        category: ErrorCategory | str,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        task: str | None = None,
        retryable: bool | None = None,
        retry_after: float | None = None,
        details: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.category = ErrorCategory(category)
        self.message = message
        self.provider = provider
        self.model = model
        self.task = task
        self.retry_after = retry_after
        self.details = details or {}
        self.cause = cause
        if retryable is None:
            retryable = self.category in RETRYABLE_CATEGORIES
        self.retryable = retryable
        super().__init__(self._render())

    def _render(self) -> str:
        where = ""
        if self.provider:
            where = f" [{self.provider}"
            if self.model:
                where += f"/{self.model}"
            where += "]"
        return f"{self.category.value}{where}: {self.message}"

    @property
    def http_status(self) -> int:
        return _HTTP_STATUS.get(self.category, 502)

    def can_fallback(self) -> bool:
        return self.category in FALLBACK_CATEGORIES and self.category not in NON_FALLBACK_CATEGORIES

    def to_dict(self) -> dict[str, Any]:
        """Safe for API responses: never contains credentials."""
        return {
            "category": self.category.value,
            "message": self.message,
            "provider": self.provider,
            "model": self.model,
            "task": self.task,
            "retryable": self.retryable,
            "retry_after": self.retry_after,
            "details": self.details,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AIError({self._render()!r})"


def missing_key(provider: str, env_var: str, *, model: str | None = None) -> AIError:
    return AIError(
        ErrorCategory.MISSING_API_KEY,
        f"Provider '{provider}' is disabled because {env_var} is not set. "
        "Add it to your environment or .env file; other providers remain available.",
        provider=provider,
        model=model,
        retryable=False,
    )


def policy_violation(message: str, *, provider: str | None = None, model: str | None = None) -> AIError:
    return AIError(
        ErrorCategory.POLICY_VIOLATION,
        message,
        provider=provider,
        model=model,
        retryable=False,
    )


def quota_exhausted(provider: str, model: str | None, message: str, *, details: dict[str, Any] | None = None) -> AIError:
    return AIError(
        ErrorCategory.FREE_QUOTA_EXHAUSTED,
        message,
        provider=provider,
        model=model,
        retryable=False,
        details=details,
    )
