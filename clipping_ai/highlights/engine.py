"""Highlight detection engine: deterministic candidates plus optional LLM refinement."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from clipping_ai.analysis.config import HighlightSettings
from clipping_ai.highlights.candidates import (
    Candidate,
    build_signal_candidates,
    build_transcript_candidates,
    dedupe_candidates,
)
from clipping_ai.highlights.classification import classify_content
from clipping_ai.highlights.llm import refine_with_llm
from clipping_ai.highlights.models import ContentType, Highlight, HighlightResult, MediaSignals
from clipping_ai.highlights.semantic import consolidate_highlights
from clipping_ai.highlights.signals import analyze_media_signals
from clipping_ai.schemas import TranscriptResult

if TYPE_CHECKING:
    from pathlib import Path

    from clipping_ai.manager import ProviderManager
    from clipping_ai.video.models import VideoMetadata

logger = logging.getLogger(__name__)


def _highlight_from_candidate(candidate: Candidate, *, source: str | None = None) -> Highlight:
    text = " ".join(segment.text.strip() for segment in candidate.transcript_segments)
    if text:
        title = text[:90].rstrip()
        if len(text) > 90:
            title = f"{title.rsplit(' ', 1)[0]}..."
        description = text[:400]
    else:
        title = candidate.category.replace("_", " ").title()
        description = candidate.reason
    return Highlight(
        start_time=candidate.start,
        end_time=candidate.end,
        title=title,
        description=description,
        selection_reason=candidate.reason,
        confidence=round(candidate.score, 4),
        category=candidate.category,
        transcript_segments=candidate.transcript_segments,
        signal_score=candidate.signal_score,
        detection_signals=dict(candidate.signals),
        source=source or candidate.source,
    )


def _dedupe_highlights(
    highlights: list[Highlight],
    *,
    overlap_ratio: float = 0.55,
) -> list[Highlight]:
    kept: list[Highlight] = []
    for highlight in sorted(highlights, key=lambda item: item.confidence, reverse=True):
        duplicate = False
        for existing in kept:
            left = max(highlight.start_time, existing.start_time)
            right = min(highlight.end_time, existing.end_time)
            if right <= left:
                continue
            shorter = min(
                highlight.end_time - highlight.start_time,
                existing.end_time - existing.start_time,
            )
            if shorter > 0 and (right - left) / shorter >= overlap_ratio:
                duplicate = True
                break
        if not duplicate:
            kept.append(highlight)
    return sorted(kept, key=lambda item: item.start_time)


def detect_highlights(
    *,
    job_id: str,
    media_path: Path,
    metadata: VideoMetadata,
    transcript: TranscriptResult | None,
    config: HighlightSettings,
    content_type: ContentType = ContentType.AUTO,
    manager: ProviderManager | None = None,
    provider: str | None = None,
    model: str | None = None,
    ffmpeg_binary: str = "ffmpeg",
    signals: MediaSignals | None = None,
    source_hint: str | None = None,
) -> HighlightResult:
    notes: list[str] = []
    if not config.enabled:
        return HighlightResult(
            job_id=job_id,
            content_type=content_type,
            language=transcript.language if transcript else None,
            duration=metadata.duration_seconds,
            notes=["Highlight detection is disabled in configuration."],
        )

    if signals is None:
        signals = analyze_media_signals(
            media_path,
            metadata,
            config=config,
            ffmpeg_binary=ffmpeg_binary,
        )
    notes.extend(signals.notes)

    classification = classify_content(
        transcript,
        signals,
        content_type,
        source_hint=source_hint,
        config=config,
    )
    resolved_type = classification.content_type
    notes.append(
        f"Content classified as {resolved_type.value} "
        f"(confidence {classification.confidence:.2f})."
    )
    candidates: list[Candidate] = []
    if transcript is not None and transcript.segments:
        candidates.extend(
            build_transcript_candidates(transcript, config, content_type=resolved_type)
        )
    else:
        notes.append("No transcript was available; using media activity only.")
    candidates.extend(
        build_signal_candidates(
            signals,
            transcript,
            config,
            content_type=resolved_type,
        )
    )
    candidates = dedupe_candidates(
        candidates,
        overlap_ratio=config.dedupe_overlap_ratio,
    )
    candidates = [
        candidate
        for candidate in candidates
        if candidate.score >= config.min_confidence
        and config.min_clip_seconds <= candidate.end - candidate.start <= config.max_clip_seconds
    ]
    if config.use_llm and manager is not None and transcript is not None:
        candidates = candidates[: config.llm_max_candidates]

    if not candidates:
        notes.append("No candidate windows met the configured thresholds.")
        return HighlightResult(
            job_id=job_id,
            content_type=resolved_type,
            language=transcript.language if transcript else None,
            duration=metadata.duration_seconds,
            signals=signals.summary(),
            classification=classification,
            notes=notes,
        )

    highlights: list[Highlight] = []
    method = "deterministic"
    if config.use_llm and manager is not None and transcript is not None:
        refined, error = refine_with_llm(
            manager,
            transcript=transcript,
            candidates=candidates,
            content_type=resolved_type,
            signals=signals,
            config=config,
            provider=provider,
            model=model,
        )
        if refined:
            highlights = refined
            method = "llm"
            notes.append("LLM refinement selected and rewrote the final highlight set.")
        elif error:
            notes.append(f"{error} Deterministic candidates were used instead.")
        else:
            notes.append("LLM refinement returned no highlights; deterministic candidates were used.")
    else:
        notes.append("Deterministic highlight selection was used.")

    if not highlights:
        highlights = [_highlight_from_candidate(candidate) for candidate in candidates]
    before_consolidation = len(highlights)
    highlights = consolidate_highlights(
        highlights,
        transcript,
        config,
        content_type=resolved_type,
    )
    if len(highlights) < before_consolidation:
        notes.append("Related highlight fragments were consolidated into complete moments.")
    highlights = _dedupe_highlights(
        highlights,
        overlap_ratio=config.dedupe_overlap_ratio,
    )
    highlights = sorted(highlights, key=lambda item: item.confidence, reverse=True)[
        : config.max_highlights
    ]
    highlights = sorted(highlights, key=lambda item: item.start_time)

    return HighlightResult(
        job_id=job_id,
        content_type=resolved_type,
        language=transcript.language if transcript else None,
        duration=metadata.duration_seconds,
        highlights=highlights,
        signals=signals.summary(),
        classification=classification,
        method=method,
        notes=notes,
    )
