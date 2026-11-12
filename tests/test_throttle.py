from __future__ import annotations

import pytest

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.throttle import Throttle


@pytest.fixture
def fake_clock(monkeypatch):
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr("clipping_ai.throttle.time.monotonic", lambda: now[0])
    monkeypatch.setattr("clipping_ai.throttle.time.sleep", sleep)
    return now


def test_throttle_allows_first_request_and_resets(fake_clock):
    throttle = Throttle()
    throttle.acquire("k", requests_per_minute=1, min_interval_seconds=1)
    throttle.reset()
    throttle.acquire("k", requests_per_minute=1, min_interval_seconds=1)


def test_throttle_waits_for_min_interval(fake_clock):
    throttle = Throttle()
    throttle.acquire("k", min_interval_seconds=2)
    throttle.acquire("k", min_interval_seconds=2)
    assert fake_clock[0] == 2


def test_throttle_rejects_excessive_min_interval_wait(fake_clock):
    throttle = Throttle()
    throttle.acquire("k", min_interval_seconds=10)
    with pytest.raises(AIError) as exc:
        throttle.acquire("k", min_interval_seconds=10, max_wait_seconds=1)
    assert exc.value.category == ErrorCategory.RATE_LIMIT_EXCEEDED
    assert exc.value.retry_after == pytest.approx(10)


def test_throttle_rate_window_waits_then_allows(fake_clock):
    throttle = Throttle()
    throttle.acquire("k", requests_per_minute=2)
    throttle.acquire("k", requests_per_minute=2)
    throttle.acquire("k", requests_per_minute=2, max_wait_seconds=120)
    assert fake_clock[0] == 60


def test_throttle_rate_window_rejects_long_wait(fake_clock):
    throttle = Throttle()
    throttle.acquire("k", requests_per_minute=1)
    with pytest.raises(AIError) as exc:
        throttle.acquire("k", requests_per_minute=1, max_wait_seconds=1)
    assert exc.value.category == ErrorCategory.RATE_LIMIT_EXCEEDED
