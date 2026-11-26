"""Offline simulation of hierarchical, time-window discovery.

This module is evaluation-only. It does not change production discovery behavior.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import Field, model_validator

from clipping_ai.analysis.config import HighlightSettings
from clipping_ai.evaluation.metrics import classification_metrics, redundancy_metrics
from clipping_ai.evaluation.models import (
    EvaluationFixture,
    MultiPassSummary,
    RecommendedWindow,
    SegmentWindowAssignment,
    Span,
    StrictEvaluationModel,
    WindowExperiment,
    WindowExperimentSet,
    WindowPlan,
    WindowRecord,
)
from clipping_ai.highlights.candidates import build_transcript_candidates, dedupe_candidates
from clipping_ai.highlights.engine import _dedupe_highlights, _highlight_from_candidate
from clipping_ai.highlights.semantic import consolidate_highlights, split_long_highlights
from clipping_ai.schemas import TranscriptSegment


def _timeline_key(item: Any) -> float:
    value = getattr(item, "start_time", None)
    if value is None:
        value = getattr(item, "start", 0.0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _spread_selection(items: Sequence[Any], limit: int) -> list[Any]:
    if limit <= 0:
        return []
    ordered = sorted(items, key=_timeline_key)
    if len(ordered) <= limit:
        return ordered
    if limit == 1:
        return [ordered[-1]]
    last = len(ordered) - 1
    indices = sorted({round(index * last / (limit - 1)) for index in range(limit)})
    return [ordered[index] for index in indices]


class WindowConfig(StrictEvaluationModel):
    window_seconds: float = Field(default=600.0, gt=0)
    overlap_seconds: float = Field(default=60.0, ge=0)

    @model_validator(mode="after")
    def _valid_overlap(self) -> WindowConfig:
        if self.overlap_seconds >= self.window_seconds:
            raise ValueError("overlap_seconds must be smaller than window_seconds")
        return self


def plan_windows(
    duration: float,
    window_seconds: float,
    overlap_seconds: float,
) -> list[WindowPlan]:
    if duration <= 0:
        return []
    config = WindowConfig(
        window_seconds=window_seconds,
        overlap_seconds=overlap_seconds,
    )
    step = config.window_seconds - config.overlap_seconds
    windows: list[WindowPlan] = []
    index = 0
    start = 0.0
    while start < duration:
        end = min(duration, start + config.window_seconds)
        if end <= start:
            break
        windows.append(
            WindowPlan(
                window_id=f"w{index:04d}_{int(round(start)):06d}_{int(round(end)):06d}",
                index=index,
                start=round(start, 6),
                end=round(end, 6),
            )
        )
        if end >= duration:
            break
        start = round(start + step, 6)
        index += 1
    return windows


def assign_segments_to_windows(
    segments: Sequence[TranscriptSegment],
    windows: Sequence[WindowPlan],
) -> list[SegmentWindowAssignment]:
    assignments: list[SegmentWindowAssignment] = []
    for window in windows:
        for segment in segments:
            overlaps = segment.end > window.start and segment.start < window.end
            if overlaps:
                assignments.append(
                    SegmentWindowAssignment(window=window, segment=segment)
                )
    return assignments


def _spans_from_highlights(highlights: Sequence[object]) -> list[Span]:
    spans: list[Span] = []
    for highlight in highlights:
        start = getattr(highlight, "start_time", None)
        end = getattr(highlight, "end_time", None)
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            spans.append(Span(start=float(start), end=float(end)))
    return spans


def run_window_simulation(
    fixture: EvaluationFixture,
    *,
    highlight_config: HighlightSettings,
    window_config: WindowConfig,
    interrupt_after: int | None = None,
    failed_windows: Sequence[int] = (),
    prior_records: Sequence[WindowRecord] = (),
) -> MultiPassSummary:
    windows = plan_windows(
        duration=fixture.duration,
        window_seconds=window_config.window_seconds,
        overlap_seconds=window_config.overlap_seconds,
    )
    assignments = assign_segments_to_windows(fixture.segments, windows)
    assignments_by_window: dict[str, list[TranscriptSegment]] = {}
    assigned_segment_ids: set[int] = set()
    for assignment in assignments:
        assignments_by_window.setdefault(assignment.window.window_id, []).append(
            assignment.segment
        )
        if assignment.segment.id is not None:
            assigned_segment_ids.add(assignment.segment.id)

    prior_by_index = {record.index: record for record in prior_records}
    failed = set(failed_windows)
    records: list[WindowRecord] = []
    discovered_candidates = []
    window_by_candidate_id: dict[int, int] = {}
    interrupted = False

    for window in windows:
        prior = prior_by_index.get(window.index)
        window_segments = assignments_by_window.get(window.window_id, [])
        segment_ids = [segment.id for segment in window_segments if segment.id is not None]

        if interrupted:
            records.append(
                WindowRecord(
                    window_id=window.window_id,
                    index=window.index,
                    start=window.start,
                    end=window.end,
                    status="pending",
                    attempts=prior.attempts if prior else 0,
                    segment_ids=segment_ids,
                    candidates_before_cap=0,
                    candidates_to_llm=0,
                    candidates_after_window=0,
                )
            )
            continue

        status: Literal[
            "complete",
            "skipped_complete",
            "resumed",
            "pending",
            "rejected",
        ]
        if prior is not None and prior.status in {"complete", "skipped_complete"}:
            status = "skipped_complete"
            attempts = prior.attempts
        else:
            status = "resumed" if prior is not None else "complete"
            attempts = (prior.attempts if prior is not None else 0) + 1

        if window.index in failed:
            records.append(
                WindowRecord(
                    window_id=window.window_id,
                    index=window.index,
                    start=window.start,
                    end=window.end,
                    status="rejected",
                    attempts=attempts,
                    segment_ids=segment_ids,
                    candidates_before_cap=0,
                    candidates_to_llm=0,
                    candidates_after_window=0,
                    error="simulated window failure",
                )
            )
            continue

        window_transcript = fixture.transcript().model_copy(
            update={"segments": window_segments, "duration": fixture.duration}
        )
        window_candidates = build_transcript_candidates(
            window_transcript,
            highlight_config,
            content_type=fixture.content_type,
        )
        before_cap = len(window_candidates)
        visible_candidates = window_candidates[: highlight_config.llm_max_candidates]
        discovered_candidates.extend(window_candidates)
        for candidate in window_candidates:
            window_by_candidate_id[id(candidate)] = window.index
        records.append(
            WindowRecord(
                window_id=window.window_id,
                index=window.index,
                start=window.start,
                end=window.end,
                status=status,
                attempts=attempts,
                segment_ids=segment_ids,
                candidates_before_cap=before_cap,
                candidates_to_llm=len(visible_candidates),
                candidates_after_window=len(visible_candidates),
            )
        )
        if interrupt_after is not None and window.index == interrupt_after:
            interrupted = True

    deduped_candidates = dedupe_candidates(
        discovered_candidates,
        overlap_ratio=highlight_config.dedupe_overlap_ratio,
    )
    candidates_by_window: dict[int, list[Any]] = {}
    for candidate in deduped_candidates:
        window_index = window_by_candidate_id.get(id(candidate))
        if window_index is not None:
            candidates_by_window.setdefault(window_index, []).append(candidate)
    selected_candidates = [
        max(items, key=lambda item: (item.score, _timeline_key(item)))
        for _, items in sorted(candidates_by_window.items())
    ]
    selected_ids = {id(item) for item in selected_candidates}
    remaining = sorted(
        (item for item in deduped_candidates if id(item) not in selected_ids),
        key=lambda item: item.score,
        reverse=True,
    )
    selected_candidates.extend(
        remaining[: max(0, highlight_config.max_highlights - len(selected_candidates))]
    )
    selected_candidates = _spread_selection(selected_candidates, highlight_config.max_highlights)
    highlights = [_highlight_from_candidate(candidate) for candidate in selected_candidates]
    highlights = consolidate_highlights(
        highlights,
        fixture.transcript(),
        highlight_config,
        content_type=fixture.content_type,
    )
    highlights = split_long_highlights(highlights, fixture.transcript(), highlight_config)
    highlights = _dedupe_highlights(
        highlights,
        overlap_ratio=highlight_config.dedupe_overlap_ratio,
    )
    highlights = _spread_selection(highlights, highlight_config.max_highlights)
    selected_spans = _spans_from_highlights(highlights)

    return MultiPassSummary(
        window_seconds=window_config.window_seconds,
        overlap_seconds=window_config.overlap_seconds,
        windows_total=len(windows),
        windows_processed=sum(1 for record in records if record.status == "complete"),
        windows_skipped=sum(1 for record in records if record.status == "skipped_complete"),
        windows_resumed=sum(1 for record in records if record.status == "resumed"),
        windows_rejected=sum(1 for record in records if record.status == "rejected"),
        segments_assigned=len(assigned_segment_ids),
        segments_unassigned=len(fixture.segments) - len(assigned_segment_ids),
        candidates_discovered_before_cap=len(discovered_candidates),
        candidates_to_llm_total=sum(record.candidates_to_llm for record in records),
        candidates_after_global_dedupe=len(deduped_candidates),
        final_highlights=len(highlights),
        selected_spans=selected_spans,
        classification=classification_metrics(selected_spans, fixture.gold_spans),
        redundancy=redundancy_metrics(selected_spans),
        records=records,
    )


def run_window_experiments(fixtures: Sequence[EvaluationFixture]) -> WindowExperimentSet:
    experiments: list[WindowExperiment] = []
    for window_seconds in (120.0, 300.0, 600.0):
        for overlap_seconds in (0.0, 30.0, 60.0):
            summaries = [
                run_window_simulation(
                    fixture,
                    highlight_config=HighlightSettings(),
                    window_config=WindowConfig(
                        window_seconds=window_seconds,
                        overlap_seconds=overlap_seconds,
                    ),
                )
                for fixture in fixtures
            ]
            if not summaries:
                continue
            f1 = sum(item.classification.f1 for item in summaries) / len(summaries)
            recall = sum(item.classification.recall for item in summaries) / len(summaries)
            covered = sum(item.classification.true_positives for item in summaries)
            redundancy = sum(item.redundancy.mean_iou for item in summaries) / len(summaries)
            windows_total = sum(item.windows_total for item in summaries)
            discovered = sum(item.candidates_discovered_before_cap for item in summaries)
            coverage_ratio = covered / max(
                1, sum(len(fixture.gold_spans) for fixture in fixtures)
            )
            score = f1 + 0.25 * coverage_ratio - 0.1 * redundancy - 0.00001 * windows_total
            experiments.append(
                WindowExperiment(
                    window_seconds=window_seconds,
                    overlap_seconds=overlap_seconds,
                    f1=round(f1, 6),
                    recall=round(recall, 6),
                    covered_quartiles=covered,
                    redundancy_iou=round(redundancy, 6),
                    windows_total=windows_total,
                    candidates_discovered=discovered,
                    score=round(score, 6),
                )
            )
    recommended = max(
        experiments,
        key=lambda item: (
            item.score,
            item.candidates_discovered,
            -item.windows_total,
            -item.window_seconds,
        ),
    )
    return WindowExperimentSet(
        experiments=experiments,
        recommended=RecommendedWindow(
            window_seconds=recommended.window_seconds,
            overlap_seconds=recommended.overlap_seconds,
            rationale=(
                "Selected by the deterministic experiment score: mean F1, 0.25 times "
                "gold-span match ratio, minus 0.1 times redundancy, minus a small "
                "window-count penalty."
            ),
        ),
    )
