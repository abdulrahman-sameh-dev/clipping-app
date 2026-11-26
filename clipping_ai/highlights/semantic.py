"""Semantic boundary expansion and related-fragment consolidation."""

from __future__ import annotations

import re

from clipping_ai.analysis.config import HighlightSettings
from clipping_ai.highlights.models import ContentType, Highlight
from clipping_ai.schemas import TranscriptResult, TranscriptSegment

__all__ = ["consolidate_highlights", "split_long_highlights"]

_COMPLETE_ENDINGS = (".", "!", "?", "؟", "。", "！", "？", "…")
_INCOMPLETE_ENDINGS = (",", "،", "؛", ";", ":", "—", "-")
_CONTINUATION_MARKERS = (
    "because",
    "so ",
    "therefore",
    "which means",
    "this means",
    "that means",
    "the reason",
    "for example",
    "for instance",
    "let me explain",
    "as a result",
    "after that",
    "then ",
    "next ",
    "however",
    "but ",
    "السبب",
    "لأن",
    "لذلك",
    "وهذا يعني",
    "هذا يعني",
    "على سبيل المثال",
    "مثلا",
    "بعد ذلك",
    "ثم",
    "لكن",
    "بالتالي",
)
_TOPIC_BOUNDARY_MARKERS = (
    "now let's",
    "next",
    "another",
    "finally",
    "moving on",
    "the next",
    "second",
    "third",
    "however",
    "الآن",
    "بعد ذلك",
    "أخيرا",
    "أولا",
    "ثانيا",
    "ثالثا",
    "لكن",
    "ننتقل",
)
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "because",
    "but",
    "by",
    "for",
    "from",
    "had",
    "has",
    "have",
    "he",
    "her",
    "his",
    "i",
    "if",
    "in",
    "is",
    "it",
    "its",
    "me",
    "my",
    "not",
    "of",
    "on",
    "or",
    "our",
    "she",
    "so",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "to",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "will",
    "with",
    "you",
    "your",
    "في",
    "من",
    "على",
    "إلى",
    "عن",
    "هذا",
    "هذه",
    "ذلك",
    "التي",
    "الذي",
    "الذين",
    "كان",
    "كانت",
    "هو",
    "هي",
    "هم",
    "أنا",
    "نحن",
    "أن",
    "إن",
    "لا",
    "ما",
    "مع",
    "كل",
    "بعد",
    "قبل",
    "ثم",
    "أو",
    "و",
}
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _segments_between(
    transcript: TranscriptResult, start: float, end: float
) -> list[TranscriptSegment]:
    return [
        segment
        for segment in transcript.segments
        if segment.end > start and segment.start < end and segment.text.strip()
    ]


def _text(segments: list[TranscriptSegment]) -> str:
    return " ".join(segment.text.strip() for segment in segments).strip()


def _text_between(transcript: TranscriptResult, start: float, end: float) -> str:
    if end <= start:
        return ""
    return _text(_segments_between(transcript, start, end))


def _normalise(text: str) -> str:
    return " ".join(_WORD_RE.findall(text.lower()))


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in _normalise(text).split()
        if len(token) >= 3 and token not in _STOPWORDS
    }


def _lexical_overlap(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))


def _bridge_overlap(bridge: str, text: str) -> float:
    bridge_tokens = _tokens(bridge)
    text_tokens = _tokens(text)
    if len(bridge_tokens) < 6 or not text_tokens:
        return 0.0
    return len(bridge_tokens & text_tokens) / len(bridge_tokens)


def _contains_marker(text: str, markers: tuple[str, ...]) -> bool:
    normalised = f" {_normalise(text)} "
    return any(f" {_normalise(marker)} " in normalised for marker in markers)


def _ends_incomplete(text: str) -> bool:
    return text.rstrip().endswith(_INCOMPLETE_ENDINGS)


def _starts_continuation(text: str) -> bool:
    normalised = _normalise(text)
    return any(
        normalised == marker or normalised.startswith(f"{marker} ")
        for marker in (_normalise(item) for item in _CONTINUATION_MARKERS)
    )


def _highlight_text(highlight: Highlight) -> str:
    if highlight.transcript_segments:
        return _text(highlight.transcript_segments)
    return " ".join(
        part
        for part in (highlight.title, highlight.description, highlight.selection_reason)
        if part
    )


