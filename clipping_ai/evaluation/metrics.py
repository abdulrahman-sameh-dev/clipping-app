"""Pure, deterministic metrics for offline highlight-discovery evaluation."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from clipping_ai.analysis.config import HighlightSettings
from clipping_ai.evaluation.models import (
    BoundaryMetrics,
    CapStatistics,
    ClassificationMetrics,
    ContextStatistics,
    FallbackStatistics,
    MatchResult,
    OverlapPair,
    QuartileStat,
    RedundancyMetrics,
    Span,
    SpanMatch,
    StageCount,
    StageSurvival,
    TemporalCoverage,
    TopicCoverage,
)
from clipping_ai.schemas import TranscriptResult


def _coerce_span(value: object) -> Span | None:
    if value is None:
        return None
    if isinstance(value, Span):
        return value
    if isinstance(value, Mapping):
        raw_start = value.get("start", value.get("start_time"))
        raw_end = value.get("end", value.get("end_time"))
        topic = value.get("topic")
        label = value.get("label")
    elif isinstance(value, Sequence) and not isinstance(value, str):
        if len(value) < 2:
            return None
        raw_start, raw_end = value[0], value[1]
        topic = None
        label = None
    else:
        return None
    try:
        start = float(raw_start)
        end = float(raw_end)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(start) or not math.isfinite(end):
        return None
    if start < 0 or end <= start:
        return None
    return Span(
        start=start,
        end=end,
        topic=str(topic) if topic is not None else None,
        label=str(label) if label is not None else None,
    )


def _coerce_spans(values: object) -> tuple[list[Span], int]:
    if isinstance(values, (Span, Mapping)) or values is None:
        iterable: Sequence[object] = [values]
    elif isinstance(values, Sequence) and len(values) == 2 and all(
        isinstance(item, (int, float)) for item in values
    ):
        iterable = [values]
    elif isinstance(values, Sequence):
        iterable = list(values)
    else:
        iterable = [values]
    spans: list[Span] = []
    invalid = 0
    for value in iterable:
        span = _coerce_span(value)
        if span is None:
            invalid += 1
        else:
            spans.append(span)
    return spans, invalid


def _intersection(left: Span, right: Span) -> float:
    start = max(left.start, right.start)
    end = min(left.end, right.end)
    return max(0.0, end - start)


def span_iou(left: object, right: object) -> float:
    left_span = _coerce_span(left)
    right_span = _coerce_span(right)
    if left_span is None or right_span is None:
        return 0.0
    intersection = _intersection(left_span, right_span)
    union = (left_span.end - left_span.start) + (right_span.end - right_span.start) - intersection
    return intersection / union if union > 0 else 0.0


def match_spans(
    predictions: object,
    gold: object,
    *,
    iou_threshold: float = 0.5,
) -> MatchResult:
    valid_predictions, invalid_predictions = _coerce_spans(predictions)
    valid_gold, invalid_gold = _coerce_spans(gold)
    candidates: list[tuple[float, float, int, int, float]] = []
    for prediction_index, prediction in enumerate(valid_predictions):
        for gold_index, gold_span in enumerate(valid_gold):
            overlap = _intersection(prediction, gold_span)
            score = span_iou(prediction, gold_span)
            if score >= iou_threshold:
                candidates.append((score, overlap, prediction_index, gold_index, score))
    candidates.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))
    used_predictions: set[int] = set()
    used_gold: set[int] = set()
    matches: list[SpanMatch] = []
    for score, overlap, prediction_index, gold_index, _ in candidates:
        if prediction_index in used_predictions or gold_index in used_gold:
            continue
        prediction = valid_predictions[prediction_index]
        gold_span = valid_gold[gold_index]
        matches.append(
            SpanMatch(
                prediction_index=prediction_index,
                gold_index=gold_index,
                iou=round(score, 6),
                intersection=round(overlap, 6),
                start_error=round(abs(prediction.start - gold_span.start), 6),
                end_error=round(abs(prediction.end - gold_span.end), 6),
            )
        )
        used_predictions.add(prediction_index)
        used_gold.add(gold_index)
    return MatchResult(
        matches=matches,
        unmatched_prediction_indices=[
            index for index in range(len(valid_predictions)) if index not in used_predictions
        ],
        unmatched_gold_indices=[
            index for index in range(len(valid_gold)) if index not in used_gold
        ],
        valid_prediction_count=len(valid_predictions),
        valid_gold_count=len(valid_gold),
        invalid_prediction_count=invalid_predictions,
        invalid_gold_count=invalid_gold,
    )


def classification_metrics(
    predictions: object,
    gold: object,
    *,
    iou_threshold: float = 0.5,
) -> ClassificationMetrics:
    matches = match_spans(predictions, gold, iou_threshold=iou_threshold)
    true_positives = len(matches.matches)
    if matches.valid_prediction_count:
        precision = true_positives / matches.valid_prediction_count
    else:
        precision = 1.0 if not matches.valid_gold_count else 0.0
    recall = (
        true_positives / matches.valid_gold_count
        if matches.valid_gold_count
        else 1.0
    )
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return ClassificationMetrics(
        precision=round(precision, 6),
        recall=round(recall, 6),
        f1=round(f1, 6),
        true_positives=true_positives,
        predicted_count=matches.valid_prediction_count,
        gold_count=matches.valid_gold_count,
        invalid_prediction_count=matches.invalid_prediction_count,
        invalid_gold_count=matches.invalid_gold_count,
    )


def boundary_metrics(matches: MatchResult) -> BoundaryMetrics:
    if not matches.matches:
        return BoundaryMetrics(
            matched_count=0,
            mean_start_error=0.0,
            mean_end_error=0.0,
            max_start_error=0.0,
            max_end_error=0.0,
        )
    start_errors = [item.start_error for item in matches.matches]
    end_errors = [item.end_error for item in matches.matches]
    return BoundaryMetrics(
        matched_count=len(matches.matches),
        mean_start_error=round(sum(start_errors) / len(start_errors), 6),
        mean_end_error=round(sum(end_errors) / len(end_errors), 6),
        max_start_error=round(max(start_errors), 6),
        max_end_error=round(max(end_errors), 6),
    )


def _quartile_bounds(duration: float, bins: int) -> list[tuple[float, float]]:
    if bins <= 0:
        return []
    effective = max(0.0, duration)
    width = effective / bins
    return [
        (round(index * width, 6), round((index + 1) * width, 6)) for index in range(bins)
    ]


def _midpoint(span: Span) -> float:
    return (span.start + span.end) / 2.0


def _count_by_midpoint(spans: Sequence[Span], bounds: Sequence[tuple[float, float]]) -> list[int]:
    counts = [0] * len(bounds)
    for span in spans:
        point = _midpoint(span)
        for index, (start, end) in enumerate(bounds):
            if start <= point < end or (index == len(bounds) - 1 and math.isclose(point, end)):
                counts[index] += 1
                break
    return counts


def quartile_coverage(
    predictions: object,
    gold: object,
    *,
    duration: float,
    bins: int = 4,
) -> TemporalCoverage:
    valid_predictions, _ = _coerce_spans(predictions)
    valid_gold, _ = _coerce_spans(gold)
    bounds = _quartile_bounds(duration, bins)
    stats: list[QuartileStat] = []
    for index, (start, end) in enumerate(bounds):
        window = Span(start=start, end=end) if end > start else None
        gold_seconds = (
            sum(_intersection(span, window) for span in valid_gold) if window else 0.0
        )
        selected_seconds = (
            sum(_intersection(span, window) for span in valid_predictions) if window else 0.0
        )
        coverage_ratio = (
            min(1.0, selected_seconds / gold_seconds) if gold_seconds > 0 else 0.0
        )
        stats.append(
            QuartileStat(
                quartile=index + 1,
                start=start,
                end=end,
                gold_seconds=round(gold_seconds, 6),
                selected_seconds=round(selected_seconds, 6),
                gold_count=_count_by_midpoint(valid_gold, [(start, end)])[0]
                if end > start
                else 0,
                selected_count=_count_by_midpoint(valid_predictions, [(start, end)])[0]
                if end > start
                else 0,
                coverage_ratio=round(coverage_ratio, 6),
            )
        )
    midpoint_counts = _count_by_midpoint(valid_predictions, bounds)
    return TemporalCoverage(
        bins=stats,
        midpoint_counts=midpoint_counts,
        covered_quartiles=sum(1 for count in midpoint_counts if count > 0),
    )


def temporal_distribution(
    spans: object,
    *,
    duration: float,
    bins: int = 4,
) -> TemporalCoverage:
    return quartile_coverage(spans, [], duration=duration, bins=bins)


def redundancy_metrics(spans: object) -> RedundancyMetrics:
    valid, _ = _coerce_spans(spans)
    pairs: list[OverlapPair] = []
    scores: list[float] = []
    for left_index, left in enumerate(valid):
        for right_index in range(left_index + 1, len(valid)):
            right = valid[right_index]
            overlap = _intersection(left, right)
            score = span_iou(left, right)
            scores.append(score)
            if overlap > 0:
                pairs.append(
                    OverlapPair(
                        left_index=left_index,
                        right_index=right_index,
                        iou=round(score, 6),
                        overlap_seconds=round(overlap, 6),
                    )
                )
    return RedundancyMetrics(
        pairwise_comparisons=len(scores),
        overlap_count=len(pairs),
        mean_iou=round(sum(scores) / len(scores), 6) if scores else 0.0,
        max_iou=round(max(scores), 6) if scores else 0.0,
        overlapping_pairs=pairs,
    )


def distinct_topic_coverage(
    gold: object,
    predictions: object,
    *,
    iou_threshold: float = 0.5,
) -> TopicCoverage:
    valid_gold, _ = _coerce_spans(gold)
    matches = match_spans(predictions, valid_gold, iou_threshold=iou_threshold)
    matched_gold = {item.gold_index for item in matches.matches}
    topics = sorted({span.topic for span in valid_gold if span.topic})
    covered = {
        topic: any(
            index in matched_gold
            for index, span in enumerate(valid_gold)
            if span.topic == topic
        )
        for topic in topics
    }
    total = len(topics)
    covered_count = sum(1 for value in covered.values() if value)
    return TopicCoverage(
        topics=covered,
        covered_topics=covered_count,
        total_topics=total,
        coverage_ratio=round(covered_count / total, 6) if total else 0.0,
    )


def candidate_survival(stage_counts: Sequence[StageCount]) -> list[StageSurvival]:
    if not stage_counts:
        return []
    initial = max((stage.before for stage in stage_counts), default=0) or stage_counts[-1].after
    results: list[StageSurvival] = []
    for stage in stage_counts:
        stage_ratio = stage.after / stage.before if stage.before else 0.0
        cumulative = stage.after / initial if initial else 0.0
        results.append(
            StageSurvival(
                stage=stage.stage,
                before=stage.before,
                after=stage.after,
                stage_survival_ratio=round(stage_ratio, 6),
                cumulative_survival_ratio=round(cumulative, 6),
            )
        )
    return results


def _merged_coverage_seconds(spans: Sequence[Any]) -> float:
    intervals = sorted((float(span.start), float(span.end)) for span in spans)
    merged: list[list[float]] = []
    for start, end in intervals:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return sum(end - start for start, end in merged)


def context_statistics(
    transcript: TranscriptResult,
    config: HighlightSettings | None = None,
) -> ContextStatistics:
    settings = config or HighlightSettings()
    text = transcript.text or ""
    visible_text = text[: settings.llm_max_transcript_chars]
    segments = list(transcript.segments)
    visible_segments = segments[:2000]
    visible_end = max((segment.end for segment in visible_segments), default=0.0)
    duration = transcript.duration or max((segment.end for segment in segments), default=0.0)
    hidden_seconds = max(0.0, duration - visible_end)
    segment_coverage = _merged_coverage_seconds(segments)
    return ContextStatistics(
        transcript_chars=len(text),
        visible_chars=len(visible_text),
        hidden_chars=max(0, len(text) - len(visible_text)),
        text_truncated=len(text) > settings.llm_max_transcript_chars,
        transcript_segments=len(segments),
        visible_segments=len(visible_segments),
        hidden_segments=max(0, len(segments) - len(visible_segments)),
        segments_truncated=len(segments) > 2000,
        transcript_duration=round(duration, 6),
        visible_segment_end=round(visible_end, 6),
        hidden_seconds=round(hidden_seconds, 6),
        segment_coverage_seconds=round(segment_coverage, 6),
        segment_coverage_ratio=round(
            segment_coverage / duration if duration > 0 else 0.0,
            6,
        ),
    )


def cap_statistics(
    *,
    candidates_before_llm_cap: int,
    candidates_to_llm: int,
    final_candidates_before_cap: int,
    final_candidates: int,
    config: HighlightSettings | None = None,
    fallback_used: bool = False,
) -> CapStatistics:
    settings = config or HighlightSettings()
    return CapStatistics(
        candidates_before_llm_cap=candidates_before_llm_cap,
        candidates_to_llm=candidates_to_llm,
        llm_candidate_limit=settings.llm_max_candidates,
        llm_candidates_hidden=max(0, candidates_before_llm_cap - candidates_to_llm),
        final_candidates_before_cap=final_candidates_before_cap,
        final_candidates=final_candidates,
        max_highlights=settings.max_highlights,
        final_candidates_hidden=max(0, final_candidates_before_cap - final_candidates),
        fallback_used=fallback_used,
    )


def fallback_statistics(values: Sequence[bool]) -> FallbackStatistics:
    total = len(values)
    fallbacks = sum(1 for value in values if value)
    return FallbackStatistics(
        evaluated_calls=total,
        fallback_calls=fallbacks,
        fallback_rate=round(fallbacks / total, 6) if total else 0.0,
    )
