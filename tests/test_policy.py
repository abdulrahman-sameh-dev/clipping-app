from __future__ import annotations

import pytest

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.policy import FreeOnlyPolicy
from clipping_ai.registry import ModelEntry, PolicyConfig


def model(**overrides):
    data = {
        "id": "m",
        "provider": "p",
        "provider_model_id": "m",
        "tasks": ["content_analysis"],
        "free": True,
        "free_verified": True,
        "status": "enabled",
    }
    data.update(overrides)
    return ModelEntry.model_validate(data)


def test_disabled_model_is_blocked():
    policy = FreeOnlyPolicy(PolicyConfig())
    verdict = policy.evaluate(model(status="disabled"), "content_analysis")
    assert verdict.allowed is False
    assert verdict.category == ErrorCategory.POLICY_VIOLATION


def test_paid_model_is_blocked_even_when_enabled():
    policy = FreeOnlyPolicy(PolicyConfig())
    verdict = policy.evaluate(model(free=False), "content_analysis")
    assert verdict.allowed is False
    assert "not explicitly approved as free" in verdict.reason
    with pytest.raises(AIError) as exc:
        policy.assert_free(model(free=False))
    assert exc.value.category == ErrorCategory.POLICY_VIOLATION


def test_task_support_is_required():
    policy = FreeOnlyPolicy(PolicyConfig())
    verdict = policy.evaluate(model(), "translation")
    assert verdict.allowed is False
    assert verdict.category == ErrorCategory.UNSUPPORTED_CAPABILITY


def test_require_verified_free_blocks_unverified():
    policy = FreeOnlyPolicy(PolicyConfig(require_verified_free=True))
    verdict = policy.evaluate(model(free_verified=False), "content_analysis")
    assert verdict.allowed is False
    assert "not verified" in verdict.reason


def test_unverified_manual_approval_can_be_allowed():
    policy = FreeOnlyPolicy(
        PolicyConfig(
            require_verified_free=False,
            allow_unverified_free_if_manually_approved=True,
        )
    )
    assert policy.evaluate(model(free_verified=False), "content_analysis").allowed is True


def test_fallback_policy_allows_free_quota_failover_by_default():
    policy = FreeOnlyPolicy(PolicyConfig())
    assert policy.can_fallback(
        AIError(ErrorCategory.RATE_LIMIT_EXCEEDED, "slow down", retryable=True)
    )
    assert policy.can_fallback(
        AIError(ErrorCategory.FREE_QUOTA_EXHAUSTED, "quota", retryable=False)
    )
    assert not policy.can_fallback(
        AIError(ErrorCategory.MISSING_API_KEY, "key", retryable=False)
    )


def test_fallback_policy_can_keep_quota_non_retryable():
    policy = FreeOnlyPolicy(PolicyConfig(fallback_on_quota_exhausted=False))
    assert not policy.can_fallback(
        AIError(ErrorCategory.FREE_QUOTA_EXHAUSTED, "quota", retryable=False)
    )
