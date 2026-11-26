"""Serializable models for deterministic visual composition."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from clipping_ai.clips.editing import Effect

Intensity = Literal["subtle", "balanced", "strong"]


class VisualSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    visual_enhancement: bool = True
    intensity: Intensity = "balanced"
    captions: bool = True
    zoom: bool = True
    punch: bool = True
    emphasis: bool = True
    transitions: bool = True
    callouts: bool = True
    max_effects: int | None = Field(default=None, ge=0, le=4)

    @model_validator(mode="after")
    def _fill_max_effects(self) -> VisualSettings:
        if self.max_effects is None:
            self.max_effects = {"subtle": 1, "balanced": 2, "strong": 3}[self.intensity]
        return self

    def intensity_scale(self) -> float:
        return {"subtle": 0.6, "balanced": 1.0, "strong": 1.25}[self.intensity]

    def category_enabled(self, kind: str) -> bool:
        mapping = {
            "zoom": self.zoom,
            "punch": self.punch,
            "emphasis": self.emphasis,
            "fade_in": self.transitions,
            "fade_out": self.transitions,
        }
        return mapping.get(kind, True)


class Callout(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1, max_length=120)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    kind: Literal["heading", "key_phrase", "number"] = "heading"
    position: Literal["top", "center", "bottom"] = "top"

    @model_validator(mode="after")
    def _valid_range(self) -> Callout:
        if self.end <= self.start:
            raise ValueError("callout end must be greater than start")
        return self

    @property
    def duration(self) -> float:
        return self.end - self.start


class CompositionPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    settings: VisualSettings = Field(default_factory=VisualSettings)
    effects: tuple[Effect, ...] = ()
    callouts: tuple[Callout, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "settings": self.settings.model_dump(mode="json"),
            "effects": [
                {
                    "kind": effect.kind,
                    "start": effect.start,
                    "end": effect.end,
                    "intensity": effect.intensity,
                }
                for effect in self.effects
            ],
            "callouts": [callout.model_dump(mode="json") for callout in self.callouts],
            "notes": list(self.notes),
        }
