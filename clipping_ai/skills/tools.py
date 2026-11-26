"""Registered built-in tools available to skills.

External manifests can reference these names, but they cannot provide or import
new executable code. This keeps skill installation data-only and reviewable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    handler: Callable[[dict[str, Any]], dict[str, Any]]
    requires_network: bool = False
    requires_filesystem: bool = False
    requires_subprocess: bool = False


def _caption_plan(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool": "composition.caption_plan",
        "caption_mode": "phrase",
        "use_existing_word_timestamps": True,
        "transcribe_again": False,
        "target_language": context.get("target_language"),
    }


def _moment_plan(context: dict[str, Any]) -> dict[str, Any]:
    duration = float(context.get("duration") or 0.0)
    return {
        "tool": "composition.moment_plan",
        "max_visual_effects": 2 if duration < 60 else 3,
        "effects": ["zoom", "punch", "emphasis"],
        "avoid_synthetic_media": True,
    }


def _callout_plan(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool": "composition.callout_plan",
        "max_callouts": 1,
        "position": "top",
        "source": "highlight_title",
    }


def _explainer_plan(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool": "analysis.explainer_plan",
        "mode": "conservative",
        "max_callouts": 1,
        "requires_existing_analysis": True,
    }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        for spec in (
            ToolSpec("composition.caption_plan", "Plan phrase captions", _caption_plan),
            ToolSpec("composition.moment_plan", "Plan restrained emphasis", _moment_plan),
            ToolSpec("composition.callout_plan", "Plan one safe-area callout", _callout_plan),
            ToolSpec("analysis.explainer_plan", "Plan explanatory emphasis", _explainer_plan),
        ):
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "requires_network": spec.requires_network,
                "requires_filesystem": spec.requires_filesystem,
                "requires_subprocess": spec.requires_subprocess,
            }
            for spec in self._tools.values()
        ]
