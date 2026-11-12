"""Retry helpers: exponential backoff, jitter and Retry-After support."""

from __future__ import annotations

import random

from clipping_ai.registry import RetryConfig


def compute_delay(
    attempt: int,
    config: RetryConfig,
    *,
    retry_after: float | None = None,
    rng: random.Random | None = None,
) -> float:
    """Return seconds to wait before the next attempt (attempt is 1-based)."""
    if retry_after is not None and retry_after >= 0:
        return min(retry_after, config.max_retry_after_seconds)
    if rng is None:
        rng = random.Random()
    base = config.base_delay_seconds * (2 ** max(attempt - 1, 0))
    jitter = rng.uniform(0.0, config.jitter_seconds)
    return min(base + jitter, config.max_delay_seconds)
