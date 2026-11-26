"""Reusable Pydantic schemas for every structured AI response."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class TranscriptWord(StrictModel):
    word: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    probability: float | None = Field(default=None, ge=0, le=1)


class TranscriptSegment(StrictModel):
    id: int | None = None
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    words: list[TranscriptWord] | None = None

    @model_validator(mode="after")
    def _end_after_start(self) -> TranscriptSegment:
        if self.end < self.start:
            raise ValueError("segment end must be >= start")
        return self


class TranscriptResult(StrictModel):
    text: str
    language: str | None = None
    duration: float | None = Field(default=None, ge=0)
    segments: list[TranscriptSegment] = Field(default_factory=list)
    words: list[TranscriptWord] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None


class HighlightSuggestion(StrictModel):
    start_time: float = Field(ge=0, description="Clip start in seconds")
    end_time: float = Field(ge=0, description="Clip end in seconds")
    suggested_title: str
    summary: str
    hook: str
    reasoning: str
    score: float = Field(ge=0, le=100)
    category: str
    transcript_excerpt: str

    @model_validator(mode="after")
    def _end_after_start(self) -> HighlightSuggestion:
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be greater than start_time")
        return self


class HighlightAnalysis(StrictModel):
    highlights: list[HighlightSuggestion]
    language: str | None = None
    notes: str | None = None


class ClipEvaluation(StrictModel):
    hook_strength: float = Field(ge=0, le=100)
    context_completeness: float = Field(ge=0, le=100)
    informational_value: float = Field(ge=0, le=100)
    emotional_engagement: float = Field(ge=0, le=100)
    standalone_quality: float = Field(ge=0, le=100)
    overall_score: float = Field(ge=0, le=100)


class GeneratedMetadata(StrictModel):
    title: str
    description: str
    hashtags: list[str] = Field(default_factory=list)
    language: str


class GeneratedMetadataItem(GeneratedMetadata):
    clip_id: str = Field(min_length=1, max_length=64)


class GeneratedMetadataBatch(StrictModel):
    items: list[GeneratedMetadataItem] = Field(default_factory=list)


class TranslationResult(StrictModel):
    translated_text: str
    source_language: str | None = None
    target_language: str
    notes: str | None = None


class CaptionSegment(StrictModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str

    @model_validator(mode="after")
    def _end_after_start(self) -> CaptionSegment:
        if self.end < self.start:
            raise ValueError("caption end must be >= start")
        return self


class CaptionResult(StrictModel):
    captions: list[CaptionSegment]
    language: str | None = None


class ContentAnalysis(StrictModel):
    summary: str
    key_points: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    sentiment: Literal["positive", "negative", "neutral", "mixed"] | None = None
    content_type: str | None = None
    language: str | None = None


TASK_SCHEMAS: dict[str, type[BaseModel]] = {
    "content_analysis": ContentAnalysis,
    "highlight_detection": HighlightAnalysis,
    "clip_scoring": ClipEvaluation,
    "title_generation": GeneratedMetadata,
    "description_generation": GeneratedMetadata,
    "translation": TranslationResult,
    "caption_refinement": CaptionResult,
}

TEXT_TASKS: tuple[str, ...] = tuple(TASK_SCHEMAS)
SPEECH_TASKS: tuple[str, ...] = ("speech_to_text",)
ALL_TASKS: tuple[str, ...] = TEXT_TASKS + SPEECH_TASKS


def schema_for_task(task: str) -> type[BaseModel] | None:
    return TASK_SCHEMAS.get(task)


__all__ = [
    "ALL_TASKS",
    "SPEECH_TASKS",
    "TASK_SCHEMAS",
    "TEXT_TASKS",
    "CaptionResult",
    "CaptionSegment",
    "ClipEvaluation",
    "ContentAnalysis",
    "GeneratedMetadata",
    "GeneratedMetadataBatch",
    "GeneratedMetadataItem",
    "HighlightAnalysis",
    "HighlightSuggestion",
    "TranslationResult",
    "TranscriptResult",
    "TranscriptSegment",
    "TranscriptWord",
    "schema_for_task",
]
