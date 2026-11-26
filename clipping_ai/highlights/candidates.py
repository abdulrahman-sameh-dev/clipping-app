"""Deterministic candidate generation from transcripts and media activity."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from clipping_ai.analysis.config import HighlightSettings
from clipping_ai.highlights.classification import infer_content_type
from clipping_ai.highlights.models import ContentType, MediaSignals
from clipping_ai.schemas import TranscriptResult, TranscriptSegment

__all__ = [
    "Candidate",
    "build_signal_candidates",
    "build_transcript_candidates",
    "candidate_summary",
    "dedupe_candidates",
    "infer_content_type",
]

_SPEECH_KEYWORDS = {
    "because",
    "but",
    "how",
    "why",
    "what",
    "when",
    "story",
    "important",
    "mistake",
    "secret",
    "never",
    "always",
    "actually",
    "really",
    "problem",
    "solution",
    "learn",
    "explain",
    "question",
    "answer",
    "believe",
    "changed",
    "best",
    "worst",
    "first",
    "last",
    "finally",
    "shocking",
    "crazy",
    "insane",
    "unbelievable",
    "win",
    "won",
    "lose",
    "lost",
    "fail",
    "failed",
    "clutch",
    "reaction",
    "wow",
    "game",
    "gameplay",
    "boss",
    "kill",
    "died",
    "rescue",
    "comeback",
    "level",
    "match",
    "round",
}
_ARABIC_KEYWORDS = {
    "لماذا",
    "كيف",
    "ماذا",
    "متى",
    "قصة",
    "مهم",
    "خطأ",
    "سر",
    "أبدا",
    "دائما",
    "مشكلة",
    "حل",
    "تعلم",
    "سؤال",
    "جواب",
    "أفضل",
    "أسوأ",
    "فوز",
    "خسارة",
    "لعبة",
    "مباراة",
    "جنون",
    "لا يصدق",
}
_COMPLETE_ENDINGS = (".", "!", "?", "؟", "。", "！", "？", "…")
_INCOMPLETE_ENDINGS = (",", "،", "؛", ";", ":", "—", "-")


@dataclass
class Candidate:
    start: float
    end: float
    score: float
    category: str
    reason: str
    transcript_segments: list[TranscriptSegment] = field(default_factory=list)
    signal_score: float | None = None
    signals: dict[str, float] = field(default_factory=dict)
    source: str = "deterministic"

    def overlaps(self, other: Candidate) -> float:
        left = max(self.start, other.start)
        right = min(self.end, other.end)
        if right <= left:
            return 0.0
        return (right - left) / min(self.end - self.start, other.end - other.start)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, value))


def _text_score(text: str) -> float:
    lowered = text.lower()
    words = set(lowered.replace("،", " ").replace("؟", " ").split())
    hits = sum(1 for keyword in _SPEECH_KEYWORDS if keyword in words)
    hits += sum(1 for keyword in _ARABIC_KEYWORDS if keyword in text)
    score = min(0.35, hits * 0.07)
    if "?" in text or "؟" in text:
        score += 0.16
    if "!" in text or "！" in text:
        score += 0.1
    if any(char.isdigit() for char in text):
        score += 0.04
    stripped = text.rstrip()
    if stripped.endswith(_COMPLETE_ENDINGS):
        score += 0.12
    elif stripped.endswith(_INCOMPLETE_ENDINGS):
        score -= 0.12
    return _clamp(score)


def _speech_density(segments: list[TranscriptSegment]) -> float:
    if not segments:
        return 0.0
    duration = max(0.1, segments[-1].end - segments[0].start)
    characters = sum(len(segment.text) for segment in segments)
    return _clamp(characters / duration / 18.0)


def _segments_between(
    segments: list[TranscriptSegment], start: float, end: float
) -> list[TranscriptSegment]:
    return [
        segment
        for segment in segments
        if segment.end > start and segment.start < end and segment.text.strip()
    ]


def _duration_score(duration: float, config: HighlightSettings) -> float:
    if (
        config.target_clip_seconds_min is not None
        and config.target_clip_seconds_max is not None
    ):
        target = (
            config.target_clip_seconds_min + config.target_clip_seconds_max
        ) / 2
        spread = max(
            1.0,
            (config.target_clip_seconds_max - config.target_clip_seconds_min) / 2
            + config.target_duration_tolerance_seconds,
        )
    else:
        target = (config.min_clip_seconds + config.max_clip_seconds) / 2
        spread = max(1.0, (config.max_clip_seconds - config.min_clip_seconds) / 2)
    return _clamp(1.0 - abs(duration - target) / spread)


def _align_to_transcript(
    start: float,
    end: float,
    segments: list[TranscriptSegment],
    config: HighlightSettings,
    *,
    duration: float,
) -> tuple[float, float, list[TranscriptSegment]]:
    if not config.align_to_transcript or not segments:
        return start, end, _segments_between(segments, start, end)
    overlapping = _segments_between(segments, start, end)
    if not overlapping:
        nearest = min(segments, key=lambda segment: abs(segment.start - start))
        if abs(nearest.start - start) <= 3.0:
            overlapping = [nearest]
        else:
            return start, end, []
    start = max(0.0, min(segment.start for segment in overlapping))
    end = min(duration, max(segment.end for segment in overlapping))
    if end - start > config.max_clip_seconds:
        end = min(duration, start + config.max_clip_seconds)
        overlapping = _segments_between(segments, start, end)
        if overlapping:
            end = min(end, max(segment.end for segment in overlapping))
    if end - start < config.min_clip_seconds:
        for segment in segments:
            if segment.start <= end and segment.end > end:
                end = min(duration, segment.end)
                break
    if end - start < config.min_clip_seconds:
        end = min(duration, start + config.min_clip_seconds)
    end = min(end, start + config.max_clip_seconds)
    return start, end, _segments_between(segments, start, end)


def _semantic_window(
    segments: list[TranscriptSegment],
    start_index: int,
    config: HighlightSettings,
) -> tuple[list[TranscriptSegment], int]:
    first = segments[start_index]
    window: list[TranscriptSegment] = []
    last_index = start_index
    for index in range(start_index, len(segments)):
        segment = segments[index]
        if window:
            previous = window[-1]
            if (
                segment.start - previous.end > config.semantic_gap_seconds
                and previous.end - first.start >= config.min_clip_seconds
            ):
                break
        if window and segment.end - first.start > config.max_clip_seconds:
            break
        window.append(segment)
        last_index = index
        if segment.end - first.start >= config.max_clip_seconds:
            break
    return window, last_index


def _semantic_starts(
    segments: list[TranscriptSegment],
    config: HighlightSettings,
) -> list[int]:
    starts = [0]
    for index in range(1, len(segments)):
        if segments[index].start - segments[index - 1].end > config.semantic_gap_seconds:
            starts.append(index)
    return starts


def _semantic_transcript_candidates(
    transcript: TranscriptResult,
    segments: list[TranscriptSegment],
    config: HighlightSettings,
    *,
    content_type: ContentType,
) -> list[Candidate]:
    if not config.semantic_consolidation or content_type in {ContentType.GAMING, ContentType.LIVE}:
        return []
    duration = max(transcript.duration or 0.0, segments[-1].end)
    starts = _semantic_starts(segments, config)
    seen_starts = set(starts)
    candidates: list[Candidate] = []
    position = 0
    while position < len(starts):
        start_index = starts[position]
        position += 1
        window, last_index = _semantic_window(segments, start_index, config)
        if not window:
            continue
        start, end, aligned = _align_to_transcript(
            window[0].start,
            window[-1].end,
            segments,
            config,
            duration=duration,
        )
        if end - start < config.min_clip_seconds or end - start > config.max_clip_seconds:
            continue
        text = " ".join(segment.text.strip() for segment in aligned)
        score = (
            0.35
            + _text_score(text)
            + _speech_density(aligned) * 0.3
            + _duration_score(end - start, config) * 0.2
        )
        if text.rstrip().endswith(_COMPLETE_ENDINGS):
            score += 0.04
        category = "discussion"
        if "?" in text or "؟" in text:
            category = "question_answer"
        elif any(word in text.lower() for word in ("because", "explain", "means", "تعلم", "يعني")):
            category = "explanation"
        elif any(word in text.lower() for word in ("story", "happened", "قصة", "حدث")):
            category = "storytelling"
        candidates.append(
            Candidate(
                start=round(start, 3),
                end=round(end, 3),
                score=_clamp(score),
                category=category,
                reason=(
                    "Semantic transcript window that follows the full idea instead of "
                    "a fixed short duration."
                ),
                transcript_segments=aligned,
                signals={"transcript": round(_clamp(score), 4)},
                source="semantic",
            )
        )
        if (
            window[-1].end - window[0].start >= config.max_clip_seconds - 0.01
            and last_index + 1 < len(segments)
        ):
            next_index = last_index + 1
            if next_index not in seen_starts:
                seen_starts.add(next_index)
                starts.append(next_index)
    return candidates


def build_transcript_candidates(
    transcript: TranscriptResult,
    config: HighlightSettings,
    *,
    content_type: ContentType,
) -> list[Candidate]:
    segments = [
        segment
        for segment in sorted(transcript.segments, key=lambda item: item.start)
        if segment.text.strip() and segment.end > segment.start
    ]
    if not segments:
        return []
    duration = max(transcript.duration or 0.0, segments[-1].end)
    candidates: list[Candidate] = []
    next_start = 0.0
    for index, first in enumerate(segments):
        if first.start + 0.01 < next_start:
            continue
        window: list[TranscriptSegment] = []
        end = first.start
        window_limit = min(config.max_clip_seconds, config.candidate_window_seconds)
        for segment in segments[index:]:
            if segment.end - first.start > window_limit and window:
                break
            window.append(segment)
            end = segment.end
            if end - first.start >= config.min_clip_seconds:
                break
        if not window:
            continue
        start, end, aligned = _align_to_transcript(
            window[0].start,
            window[-1].end,
            segments,
            config,
            duration=duration,
        )
        if end - start < config.min_clip_seconds or end - start > config.max_clip_seconds:
            continue
        text = " ".join(segment.text.strip() for segment in aligned)
        score = (
            0.35
            + _text_score(text)
            + _speech_density(aligned) * 0.3
            + _duration_score(end - start, config) * 0.2
        )
        if content_type in {ContentType.GAMING, ContentType.LIVE}:
            score -= 0.1
        category = "discussion"
        if "?" in text or "؟" in text:
            category = "question_answer"
        elif any(word in text.lower() for word in ("because", "explain", "means", "تعلم", "يعني")):
            category = "explanation"
        elif any(word in text.lower() for word in ("story", "happened", "قصة", "حدث")):
            category = "storytelling"
        candidates.append(
            Candidate(
                start=round(start, 3),
                end=round(end, 3),
                score=_clamp(score),
                category=category,
                reason=(
                    "Transcript window with a self-contained statement, useful keywords, "
                    "and complete segment boundaries."
                ),
                transcript_segments=aligned,
                signals={"transcript": round(_clamp(score), 4)},
                source="transcript",
            )
        )
        next_start = first.start + config.candidate_stride_seconds
    candidates.extend(
        _semantic_transcript_candidates(
            transcript,
            segments,
            config,
            content_type=content_type,
        )
    )
    return candidates


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((percentile / 100.0) * (len(ordered) - 1)))))
    return ordered[index]


def _transcript_bonus(
    start: float,
    end: float,
    segments: list[TranscriptSegment],
    weight: float,
) -> tuple[float, list[TranscriptSegment]]:
    matching = _segments_between(segments, start, end)
    if not matching:
        return 0.0, []
    text = " ".join(segment.text for segment in matching)
    return _text_score(text) * weight, matching


def build_signal_candidates(
    signals: MediaSignals,
    transcript: TranscriptResult | None,
    config: HighlightSettings,
    *,
    content_type: ContentType,
) -> list[Candidate]:
    duration = max(signals.duration, 0.0)
    if duration <= 0:
        return []
    windows = signals.audio_windows
    energies = [window.energy for window in windows]
    active_energies = [window.energy for window in windows if not window.silence]
    baseline = sum(energies) / len(energies) if energies else 0.0
    threshold = (
        max(
            config.audio_event_min_energy,
            _percentile(active_energies, config.spike_percentile),
        )
        if active_energies
        else 0.0
    )

    events: list[tuple[float, float, str]] = []
    for index, window in enumerate(windows):
        if window.silence:
            continue
        previous = windows[index - 1] if index > 0 else None
        previous_energy = (
            previous.energy if previous is not None and not previous.silence else 0.0
        )
        if window.energy >= threshold:
            events.append((window.start, window.energy, "audio"))
        delta = window.energy - previous_energy
        if window.energy >= threshold and delta >= config.audio_peak_delta:
            events.append((window.start, _clamp(delta), "peak"))
        if (
            previous is not None
            and previous.silence
            and window.energy >= max(config.audio_event_min_energy, baseline + 0.1)
        ):
            events.append((window.start, window.energy, "silence"))
    for scene in signals.scene_changes:
        events.append((scene.time, scene.score, "scene"))
    for sample in signals.motion_samples:
        if sample.score >= config.motion_event_threshold:
            events.append((sample.time, sample.score, "motion"))
    if not events:
        return []
    events.sort(key=lambda item: item[0])

    groups: list[list[tuple[float, float, str]]] = []
    for event in events:
        if not groups or event[0] - groups[-1][-1][0] > config.event_merge_seconds:
            groups.append([event])
        else:
            groups[-1].append(event)

    segments = transcript.segments if transcript else []
    candidates: list[Candidate] = []
    for group in groups:
        first_time = min(event[0] for event in group)
        last_time = max(event[0] for event in group)
        start = max(0.0, first_time - config.pre_roll_seconds)
        end = min(duration, last_time + config.post_roll_seconds)
        if end - start < config.min_clip_seconds:
            missing = config.min_clip_seconds - (end - start)
            start = max(0.0, start - missing / 2)
            end = min(duration, start + config.min_clip_seconds)
        if end - start > config.max_clip_seconds:
            end = start + config.max_clip_seconds
        start, end, aligned = _align_to_transcript(
            start,
            end,
            segments,
            config,
            duration=duration,
        )
        if end - start < config.min_clip_seconds:
            continue

        peak_energy = max(
            (event[1] for event in group if event[2] in {"audio", "silence"}),
            default=0.0,
        )
        peak_activity = max((event[1] for event in group if event[2] == "peak"), default=0.0)
        scene_activity = max((event[1] for event in group if event[2] == "scene"), default=0.0)
        motion_activity = max((event[1] for event in group if event[2] == "motion"), default=0.0)
        silence_transition = 1.0 if any(event[2] == "silence" for event in group) else 0.0
        audio_activity = (
            _clamp((peak_energy - baseline) / max(0.05, 1.0 - baseline))
            if peak_energy > 0
            else 0.0
        )
        transcript_bonus, aligned = _transcript_bonus(
            start,
            end,
            segments,
            config.transcript_signal_weight,
        )
        score = (
            config.signal_base_score
            + audio_activity * config.audio_weight
            + peak_activity * config.peak_weight
            + scene_activity * config.scene_weight
            + motion_activity * config.motion_weight
            + silence_transition * config.silence_transition_weight
            + transcript_bonus
        )
        if content_type in {ContentType.PODCAST, ContentType.EDUCATIONAL, ContentType.GENERAL}:
            score -= config.content_type_signal_penalty
        activity = max(audio_activity, peak_activity, scene_activity, motion_activity)
        if aligned and any("!" in segment.text for segment in aligned):
            category = "reaction"
        elif content_type in {ContentType.GAMING, ContentType.LIVE} and max(
            scene_activity, motion_activity
        ) >= 0.2:
            category = "gameplay_event"
        elif peak_activity >= 0.25:
            category = "audio_spike"
        elif silence_transition:
            category = "sound_event"
        elif scene_activity >= 0.5:
            category = "scene_change"
        elif motion_activity >= 0.5:
            category = "motion_event"
        else:
            category = "activity_spike"

        reasons: list[str] = []
        if audio_activity >= 0.2:
            reasons.append("sustained audio activity")
        if peak_activity >= 0.15:
            reasons.append("sudden audio peak")
        if silence_transition:
            reasons.append("silence-to-sound transition")
        if scene_activity >= 0.2:
            reasons.append("video scene change")
        if motion_activity >= 0.2:
            reasons.append("visual motion")
        if aligned:
            reasons.append("matching transcript")
        reason = (
            " + ".join(reasons).capitalize() + " with surrounding context."
            if reasons
            else "Combined media activity with surrounding context."
        )
        candidates.append(
            Candidate(
                start=round(start, 3),
                end=round(end, 3),
                score=_clamp(score),
                category=category,
                reason=reason,
                transcript_segments=aligned,
                signal_score=round(_clamp(activity), 4),
                signals={
                    "audio_activity": round(_clamp(audio_activity), 4),
                    "audio_peak": round(_clamp(peak_activity), 4),
                    "silence_transition": round(silence_transition, 4),
                    "scene_change": round(_clamp(scene_activity), 4),
                    "motion": round(_clamp(motion_activity), 4),
                    "transcript": round(_clamp(transcript_bonus), 4),
                },
                source="signals",
            )
        )
    return candidates


def dedupe_candidates(
    candidates: list[Candidate],
    *,
    overlap_ratio: float = 0.55,
) -> list[Candidate]:
    kept: list[Candidate] = []
    for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
        if any(candidate.overlaps(existing) >= overlap_ratio for existing in kept):
            continue
        kept.append(candidate)
    return kept


def candidate_summary(candidate: Candidate) -> dict[str, Any]:
    return {
        "start_time": candidate.start,
        "end_time": candidate.end,
        "duration": round(candidate.end - candidate.start, 3),
        "score": round(candidate.score, 4),
        "category": candidate.category,
        "source": candidate.source,
        "reason": candidate.reason,
        "signals": candidate.signals,
        "transcript": " ".join(segment.text.strip() for segment in candidate.transcript_segments),
    }
