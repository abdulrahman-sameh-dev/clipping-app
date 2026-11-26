"""Build a deterministic composition plan from existing edit decisions."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

from clipping_ai.clips.config import ClipConfig
from clipping_ai.clips.editing import EditPlan, Effect, is_arabic_text
from clipping_ai.composition.models import Callout, CompositionPlan, VisualSettings
from clipping_ai.highlights.models import Highlight


def _scale_effects(plan: EditPlan, settings: VisualSettings) -> tuple[Effect, ...]:
    if not settings.visual_enhancement:
        return ()
    scale = settings.intensity_scale()
    selected: list[Effect] = []
    for effect in plan.effects:
        if not settings.category_enabled(effect.kind):
            continue
        intensity = effect.intensity
        if effect.kind in {"zoom", "punch", "emphasis"}:
            intensity = min(0.2, max(0.0, intensity * scale))
        selected.append(replace(effect, intensity=round(intensity, 4)))
    visual = [effect for effect in selected if effect.kind in {"zoom", "punch", "emphasis"}]
    if len(visual) > (settings.max_effects or 0):
        allowed = set(id(effect) for effect in visual[: settings.max_effects or 0])
        selected = [
            effect
            for effect in selected
            if effect.kind in {"fade_in", "fade_out"} or id(effect) in allowed
        ]
    return tuple(selected)


def _callout_text(highlight: Highlight | None) -> str:
    if highlight is None:
        return ""
    text = " ".join((highlight.title or "").split())
    if not text:
        return ""
    if len(text) > 48:
        text = text[:47].rstrip() + "…"
    return text


def _build_callouts(
    *,
    highlight: Highlight | None,
    start_time: float,
    end_time: float,
    settings: VisualSettings,
    config: ClipConfig,
) -> tuple[Callout, ...]:
    if not settings.visual_enhancement or not settings.callouts or highlight is None:
        return ()
    duration = max(0.0, end_time - start_time)
    if duration < 3.0:
        return ()
    if highlight.confidence < config.effects_min_importance:
        return ()
    text = _callout_text(highlight)
    if not text:
        return ()
    callout_start = min(0.8, max(0.2, duration * 0.1))
    callout_end = min(duration, callout_start + min(3.0, duration * 0.4))
    if callout_end - callout_start < 1.0:
        return ()
    kind: Literal["heading", "key_phrase", "number"] = (
        "number" if any(char.isdigit() for char in text) else "heading"
    )
    return (
        Callout(
            text=text,
            start=round(callout_start, 3),
            end=round(callout_end, 3),
            kind=kind,
            position="top",
        ),
    )


def build_composition_plan(
    edit_plan: EditPlan,
    *,
    highlight: Highlight | None,
    start_time: float,
    end_time: float,
    settings: VisualSettings | None = None,
    config: ClipConfig,
) -> CompositionPlan:
    settings = settings or VisualSettings()
    notes: list[str] = []
    if not settings.visual_enhancement:
        notes.append("visual enhancement disabled; captions render unchanged")
    effects = _scale_effects(edit_plan, settings)
    callouts = _build_callouts(
        highlight=highlight,
        start_time=start_time,
        end_time=end_time,
        settings=settings,
        config=config,
    )
    if callouts:
        notes.append("one restrained top-safe-area callout generated from the highlight title")
    return CompositionPlan(
        settings=settings,
        effects=effects,
        callouts=callouts,
        notes=tuple(notes),
    )


def plan_language(callouts: tuple[Callout, ...]) -> str:
    return "ar" if any(is_arabic_text(callout.text) for callout in callouts) else "en"
