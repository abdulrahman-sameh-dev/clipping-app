"""Deterministic window planning and global aggregation for multi-pass discovery."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from clipping_ai.highlights.candidates import dedupe_candidates
from clipping_ai.highlights.engine import _dedupe_highlights, _highlight_from_candidate
from clipping_ai.highlights.models import ContentType, HighlightResult, MediaSignals
from clipping_ai.highlights.semantic import consolidate_highlights, split_long_highlights
from clipping_ai.schemas import TranscriptResult

if TYPE_CHECKING:
    from clipping_ai.analysis.config import HighlightSettings

    class DiscoveryWindowArtifact:
        candidate_artifact: Any
        outcome: Any


@dataclass(frozen=True)
class DiscoveryWindow:
    window_id: str
    index: int
    start: float
    end: float


def plan_discovery_windows(
    duration: float,
    window_seconds: float,
    overlap_seconds: float,
) -> list[DiscoveryWindow]:
    if duration <= 0:
        return []
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    if overlap_seconds < 0 or overlap_seconds >= window_seconds:
        raise ValueError("overlap_seconds must be smaller than window_seconds")
    step = window_seconds - overlap_seconds
    windows: list[DiscoveryWindow] = []
    index = 0
    start = 0.0
    while start < duration:
        end = round(min(duration, start + window_seconds), 6)
        if end <= start:
            break
        windows.append(
            DiscoveryWindow(
                window_id=f"w{index:04d}_{int(round(start)):06d}_{int(round(end)):06d}",
                index=index,
                start=round(start, 6),
                end=end,
            )
        )
        if end >= duration:
            break
        start = round(start + step, 6)
        index += 1
    return windows


def window_transcript(
    transcript: TranscriptResult | None,
    window: DiscoveryWindow,
) -> TranscriptResult | None:
    if transcript is None:
        return None
    segments = [
        segment.model_copy(deep=True)
        for segment in transcript.segments
        if segment.end > window.start
        and segment.start < window.end
        and segment.text.strip()
    ]
    words = [
        word.model_copy(deep=True)
        for word in transcript.words
        if word.end > window.start and word.start < window.end
    ]
    return TranscriptResult(
        text=" ".join(segment.text.strip() for segment in segments if segment.text.strip()),
        language=transcript.language,
        duration=window.end,
        segments=segments,
        words=words,
        provider=transcript.provider,
        model=transcript.model,
    )


def window_signals(signals: MediaSignals, window: DiscoveryWindow) -> MediaSignals:
    return MediaSignals(
        duration=window.end,
        audio_windows=[
            item.model_copy(deep=True)
            for item in signals.audio_windows
            if item.end > window.start and item.start < window.end
        ],
        scene_changes=[
            item.model_copy(deep=True)
            for item in signals.scene_changes
            if window.start <= item.time < window.end
        ],
        motion_samples=[
            item.model_copy(deep=True)
            for item in signals.motion_samples
            if window.start <= item.time < window.end
        ],
        audio_analyzed=signals.audio_analyzed,
        video_analyzed=signals.video_analyzed,
        motion_analyzed=signals.motion_analyzed,
        notes=list(signals.notes),
    )


def _overlap_ratio(left: float, right: float, other_left: float, other_right: float) -> float:
    start = max(left, other_left)
    end = min(right, other_right)
    if end <= start:
        return 0.0
    shorter = min(right - left, other_right - other_left)
    if shorter <= 0:
        return 0.0
    return (end - start) / shorter


def select_diverse_highlights(
    highlights: Sequence[Any],
    limit: int,
    config: HighlightSettings,
) -> list[Any]:
    if limit <= 0:
        return []
    deduped: list[Any] = []
    for highlight in sorted(
        highlights,
        key=lambda item: (-float(item.confidence), float(item.start_time), str(item.title)),
    ):
        duplicate = any(
            _overlap_ratio(
                float(highlight.start_time),
                float(highlight.end_time),
                float(existing.start_time),
                float(existing.end_time),
            )
            >= config.dedupe_overlap_ratio
            for existing in deduped
        )
        if not duplicate:
            deduped.append(highlight)

    remaining = list(deduped)
    selected: list[Any] = []
    category_counts: dict[str, int] = {}
    while len(selected) < limit and remaining:
        def rank(index: int) -> tuple[float, float, str]:
            item = remaining[index]
            category = str(getattr(item, "category", ""))
            topic_penalty = 0.03 * category_counts.get(category, 0)
            distances = [
                abs(float(item.start_time) - float(existing.start_time))
                for existing in selected
            ]
            temporal_penalty = 0.08 if distances and min(distances) < 300.0 else 0.0
            return (
                float(item.confidence) - topic_penalty - temporal_penalty,
                -float(item.start_time),
                str(item.title),
            )

        index = max(range(len(remaining)), key=rank)
        chosen = remaining.pop(index)
        selected.append(chosen)
        category = str(getattr(chosen, "category", ""))
        category_counts[category] = category_counts.get(category, 0) + 1
    return sorted(selected, key=lambda item: (float(item.start_time), float(item.end_time)))


def aggregate_window_artifacts(
    artifacts: Sequence[Any],
    *,
    job_id: str,
    content_type: ContentType,
    duration: float,
    transcript: TranscriptResult | None,
    config: HighlightSettings,
) -> HighlightResult:
    notes: list[str] = []
    deterministic_pool: list[Any] = []
    highlights: list[Any] = []
    used_llm = False
    for artifact in artifacts:
        notes.extend(artifact.candidate_artifact.notes)
        candidates = artifact.candidate_artifact.candidate_objects()
        outcome = artifact.outcome
        if outcome.applied and outcome.highlights:
            highlights.extend(outcome.highlights)
            used_llm = True
        else:
            deterministic_pool.extend(candidates)
            if outcome.error:
                notes.append(outcome.error)

    highlights.extend(
        _highlight_from_candidate(candidate)
        for candidate in dedupe_candidates(
            deterministic_pool,
            overlap_ratio=config.dedupe_overlap_ratio,
        )
    )
    highlights = _dedupe_highlights(
        highlights,
        overlap_ratio=config.dedupe_overlap_ratio,
    )
    highlights = consolidate_highlights(
        highlights,
        transcript,
        config,
        content_type=content_type,
    )
    highlights = split_long_highlights(highlights, transcript, config)
    highlights = _dedupe_highlights(
        highlights,
        overlap_ratio=config.dedupe_overlap_ratio,
    )
    selected = select_diverse_highlights(highlights, config.max_highlights, config)
    resolved_type = content_type
    for artifact in artifacts:
        classification = artifact.candidate_artifact.classification
        if classification is not None:
            resolved_type = classification.content_type
            break
    notes.append(
        f"Aggregated {len(artifacts)} discovery window(s) and selected {len(selected)} final highlight(s)."
    )
    return HighlightResult(
        job_id=job_id,
        content_type=resolved_type,
        language=transcript.language if transcript else None,
        duration=duration,
        highlights=selected,
        method="llm" if used_llm else "deterministic",
        notes=notes,
    )
