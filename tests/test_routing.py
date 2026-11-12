from __future__ import annotations

from clipping_ai.policy import FreeOnlyPolicy
from clipping_ai.registry import PolicyConfig, ProviderConfig, RouteCandidate
from clipping_ai.routing import Router
from tests.helpers import make_registry, text_model


def registry():
    providers = {
        "p1": ProviderConfig(type="litellm", enabled=True),
        "p2": ProviderConfig(type="litellm", enabled=True),
        "off": ProviderConfig(type="litellm", enabled=False),
    }
    models = [
        text_model("free-1", provider="p1"),
        text_model("paid-1", provider="p1", free=False),
        text_model("free-2", provider="p2"),
        text_model("off-1", provider="off"),
        text_model("translation-only", provider="p2", tasks=["translation"]),
    ]
    routing = {
        "content_analysis": [
            RouteCandidate(provider="p1", model="paid-1"),
            RouteCandidate(provider="p1", model="free-1"),
            RouteCandidate(provider="off", model="off-1"),
            RouteCandidate(provider="p2", model="free-2"),
            RouteCandidate(provider="p2", model="translation-only"),
        ]
    }
    return make_registry(
        providers=providers,
        models=models,
        routing=routing,
        policy=PolicyConfig(max_fallback_candidates=10),
    )


def test_router_filters_policy_provider_and_task_eligibility():
    reg = registry()
    result = Router(reg, FreeOnlyPolicy(reg.policy)).resolve("content_analysis")
    assert [candidate.entry.id for candidate in result.candidates] == ["free-1", "free-2"]
    reasons = {item.model: item.category.value for item in result.skipped}
    assert reasons["paid-1"] == "policy_violation"
    assert reasons["off-1"] == "provider_unavailable"
    assert reasons["translation-only"] == "unsupported_capability"


def test_explicit_paid_model_is_never_a_candidate():
    reg = registry()
    result = Router(reg, FreeOnlyPolicy(reg.policy)).resolve(
        "content_analysis", model="paid-1"
    )
    assert result.candidates == []
    assert result.skipped[0].category.value == "policy_violation"


def test_explicit_model_alias_is_resolved_only_if_registered():
    reg = registry()
    result = Router(reg, FreeOnlyPolicy(reg.policy)).resolve(
        "content_analysis", model="free-1"
    )
    assert [candidate.entry.id for candidate in result.candidates] == ["free-1"]

    missing = Router(reg, FreeOnlyPolicy(reg.policy)).resolve(
        "content_analysis", model="not-registered"
    )
    assert missing.candidates == []
    assert missing.skipped[0].category.value == "policy_violation"


def test_provider_override_keeps_only_that_provider_and_task():
    reg = registry()
    result = Router(reg, FreeOnlyPolicy(reg.policy)).resolve(
        "content_analysis", provider="p2"
    )
    assert [candidate.entry.id for candidate in result.candidates] == ["free-2"]


def test_max_fallback_candidates_is_enforced():
    reg = registry()
    reg.policy.max_fallback_candidates = 1
    result = Router(reg, FreeOnlyPolicy(reg.policy)).resolve("content_analysis")
    assert [candidate.entry.id for candidate in result.candidates] == ["free-1"]
