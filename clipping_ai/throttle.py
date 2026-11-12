"""In-process request throttling for a single local user."""

from __future__ import annotations

import threading
import time
from collections import deque

from clipping_ai.errors import AIError, ErrorCategory


class Throttle:
    """A small per-key token/interval limiter.

    It intentionally does not try to model provider quotas. It only protects the
    user from obviously exceeding configured local request rates.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = {}
        self._last: dict[str, float] = {}

    def acquire(
        self,
        key: str,
        *,
        requests_per_minute: int = 0,
        min_interval_seconds: float = 0.0,
        max_wait_seconds: float = 60.0,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        with self._lock:
            now = time.monotonic()

            if min_interval_seconds > 0:
                last = self._last.get(key)
                if last is not None:
                    wait = last + min_interval_seconds - now
                    if wait > 0:
                        if wait > max_wait_seconds:
                            raise AIError(
                                ErrorCategory.RATE_LIMIT_EXCEEDED,
                                f"Local throttle would require waiting {wait:.1f}s "
                                f"(limit {max_wait_seconds:.0f}s).",
                                provider=provider,
                                model=model,
                                retry_after=wait,
                            )
                        time.sleep(wait)
                        now = time.monotonic()

            if requests_per_minute > 0:
                window = self._events.setdefault(key, deque())
                cutoff = now - 60.0
                while window and window[0] <= cutoff:
                    window.popleft()
                if len(window) >= requests_per_minute:
                    wait = window[0] + 60.0 - now
                    if wait > max_wait_seconds:
                        raise AIError(
                            ErrorCategory.RATE_LIMIT_EXCEEDED,
                            f"Local rate limit of {requests_per_minute}/min reached; "
                            f"retry in {wait:.0f}s.",
                            provider=provider,
                            model=model,
                            retry_after=wait,
                        )
                    time.sleep(max(wait, 0.0))
                    now = time.monotonic()
                    cutoff = now - 60.0
                    while window and window[0] <= cutoff:
                        window.popleft()
                window.append(now)

            self._last[key] = now

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._last.clear()