def _expand_to_context(
    highlight: Highlight,
    transcript: TranscriptResult,
    config: HighlightSettings,
) -> Highlight:
    segments = transcript.segments
    overlapping = _segments_between(transcript, highlight.start_time, highlight.end_time)
    if not overlapping:
        return highlight
    first_index = next(
        (index for index, segment in enumerate(segments) if segment is overlapping[0]),
        None,
    )
    last_index = next(
        (index for index, segment in enumerate(segments) if segment is overlapping[-1]),
        None,
    )
    if first_index is None or last_index is None:
        return highlight

    start = overlapping[0].start
    end = overlapping[-1].end
    if first_index > 0:
        previous = segments[first_index - 1]
        if start - previous.end <= config.semantic_boundary_context_seconds and (
            _ends_incomplete(previous.text) or _starts_continuation(overlapping[0].text)
        ):
            start = previous.start
    if last_index + 1 < len(segments):
        following = segments[last_index + 1]
        if following.start - end <= config.semantic_boundary_context_seconds and (
            _ends_incomplete(overlapping[-1].text) or _starts_continuation(following.text)
        ):
            end = following.end

    if end - start > config.max_clip_seconds or end - start < config.min_clip_seconds:
        return highlight
    if start == highlight.start_time and end == highlight.end_time:
        return highlight
    return highlight.model_copy(
        update={
            "start_time": round(start, 3),
            "end_time": round(end, 3),
            "transcript_segments": _segments_between(transcript, start, end),
        }
    )


def _should_merge(
    group: list[Highlight],
    candidate: Highlight,
    transcript: TranscriptResult,
    config: HighlightSettings,
) -> bool:
    start = min(item.start_time for item in group)
    end = max(item.end_time for item in group)
    gap = candidate.start_time - end
    if gap > config.semantic_bridge_gap_seconds:
        return False
    combined_duration = max(end, candidate.end_time) - min(start, candidate.start_time)
    if combined_duration > config.max_clip_seconds:
        return False

    if not all(item.transcript_segments for item in group) or not candidate.transcript_segments:
        return False
    group_text = " ".join(_highlight_text(item) for item in group)
    candidate_text = _highlight_text(candidate)
    if not group_text or not candidate_text:
        return False
    bridge = _text_between(transcript, end, candidate.start_time) if gap > 0 else ""
    overlap = _lexical_overlap(group_text, candidate_text)

    if gap <= config.semantic_merge_gap_seconds:
        if overlap >= config.semantic_topic_overlap:
            return True
        if _contains_marker(bridge, _CONTINUATION_MARKERS):
            return True
        return bool(_starts_continuation(candidate_text) or _ends_incomplete(group_text))

    bridge_overlap = max(
        _bridge_overlap(bridge, group_text),
        _bridge_overlap(bridge, candidate_text),
    )
    if overlap >= config.semantic_topic_overlap and _contains_marker(
        bridge, _CONTINUATION_MARKERS
    ):
        return True
    if overlap >= min(0.5, config.semantic_topic_overlap + 0.15):
        return True
    if bridge_overlap >= config.semantic_topic_overlap:
        return True
    return bool(
        all(item.category == candidate.category for item in group)
        and _contains_marker(bridge, _TOPIC_BOUNDARY_MARKERS)
        and overlap >= config.semantic_topic_overlap / 2
    )


def _merge_group(
    group: list[Highlight],
    transcript: TranscriptResult,
    config: HighlightSettings,
) -> Highlight:
    start = min(item.start_time for item in group)
    end = max(item.end_time for item in group)
    segments = _segments_between(transcript, start, end)
    best = max(group, key=lambda item: (item.confidence, item.end_time - item.start_time))
    descriptions: list[str] = []
    for item in group:
        if item.description and item.description not in descriptions:
            descriptions.append(item.description)
    signals: dict[str, float] = {}
    for item in group:
        for key, value in item.detection_signals.items():
            signals[key] = max(signals.get(key, 0.0), value)
    signals["semantic_completeness"] = 1.0
    categories = {item.category for item in group}
    sources = {item.source for item in group}
    return Highlight(
        start_time=round(start, 3),
        end_time=round(end, 3),
        title=best.title,
        description=" ".join(descriptions),
        selection_reason=(
            "Related fragments were consolidated into one complete moment. "
            f"{best.selection_reason}"
        ).strip(),
        confidence=round(
            min(1.0, max(item.confidence for item in group) + config.semantic_completeness_bonus),
            4,
        ),
        category=best.category if len(categories) == 1 else "combined",
        transcript_segments=segments or best.transcript_segments,
        signal_score=max(
            (item.signal_score for item in group if item.signal_score is not None),
            default=None,
        ),
        detection_signals=signals,
        source=best.source if len(sources) == 1 else "consolidated",
    )




