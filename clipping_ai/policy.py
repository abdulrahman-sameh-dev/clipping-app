"""Hard zero-cost policy: only explicitly approved free models may execute."""

from __future__ import annotations

from dataclasses import dataclass

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.registry import ModelEntry, PolicyConfig


@dataclass(frozen=True)
class Eligibility:
    allowed: bool
    reason: str = ""
    category: ErrorCategory | None = None


class FreeOnlyPolicy:
    """Evaluates whether a registry model may be used for a task.

    The policy is deliberately allow-list based: a model is blocked unless the
    local configuration explicitly sets ``free: true`` and enables it.
    """

    def __init__(self, config: PolicyConfig) -> None:
        self.config = config

    def evaluate(self, entry: ModelEntry, task: str) -> Eligibility:
        if not entry.is_enabled():
            return Eligibility(False, "model is disabled in the registry", ErrorCategory.POLICY_VIOLATION)
        if not entry.free:
            return Eligibility(
                False,
                "model is not explicitly approved as free (free=false)",
                ErrorCategory.POLICY_VIOLATION,
            )
        if not entry.supports_task(task):
            return Eligibility(
                False,
                f"model does not support task '{task}'",
                ErrorCategory.UNSUPPORTED_CAPABILITY,
            )
        if self.config.require_verified_free and not entry.free_verified:
            return Eligibility(
                False,
                "model free eligibility is not verified and require_verified_free=true",
                ErrorCategory.POLICY_VIOLATION,
            )
        if (
            not entry.free_verified
            and not self.config.allow_unverified_free_if_manually_approved
        ):
            return Eligibility(
                False,
                "unverified free models are not allowed by policy",
                ErrorCategory.POLICY_VIOLATION,
            )
        return Eligibility(True)

    def assert_allowed(self, entry: ModelEntry, task: str) -> None:
        verdict = self.evaluate(entry, task)
        if not verdict.allowed:
            raise AIError(
                verdict.category or ErrorCategory.POLICY_VIOLATION,
                verdict.reason,
                provider=entry.provider,
                model=entry.id,
                task=task,
                retryable=False,
            )

    def assert_free(self, entry: ModelEntry) -> None:
        """Last-line guard placed immediately before any provider call."""
        if not entry.free:
            raise AIError(
                ErrorCategory.POLICY_VIOLATION,
                "Blocked: the selected model is not explicitly approved as free.",
                provider=entry.provider,
                model=entry.id,
                retryable=False,
            )

    def can_fallback(self, error: AIError) -> bool:
        if not error.can_fallback():
            return False
        mapping = {
            ErrorCategory.RATE_LIMIT_EXCEEDED: self.config.fallback_on_rate_limit,
            ErrorCategory.FREE_QUOTA_EXHAUSTED: self.config.fallback_on_quota_exhausted,
            ErrorCategory.PROVIDER_UNAVAILABLE: self.config.fallback_on_provider_unavailable,
            ErrorCategory.MODEL_UNAVAILABLE: self.config.fallback_on_model_unavailable,
            ErrorCategory.NETWORK_TIMEOUT: self.config.fallback_on_timeout,
            ErrorCategory.INVALID_RESPONSE: self.config.fallback_on_invalid_response,
            ErrorCategory.SCHEMA_VALIDATION_FAILED: self.config.fallback_on_schema_validation,
        }
        return mapping.get(error.category, False)

    def describe(self) -> dict[str, bool | int]:
        return {
            "require_verified_free": self.config.require_verified_free,
            "allow_unverified_free_if_manually_approved": (
                self.config.allow_unverified_free_if_manually_approved
            ),
            "fallback_on_rate_limit": self.config.fallback_on_rate_limit,
            "fallback_on_quota_exhausted": self.config.fallback_on_quota_exhausted,
            "schema_validation_retries": self.config.schema_validation_retries,
            "max_fallback_candidates": self.config.max_fallback_candidates,
        }
