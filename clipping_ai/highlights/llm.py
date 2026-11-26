"""Optional LLM refinement for deterministic highlight candidates."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from clipping_ai.analysis.config import HighlightSettings
from clipping_ai.analysis.transcription import stage_retryable
from clipping_ai.errors import AIError
from clipping_ai.highlights.candidates import (
    Candidate,
    _align_to_transcript,
    candidate_summary,
)
from clipping_ai.highlights.models import ContentType, Highlight, LlmOutcome, MediaSignals
from clipping_ai.schemas import TranscriptResult

if TYPE_CHECKING:
    from clipping_ai.manager import ProviderManager


def _segments_for_range(transcript: TranscriptResult, start: float, end: float):
    return [
        segment
        for segment in transcript.segments
        if segment.end > start and segment.start < end and segment.text.strip()
    ]


def _clamp_range(
    start: float,
    end: float,
    *,
    duration: float,
    config: HighlightSettings,
) -> tuple[float, float]:
    start = min(max(0.0, start), duration)
    end = min(max(start, end), duration)
    if end - start < config.min_clip_seconds:
        end = min(duration, start + config.min_clip_seconds)
        start = max(0.0, end - config.min_clip_seconds)
    if end - start > config.max_clip_seconds:
        end = start + config.max_clip_seconds
    return round(start, 3), round(end, 3)


def llm_instructions() -> str:
    return (
        "Use only the supplied transcript and candidate windows. Choose complete, meaningful "
        "moments and preserve original start/end timestamps. Prefer one full explanation over "
        "several short fragments. If adjacent candidate windows belong to the same example or "
        "argument, return one combined range that covers the full idea. Do not merge unrelated "
        "ideas. Candidate windows are hints, not limits. A self-contained short statement may "
        "stay short, while a longer explanation may span a minute or more. For gaming or live "
        "content, use audio/video activity windows even when speech is sparse. Do not invent "
        "events or words. Return at most the requested number of highlights."
    )


def build_llm_payload(
    *,
    transcript: TranscriptResult,
    candidates: list[Candidate],
    content_type: ContentType,
    signals: MediaSignals | None,
    config: HighlightSettings,
) -> tuple[str, dict[str, Any]]:
    text = transcript.text.strip()
    limited_text = text[: config.llm_max_transcript_chars]
    payload: dict[str, Any] = {
        "content_type": content_type.value,
        "language": transcript.language,
        "duration": transcript.duration,
        "transcript_segments": [
            segment.model_dump(mode="json") for segment in transcript.segments[:2000]
        ],
        "candidate_windows": [
            candidate_summary(candidate) for candidate in candidates[: config.llm_max_candidates]
        ],
        "signal_summary": signals.summary() if signals else None,
        "max_highlights": config.max_highlights,
        "min_clip_seconds": config.min_clip_seconds,
        "max_clip_seconds": config.max_clip_seconds,
    }
    return limited_text, payload


def parse_llm_response(
    response_data: dict[str, Any] | None,
    *,
    transcript: TranscriptResult,
    candidates: list[Candidate],
    signals: MediaSignals | None,
    config: HighlightSettings,
) -> tuple[list[Highlight], str | None]:
    raw_highlights = response_data.get("highlights") if isinstance(response_data, dict) else None
    if not isinstance(raw_highlights, list):
        return [], "LLM refinement returned no usable highlights."

    signal_duration = signals.duration if signals is not None else 0.0
    duration = max(
        transcript.duration or 0.0,
        transcript.segments[-1].end if transcript.segments else 0.0,
        signal_duration,
    )
    highlights: list[Highlight] = []
    for raw in raw_highlights:
        if not isinstance(raw, dict):
            continue
        try:
            start = float(raw.get("start_time", 0.0))
            end = float(raw.get("end_time", 0.0))
            score = float(raw.get("score", 0.0))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        start, end = _clamp_range(start, end, duration=duration, config=config)
        if end <= start:
            continue
        if transcript.segments:
            start, end, aligned = _align_to_transcript(
                start,
                end,
                transcript.segments,
                config,
                duration=duration,
            )
        else:
            aligned = _segments_for_range(transcript, start, end)
        if end - start < config.min_clip_seconds or end - start > config.max_clip_seconds:
            continue
        confidence = min(1.0, max(0.0, score / 100.0))
        title = str(raw.get("suggested_title") or raw.get("title") or "").strip()
        description = str(raw.get("summary") or raw.get("description") or "").strip()
        reason = str(raw.get("reasoning") or raw.get("selection_reason") or "").strip()
        category = str(raw.get("category") or "highlight").strip()
        raw_signals = raw.get("detection_signals") or raw.get("signals") or {}
        detection_signals: dict[str, float] = {}
        if isinstance(raw_signals, dict):
            for key, value in raw_signals.items():
                try:
                    detection_signals[str(key)] = min(1.0, max(0.0, float(value)))
                except (TypeError, ValueError):
                    continue
        if not title or not description or not reason:
            continue
        highlights.append(
            Highlight(
                start_time=start,
                end_time=end,
                title=title,
                description=description,
                selection_reason=reason,
                confidence=round(confidence, 4),
                category=category,
                transcript_segments=aligned,
                detection_signals=detection_signals,
                source="llm",
            )
        )
    return highlights, None


def refine_with_llm_outcome(
    manager: ProviderManager | None,
    *,
    transcript: TranscriptResult,
    candidates: list[Candidate],
    content_type: ContentType,
    signals: MediaSignals | None,
    config: HighlightSettings,
    provider: str | None = None,
    model: str | None = None,
) -> LlmOutcome:
    if manager is None or not config.use_llm or not candidates:
        return LlmOutcome()
    text = transcript.text.strip()
    if not text:
        return LlmOutcome(error="No transcript text was available for LLM refinement.")

    limited_text, payload = build_llm_payload(
        transcript=transcript,
        candidates=candidates,
        content_type=content_type,
        signals=signals,
        config=config,
    )
    try:
        response = manager.analyze(
            "highlight_detection",
            text=limited_text,
            instructions=llm_instructions(),
            provider=provider,
            model=model,
            payload=payload,
        )
    except AIError as exc:
        return LlmOutcome(
            error=f"LLM refinement failed: {exc.message}",
            provider=exc.provider or provider,
            model=exc.model or model,
            applied=True,
            failed=True,
            retryable=stage_retryable(exc),
            retry_after=exc.retry_after,
        )

    response_data = response.get("data") if isinstance(response, dict) else None
    response_data = response_data if isinstance(response_data, dict) else None
    highlights, error = parse_llm_response(
        response_data,
        transcript=transcript,
        candidates=candidates,
        signals=signals,
        config=config,
    )
    response_provider = response.get("provider") if isinstance(response, dict) else None
    response_model = response.get("model") if isinstance(response, dict) else None
    invalid_response = error is not None
    return LlmOutcome(
        highlights=highlights,
        error=error,
        response_data=response_data,
        provider=str(response_provider) if response_provider else provider,
        model=str(response_model) if response_model else model,
        applied=True,
        failed=invalid_response,
        retryable=invalid_response,
    )


def refine_with_llm(
    manager: ProviderManager | None,
    *,
    transcript: TranscriptResult,
    candidates: list[Candidate],
    content_type: ContentType,
    signals: MediaSignals | None,
    config: HighlightSettings,
    provider: str | None = None,
    model: str | None = None,
) -> tuple[list[Highlight], str | None]:
    outcome = refine_with_llm_outcome(
        manager,
        transcript=transcript,
        candidates=candidates,
        content_type=content_type,
        signals=signals,
        config=config,
        provider=provider,
        model=model,
    )
    return outcome.highlights, outcome.error