def split_long_highlights(
    highlights: list[Highlight],
    transcript: TranscriptResult | None,
    config: HighlightSettings,
) -> list[Highlight]:
    """Split only excessively long moments at transcript boundaries.

    The preferred target is a soft range. Highlights already within the target
    plus tolerance are left untouched, and a single long transcript segment is
    never cut mid-sentence.
    """
    target_min = config.target_clip_seconds_min
    target_max = config.target_clip_seconds_max
    if (
        target_min is None
        or target_max is None
        or transcript is None
        or not transcript.segments
        or not highlights
    ):
        return highlights

    max_allowed = target_max + config.target_duration_tolerance_seconds
    result: list[Highlight] = []
    for highlight in highlights:
        duration = highlight.end_time - highlight.start_time
        if duration <= max_allowed:
            result.append(highlight)
            continue
        segments = _segments_between(
            transcript, highlight.start_time, highlight.end_time
        )
        if len(segments) < 2:
            result.append(highlight)
            continue

        chunks: list[list[TranscriptSegment]] = []
        current: list[TranscriptSegment] = []
        for segment in segments:
            if current:
                current_duration = current[-1].end - current[0].start
                candidate_duration = segment.end - current[0].start
                if current_duration >= target_min and candidate_duration > target_max:
                    chunks.append(current)
                    current = []
            current.append(segment)
        if current:
            chunks.append(current)
        if len(chunks) < 2:
            result.append(highlight)
            continue

        if len(chunks) > 1:
            last_duration = chunks[-1][-1].end - chunks[-1][0].start
            previous_duration = chunks[-2][-1].end - chunks[-2][0].start
            if (
                last_duration < target_min
                and previous_duration + last_duration <= max_allowed
            ):
                chunks[-2].extend(chunks.pop())

        for index, chunk in enumerate(chunks, start=1):
            start = chunk[0].start
            end = chunk[-1].end
            if index == 1:
                start = min(start, highlight.start_time)
            if index == len(chunks):
                end = max(end, highlight.end_time)
            signals = dict(highlight.detection_signals)
            signals["duration_split"] = 1.0
            signals["original_duration"] = round(duration, 3)
            title = highlight.title
            if index > 1 and title:
                title = f"{title} — part {index}"
            result.append(
                highlight.model_copy(
                    update={
                        "start_time": round(start, 3),
                        "end_time": round(end, 3),
                        "title": title,
                        "selection_reason": (
                            "Split from a longer coherent moment to stay close to the "
                            f"{target_min:g}-{target_max:g}s target. "
                            f"{highlight.selection_reason}"
                        ).strip(),
                        "transcript_segments": chunk,
                        "detection_signals": signals,
                    }
                )
            )
    return sorted(result, key=lambda item: item.start_time)


def consolidate_highlights(
    highlights: list[Highlight],
    transcript: TranscriptResult | None,
    config: HighlightSettings,
    *,
    content_type: ContentType,
) -> list[Highlight]:
    if (
        not config.semantic_consolidation
        or transcript is None
        or not transcript.segments
        or not highlights
        or content_type in {ContentType.GAMING, ContentType.LIVE}
    ):
        return highlights

    expanded = [
        _expand_to_context(highlight, transcript, config) for highlight in highlights
    ]
    ordered = sorted(expanded, key=lambda item: item.start_time)
    groups: list[list[Highlight]] = [[ordered[0]]]
    for candidate in ordered[1:]:
        if _should_merge(groups[-1], candidate, transcript, config):
            groups[-1].append(candidate)
        else:
            groups.append([candidate])

    consolidated = [
        _merge_group(group, transcript, config) if len(group) > 1 else group[0]
        for group in groups
    ]
    return sorted(consolidated, key=lambda item: item.start_time)
