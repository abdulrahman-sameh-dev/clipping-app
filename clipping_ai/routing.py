"""Task-based routing resolution, independent of provider implementations."""

from __future__ import annotations

from dataclasses import dataclass, field

from clipping_ai.errors import ErrorCategory
from clipping_ai.policy import FreeOnlyPolicy
from clipping_ai.registry import ModelEntry, ProviderConfig, Registry


@dataclass(frozen=True)
class ResolvedCandidate:
    entry: ModelEntry
    provider: ProviderConfig


@dataclass(frozen=True)
class SkippedCandidate:
    provider: str
    model: str
    reason: str
    category: ErrorCategory


@dataclass
class RoutingResult:
    candidates: list[ResolvedCandidate] = field(default_factory=list)
    skipped: list[SkippedCandidate] = field(default_factory=list)

    def add_skip(self, provider: str, model: str, reason: str, category: ErrorCategory) -> None:
        self.skipped.append(SkippedCandidate(provider, model, reason, category))


class Router:
    def __init__(self, registry: Registry, policy: FreeOnlyPolicy) -> None:
        self.registry = registry
        self.policy = policy

    def resolve(
        self,
        task: str,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> RoutingResult:
        result = RoutingResult()

        if provider or model:
            raw_candidates = self._explicit_candidates(task, provider=provider, model=model, result=result)
        else:
            raw_candidates = self._routed_candidates(task, result=result)

        seen: set[str] = set()
        for provider_id, model_id in raw_candidates:
            if model_id in seen:
                continue
            seen.add(model_id)

            entry = self.registry.get_model(model_id)
            if entry is None:
                result.add_skip(
                    provider_id,
                    model_id,
                    f"model '{model_id}' is not in the registry",
                    ErrorCategory.POLICY_VIOLATION,
                )
                continue
            if provider and entry.provider != provider:
                result.add_skip(
                    provider_id,
                    model_id,
                    f"model belongs to provider '{entry.provider}', not '{provider}'",
                    ErrorCategory.INVALID_REQUEST,
                )
                continue

            provider_cfg = self.registry.get_provider(entry.provider)
            if provider_cfg is None:
                result.add_skip(
                    entry.provider,
                    entry.id,
                    "provider is not configured",
                    ErrorCategory.PROVIDER_UNAVAILABLE,
                )
                continue
            if not provider_cfg.enabled:
                result.add_skip(
                    entry.provider,
                    entry.id,
                    "provider is disabled in configuration",
                    ErrorCategory.PROVIDER_UNAVAILABLE,
                )
                continue

            verdict = self.policy.evaluate(entry, task)
            if not verdict.allowed:
                result.add_skip(
                    entry.provider,
                    entry.id,
                    verdict.reason,
                    verdict.category or ErrorCategory.POLICY_VIOLATION,
                )
                continue

            result.candidates.append(ResolvedCandidate(entry=entry, provider=provider_cfg))

        limit = self.registry.policy.max_fallback_candidates
        if limit > 0:
            result.candidates = result.candidates[:limit]
        return result

    def _routed_candidates(
        self, task: str, *, result: RoutingResult
    ) -> list[tuple[str, str]]:
        routed = self.registry.candidates_for_task(task)
        if not routed:
            result.add_skip(
                "*",
                "*",
                f"no routing configured for task '{task}'",
                ErrorCategory.INVALID_REQUEST,
            )
        return [(c.provider, c.model) for c in routed]

    def _explicit_candidates(
        self,
        task: str,
        *,
        provider: str | None,
        model: str | None,
        result: RoutingResult,
    ) -> list[tuple[str, str]]:
        if model:
            entry = self.registry.get_model(model)
            if entry is None:
                # Allow provider_model_id as an alias, but never an unregistered model.
                matches = [
                    m
                    for m in self.registry.models.values()
                    if m.provider_model_id == model and (provider is None or m.provider == provider)
                ]
                if len(matches) == 1:
                    return [(matches[0].provider, matches[0].id)]
                result.add_skip(
                    provider or "*",
                    model,
                    "requested model is not present in the free-model registry",
                    ErrorCategory.POLICY_VIOLATION,
                )
                return []
            return [(entry.provider, entry.id)]

        assert provider is not None
        # Provider-only override: keep registry order, but only for this task.
        return [
            (m.provider, m.id)
            for m in self.registry.models_for_provider(provider)
            if m.supports_task(task)
        ]
