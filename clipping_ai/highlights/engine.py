"""Highlight detection engine: deterministic candidates plus optional LLM refinement."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from clipping_ai.analysis.config import HighlightSettings
from clipping_ai.highlights.candidates import (
    Candidate,
    build_signal_candidates,
    build_transcript_candidates,
    dedupe_candidates,
)
from clipping_ai.highlights.classification import classify_content
from clipping_ai.highlights.llm import refine_with_llm_outcome
from clipping_ai.highlights.models import (
    ContentType,
    Highlight,
    HighlightResult,
    LlmOutcome,
    MediaClassification,
    MediaSignals,
)
from clipping_ai.highlights.observability import DiscoveryObserver, StageEvent
from clipping_ai.highlights.semantic import consolidate_highlights, split_long_highlights
from clipping_ai.highlights.signals import analyze_media_signals
from clipping_ai.schemas import TranscriptResult

if TYPE_CHECKING:
    from pathlib import Path

    from clipping_ai.manager import ProviderManager
    from clipping_ai.video.models import VideoMetadata

logger = logging.getLogger(__name__)


def _candidate_source_counts(candidates: list[Candidate]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.source] = counts.get(candidate.source, 0) + 1
    return counts


def _emit_stage(
    observer: DiscoveryObserver | None,
    stage: str,
    before: int,
    after: int,
    details: dict[str, object] | None = None,
) -> None:
    if observer is not None:
        observer.record(
            StageEvent(
                stage=stage,
                before=before,
                after=after,
                details=details or {},
            )
        )


@dataclass(frozen=True)
class HighlightPreparation:
    result: HighlightResult | None
    notes: list[str]
    signals: MediaSignals | None
    classification: MediaClassification | None
    candidates: list[Candidate]
    resolved_type: ContentType


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


def prepare_highlights(
    *,
    job_id: str,
    media_path: Path,
    metadata: VideoMetadata,
    transcript: TranscriptResult | None,
    config: HighlightSettings,
    content_type: ContentType = ContentType.AUTO,
    manager: ProviderManager | None = None,
    signals: MediaSignals | None = None,
    source_hint: str | None = None,
    observer: DiscoveryObserver | None = None,
) -> HighlightPreparation:
    notes: list[str] = []
    if not config.enabled:
        return HighlightPreparation(
            result=HighlightResult(
                job_id=job_id,
                content_type=content_type,
                language=transcript.language if transcript else None,
                duration=metadata.duration_seconds,
                notes=["Highlight detection is disabled in configuration."],
            ),
            notes=notes,
            signals=None,
            classification=None,
            candidates=[],
            resolved_type=content_type,
        )

    if signals is None:
        signals = analyze_media_signals(
            media_path,
            metadata,
            config=config,
            ffmpeg_binary=getattr(media_path, "_ffmpeg_binary", "ffmpeg"),
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
    transcript_candidates: list[Candidate] = []
    if transcript is not None and transcript.segments:
        transcript_candidates = build_transcript_candidates(
            transcript, config, content_type=resolved_type
        )
        candidates.extend(transcript_candidates)
    else:
        notes.append("No transcript was available; using media activity only.")
    _emit_stage(
        observer,
        "transcript_candidates",
        0,
        len(transcript_candidates),
        {"sources": _candidate_source_counts(transcript_candidates)},
    )
    before_signal_candidates = len(candidates)
    signal_candidates = build_signal_candidates(
        signals,
        transcript,
        config,
        content_type=resolved_type,
    )
    candidates.extend(signal_candidates)
    _emit_stage(
        observer,
        "signal_candidates",
        before_signal_candidates,
        len(candidates),
        {"sources": _candidate_source_counts(signal_candidates)},
    )
    before_dedupe = len(candidates)
    candidates = dedupe_candidates(
        candidates,
        overlap_ratio=config.dedupe_overlap_ratio,
    )
    _emit_stage(
        observer,
        "candidate_dedupe",
        before_dedupe,
        len(candidates),
        {"sources": _candidate_source_counts(candidates)},
    )
    before_filter = len(candidates)
    candidates = [
        candidate
        for candidate in candidates
        if candidate.score >= config.min_confidence
        and config.min_clip_seconds <= candidate.end - candidate.start <= config.max_clip_seconds
    ]
    _emit_stage(
        observer,
        "confidence_duration_filter",
        before_filter,
        len(candidates),
        {"sources": _candidate_source_counts(candidates)},
    )
    if config.use_llm and manager is not None and transcript is not None:
        before_cap = len(candidates)
        candidates = candidates[: config.llm_max_candidates]
        _emit_stage(
            observer,
            "llm_candidate_cap",
            before_cap,
            len(candidates),
            {"limit": config.llm_max_candidates},
        )

    if not candidates:
        notes.append("No candidate windows met the configured thresholds.")
        return HighlightPreparation(
            result=HighlightResult(
                job_id=job_id,
                content_type=resolved_type,
                language=transcript.language if transcript else None,
                duration=metadata.duration_seconds,
                signals=signals.summary(),
                classification=classification,
                notes=notes,
            ),
            notes=notes,
            signals=signals,
            classification=classification,
            candidates=candidates,
            resolved_type=resolved_type,
        )

    return HighlightPreparation(
        result=None,
        notes=notes,
        signals=signals,
        classification=classification,
        candidates=candidates,
        resolved_type=resolved_type,
    )


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
    classification: MediaClassification | None = None,
    candidates: list[Candidate] | None = None,
    llm_outcome: LlmOutcome | None = None,
    source_hint: str | None = None,
    preparation: HighlightPreparation | None = None,
    observer: DiscoveryObserver | None = None,
) -> HighlightResult:
    preparation = preparation or prepare_highlights(
        job_id=job_id,
        media_path=media_path,
        metadata=metadata,
        transcript=transcript,
        config=config,
        content_type=content_type,
        manager=manager,
        signals=signals,
        source_hint=source_hint,
        observer=observer,
    )
    if preparation.result is not None:
        return preparation.result

    notes = list(preparation.notes)
    resolved_type = preparation.resolved_type
    selected_candidates = preparation.candidates if candidates is None else candidates
    media_signals = preparation.signals
    selected_classification = preparation.classification or classification
    highlights: list[Highlight] = []
    method = "deterministic"

    if llm_outcome is not None or (config.use_llm and manager is not None and transcript is not None):
        if llm_outcome is not None:
            outcome = llm_outcome
        elif transcript is not None and manager is not None:
            outcome = refine_with_llm_outcome(
                manager,
                transcript=transcript,
                candidates=selected_candidates,
                content_type=resolved_type,
                signals=media_signals,
                config=config,
                provider=provider,
                model=model,
            )
        else:
            outcome = LlmOutcome()
        if outcome.applied and outcome.highlights:
            highlights = outcome.highlights
            method = "llm"
            notes.append("LLM refinement selected and rewrote the final highlight set.")
        elif outcome.applied and outcome.error:
            notes.append(f"{outcome.error} Deterministic candidates were used instead.")
        elif outcome.applied:
            notes.append("LLM refinement returned no highlights; deterministic candidates were used.")
        else:
            notes.append("Deterministic highlight selection was used.")
    else:
        notes.append("Deterministic highlight selection was used.")

    if not highlights:
        highlights = [_highlight_from_candidate(candidate) for candidate in selected_candidates]
    before_consolidation = len(highlights)
    highlights = consolidate_highlights(
        highlights,
        transcript,
        config,
        content_type=resolved_type,
    )
    _emit_stage(observer, "semantic_consolidation", before_consolidation, len(highlights), {})
    if len(highlights) < before_consolidation:
        notes.append("Related highlight fragments were consolidated into complete moments.")
    before_split = len(highlights)
    highlights = split_long_highlights(highlights, transcript, config)
    _emit_stage(observer, "long_highlight_split", before_split, len(highlights), {})
    if len(highlights) > before_split:
        notes.append("Overlong moments were split at transcript boundaries to target 30-45 seconds.")
    before_highlight_dedupe = len(highlights)
    highlights = _dedupe_highlights(
        highlights,
        overlap_ratio=config.dedupe_overlap_ratio,
    )
    _emit_stage(
        observer,
        "highlight_dedupe",
        before_highlight_dedupe,
        len(highlights),
        {},
    )
    before_final_cap = len(highlights)
    highlights = sorted(highlights, key=lambda item: item.confidence, reverse=True)[
        : config.max_highlights
    ]
    _emit_stage(
        observer,
        "final_highlight_cap",
        before_final_cap,
        len(highlights),
        {"limit": config.max_highlights},
    )
    highlights = sorted(highlights, key=lambda item: item.start_time)

    return HighlightResult(
        job_id=job_id,
        content_type=resolved_type,
        language=transcript.language if transcript else None,
        duration=metadata.duration_seconds,
        highlights=highlights,
        signals=media_signals.summary() if media_signals else {},
        classification=selected_classification,
        method=method,
        notes=notes,
    )
