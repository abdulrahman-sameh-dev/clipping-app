from __future__ import annotations

import pytest

from clipping_ai.analysis.config import HighlightSettings
from clipping_ai.evaluation.metrics import (
    boundary_metrics,
    candidate_survival,
    cap_statistics,
    classification_metrics,
    context_statistics,
    distinct_topic_coverage,
    fallback_statistics,
    match_spans,
    quartile_coverage,
    redundancy_metrics,
    span_iou,
    temporal_distribution,
)
from clipping_ai.evaluation.models import Span
from clipping_ai.schemas import TranscriptResult, TranscriptSegment


def test_span_iou_handles_overlap_invalid_and_empty() -> None:
    assert span_iou((0, 10), (0, 10)) == pytest.approx(1.0)
    assert span_iou((0, 10), (5, 15)) == pytest.approx(5 / 15)
    assert span_iou((10, 5), (0, 10)) == 0.0
    assert span_iou(None, (0, 10)) == 0.0
    assert span_iou((0, 0), (0, 10)) == 0.0


def test_match_spans_is_deterministic_with_overlapping_predictions() -> None:
    matches = match_spans(
        [(0, 10), (5, 15)],
        [(0, 12)],
        iou_threshold=0.5,
    )
    assert [pair.prediction_index for pair in matches.matches] == [0]
    assert matches.unmatched_prediction_indices == [1]
    assert matches.unmatched_gold_indices == []
    assert matches.matches[0].iou == pytest.approx(10 / 12)


def test_classification_metrics_handle_empty_and_malformed_inputs() -> None:
    empty = classification_metrics([], [])
    assert (empty.precision, empty.recall, empty.f1) == (1.0, 1.0, 1.0)

    missing = classification_metrics([], [(0, 10)])
    assert (missing.precision, missing.recall, missing.f1) == (0.0, 0.0, 0.0)

    malformed = classification_metrics([(10, 5), (0, 10)], [(0, 10)])
    assert malformed.invalid_prediction_count == 1
    assert malformed.precision == pytest.approx(1.0)
    assert malformed.recall == pytest.approx(1.0)


def test_boundary_quartile_and_temporal_metrics() -> None:
    matches = match_spans([(8, 18)], [(10, 20)], iou_threshold=0.5)
    boundaries = boundary_metrics(matches)
    assert boundaries.mean_start_error == pytest.approx(2.0)
    assert boundaries.mean_end_error == pytest.approx(2.0)

    coverage = quartile_coverage([(8, 18)], [(10, 20)], duration=40)
    assert [item.quartile for item in coverage.bins] == [1, 2, 3, 4]
    assert coverage.bins[0].gold_seconds == pytest.approx(0.0)
    assert coverage.bins[0].selected_seconds == pytest.approx(2.0)
    assert coverage.bins[1].gold_seconds == pytest.approx(10.0)
    assert coverage.bins[1].selected_seconds == pytest.approx(8.0)
    assert coverage.bins[1].coverage_ratio == pytest.approx(0.8)

    distribution = temporal_distribution([(8, 18)], duration=40)
    assert distribution.midpoint_counts == [0, 1, 0, 0]


def test_redundancy_and_topic_coverage_are_deterministic() -> None:
    redundancy = redundancy_metrics([(0, 10), (5, 15), (30, 40)])
    assert redundancy.overlap_count == 1
    assert redundancy.overlapping_pairs[0].left_index == 0
    assert redundancy.overlapping_pairs[0].right_index == 1

    topics = distinct_topic_coverage(
        [Span(start=0, end=10, topic="intro"), Span(start=30, end=40, topic="close")],
        [(0, 10)],
    )
    assert topics.topics == {"intro": True, "close": False}
    assert topics.coverage_ratio == pytest.approx(0.5)


def test_candidate_survival_context_cap_and_fallback_stats() -> None:
    from clipping_ai.evaluation.models import StageCount

    stages = [
        StageCount(stage="built", before=40, after=40, removed=0, details={}),
        StageCount(stage="dedupe", before=40, after=30, removed=10, details={}),
        StageCount(stage="confidence", before=30, after=15, removed=15, details={}),
        StageCount(stage="llm_cap", before=15, after=12, removed=3, details={}),
    ]
    survival = candidate_survival(stages)
    assert survival[-1].stage_survival_ratio == pytest.approx(12 / 15)
    assert survival[-1].cumulative_survival_ratio == pytest.approx(12 / 40)

    generated = [
        StageCount(stage="transcript_candidates", before=0, after=100, removed=0, details={}),
        StageCount(stage="signal_candidates", before=100, after=160, removed=0, details={}),
        StageCount(stage="candidate_dedupe", before=160, after=120, removed=40, details={}),
    ]
    generated_survival = candidate_survival(generated)
    assert generated_survival[0].cumulative_survival_ratio == pytest.approx(100 / 160)
    assert generated_survival[-1].cumulative_survival_ratio == pytest.approx(120 / 160)

    transcript = TranscriptResult(
        text="x" * 50_000,
        language="en",
        duration=600.0,
        segments=[
            TranscriptSegment(start=float(index * 0.2), end=float(index * 0.2 + 0.1), text="word")
            for index in range(2500)
        ],
    )
    context = context_statistics(transcript, HighlightSettings())
    assert context.transcript_chars == 50_000
    assert context.visible_chars == 24_000
    assert context.transcript_segments == 2500
    assert context.visible_segments == 2000
    assert context.hidden_segments == 500
    assert context.segments_truncated is True

    caps = cap_statistics(
        candidates_before_llm_cap=20,
        candidates_to_llm=12,
        final_candidates_before_cap=14,
        final_candidates=10,
        fallback_used=True,
    )
    assert caps.llm_candidates_hidden == 8
    assert caps.final_candidates_hidden == 4

    fallback = fallback_statistics([True, False, True])
    assert fallback.evaluated_calls == 3
    assert fallback.fallback_calls == 2
    assert fallback.fallback_rate == pytest.approx(2 / 3)
