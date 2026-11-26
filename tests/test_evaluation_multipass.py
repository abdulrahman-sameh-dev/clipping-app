from __future__ import annotations

from clipping_ai.analysis.config import HighlightSettings
from clipping_ai.evaluation.fixtures import load_corpus
from clipping_ai.evaluation.multipass import (
    WindowConfig,
    assign_segments_to_windows,
    plan_windows,
    run_window_experiments,
    run_window_simulation,
)
from clipping_ai.schemas import TranscriptSegment


def test_plan_windows_covers_full_duration_with_overlap_and_stable_ids() -> None:
    windows = plan_windows(duration=300.0, window_seconds=120.0, overlap_seconds=30.0)
    assert [(window.start, window.end) for window in windows] == [
        (0.0, 120.0),
        (90.0, 210.0),
        (180.0, 300.0),
    ]
    assert len({window.window_id for window in windows}) == len(windows)
    assert plan_windows(300, 120, 30) == windows


def test_window_assignment_preserves_absolute_timestamps_and_assigns_every_segment() -> None:
    segments = [
        TranscriptSegment(id=1, start=0, end=10, text="one"),
        TranscriptSegment(id=2, start=85, end=95, text="boundary"),
        TranscriptSegment(id=3, start=290, end=300, text="end"),
    ]
    windows = plan_windows(300, 120, 30)
    assignments = assign_segments_to_windows(segments, windows)
    assert sorted(assignment.segment.id for assignment in assignments) == [1, 2, 2, 3]
    boundary = [item for item in assignments if item.segment.id == 2]
    assert [(item.window.start, item.window.end) for item in boundary] == [
        (0.0, 120.0),
        (90.0, 210.0),
    ]
    starts_by_id = {segment.id: segment.start for segment in segments}
    assert all(
        item.segment.start == starts_by_id[item.segment.id]
        for item in assignments
        if item.segment.id is not None
    )


def test_multipass_resume_skips_complete_and_retries_first_incomplete() -> None:
    fixture = next(item for item in load_corpus().fixtures if item.fixture_id.endswith("long-multitopic"))
    config = WindowConfig(window_seconds=600.0, overlap_seconds=60.0)
    interrupted = run_window_simulation(
        fixture,
        highlight_config=HighlightSettings(),
        window_config=config,
        interrupt_after=0,
    )
    assert interrupted.records[0].status == "complete"
    assert any(record.status == "pending" for record in interrupted.records[1:])

    resumed = run_window_simulation(
        fixture,
        highlight_config=HighlightSettings(),
        window_config=config,
        prior_records=interrupted.records,
    )
    assert resumed.records[0].status == "skipped_complete"
    assert sum(record.status == "resumed" for record in resumed.records) >= 1
    assert resumed.windows_processed + resumed.windows_skipped + resumed.windows_resumed == len(
        resumed.records
    )


def test_multipass_retains_candidates_from_all_time_regions_and_allows_more_than_llm_cap() -> None:
    fixture = next(item for item in load_corpus().fixtures if "late-moment" in item.fixture_id)
    result = run_window_simulation(
        fixture,
        highlight_config=HighlightSettings(llm_max_candidates=3),
        window_config=WindowConfig(window_seconds=1800.0, overlap_seconds=180.0),
    )
    assert result.candidates_discovered_before_cap > 3
    assert any(record.start >= 5400 for record in result.records if record.candidates_before_cap)
    assert result.candidates_discovered_before_cap >= result.candidates_to_llm_total


def test_multipass_handles_adjacent_distinct_and_duplicate_boundary_candidates() -> None:
    fixture = next(item for item in load_corpus().fixtures if "adjacent-distinct" in item.fixture_id)
    result = run_window_simulation(
        fixture,
        highlight_config=HighlightSettings(),
        window_config=WindowConfig(window_seconds=240.0, overlap_seconds=60.0),
    )
    assert result.segments_unassigned == 0
    assert result.windows_rejected == 0
    assert result.redundancy.overlap_count >= 0
    starts = [span.start for span in result.selected_spans]
    assert any(220 <= start <= 260 for start in starts)


def test_window_experiments_are_deterministic_and_choose_a_config() -> None:
    fixtures = load_corpus().fixtures[:3]
    first = run_window_experiments(fixtures)
    second = run_window_experiments(fixtures)
    assert first == second
    assert first.experiments
    assert first.recommended.window_seconds in {120.0, 300.0, 600.0}
    assert first.recommended.overlap_seconds in {0.0, 30.0, 60.0}
    assert first.recommended.rationale
