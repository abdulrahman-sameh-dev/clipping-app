"""Additive stage observation for highlight discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class StageEvent:
    stage: str
    before: int
    after: int
    details: dict[str, Any] = field(default_factory=dict)


class DiscoveryObserver(Protocol):
    def record(self, event: StageEvent) -> None:
        """Record a discovery stage transition without changing pipeline behavior."""
