from __future__ import annotations

import random

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.providers.base import sanitize_message
from clipping_ai.providers.error_mapping import (
    map_provider_exception,
    parse_retry_after,
    status_code,
)
from clipping_ai.registry import RetryConfig
from clipping_ai.retry import compute_delay


class FakeResponse:
    def __init__(self, status_code: int, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class FakeProviderError(Exception):
    def __init__(self, message: str, status_code: int | None = None, headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = FakeResponse(status_code, headers) if status_code else None


def test_quota_mapping_has_no_retry():
    error = map_provider_exception(
        FakeProviderError("insufficient_quota"), provider="p", model="m"
    )
    assert error.category == ErrorCategory.FREE_QUOTA_EXHAUSTED
    assert error.retryable is False


def test_rate_limit_mapping_is_retryable():
    error = map_provider_exception(
        FakeProviderError("rate limit", status_code=429, headers={"retry-after": "2"}),
        provider="p",
    )
    assert error.category == ErrorCategory.RATE_LIMIT_EXCEEDED
    assert error.retryable is True
    assert error.retry_after == 2.0


def test_auth_and_missing_key_mapping():
    auth = map_provider_exception(
        FakeProviderError("unauthorized", status_code=401), provider="p"
    )
    assert auth.category == ErrorCategory.AUTHENTICATION_FAILED
    missing = map_provider_exception(
        FakeProviderError("api key not set"), provider="p"
    )
    assert missing.category == ErrorCategory.MISSING_API_KEY


def test_timeout_and_provider_mapping():
    timeout = map_provider_exception(TimeoutError("timed out"), provider="p")
    assert timeout.category == ErrorCategory.NETWORK_TIMEOUT
    unavailable = map_provider_exception(
        FakeProviderError("boom", status_code=503), provider="p"
    )
    assert unavailable.category == ErrorCategory.PROVIDER_UNAVAILABLE


def test_retry_after_parser():
    assert parse_retry_after("2") == 2.0
    assert parse_retry_after("1m30s") == 90.0
    assert parse_retry_after("500ms") == 0.5
    assert parse_retry_after("nonsense") is None


def test_status_code_from_exception_or_response():
    assert status_code(FakeProviderError("x", status_code=404)) == 404
    assert status_code(Exception("x")) is None


def test_compute_delay_uses_retry_after_and_jitter():
    config = RetryConfig(
        base_delay_seconds=1.0,
        max_delay_seconds=30.0,
        jitter_seconds=0.5,
        max_retry_after_seconds=60.0,
    )
    assert compute_delay(1, config, retry_after=3.0) == 3.0
    assert compute_delay(2, config, rng=random.Random(0)) >= 2.0


def test_error_serialization_has_stable_shape():
    error = AIError(
        ErrorCategory.PROVIDER_UNAVAILABLE,
        "provider failed",
        provider="p",
        model="m",
    )
    payload = error.to_dict()
    assert payload["category"] == "provider_unavailable"
    assert payload["provider"] == "p"
    assert payload["model"] == "m"


def test_sanitize_message_redacts_common_secret_shapes():
    assert "sk-abcdefghijklmnop" not in sanitize_message("bad key sk-abcdefghijklmnop")
    assert "gsk_abcdefghijklmnop" not in sanitize_message("bad key gsk_abcdefghijklmnop")
    assert "AIzaabcdefghijklmnopqrstuv" not in sanitize_message(
        "bad key AIzaabcdefghijklmnopqrstuv"
    )
