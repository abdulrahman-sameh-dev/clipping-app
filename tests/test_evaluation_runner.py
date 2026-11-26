from __future__ import annotations

import json
from pathlib import Path

import pytest

from clipping_ai.analysis.config import HighlightSettings
from clipping_ai.evaluation.models import StageEvent
from clipping_ai.evaluation.runner import (
    build_report,
    run_evaluation,
    write_reports,
)
from clipping_ai.highlights.engine import detect_highlights, prepare_highlights
from clipping_ai.highlights.models import ContentType, HighlightResult


class CollectingObserver:
    def __init__(self) -> None:
        self.events: list[StageEvent] = []

    def record(self, event: StageEvent) -> None:
        self.events.append(event)


def test_run_evaluation_reports_synthetic_stage_counts_without_live_calls() -> None:
    report = run_evaluation()
    assert report.live_provider_calls == 0
    assert report.synthetic_results is True
    assert len(report.fixtures) >= 11
    assert any("synthetic" in warning.lower() for warning in report.interpretation_warnings)

    serialized = report.model_dump_json()
    for forbidden in ("Authorization", "api_key", "headers", "provider_prompt"):
        assert forbidden not in serialized

    for fixture in report.fixtures:
        assert fixture.stage_counts
        assert fixture.classification.precision >= 0.0
        assert fixture.context.hidden_segments >= 0
        assert fixture.caps.max_highlights == 10


def test_current_limits_are_documented_with_generated_demonstrations() -> None:
    report = run_evaluation()
    demonstrations = {item.name: item for item in report.limit_demonstrations}
    assert demonstrations["llm_transcript_chars"].configured_limit == 24_000
    assert demonstrations["llm_segments"].configured_limit == 2_000
    assert demonstrations["llm_candidates"].configured_limit == 12
    assert demonstrations["final_highlights"].configured_limit == 10
    assert all(item.observed_value > item.configured_limit for item in demonstrations.values())


def test_single_pass_vs_multipass_reports_late_coverage() -> None:
    report = run_evaluation()
    late = next(item for item in report.single_vs_multipass.per_fixture if "late" in item.fixture_id)
    assert late.multipass_recall >= late.single_pass_recall
    assert late.multipass_hidden_segments == 0
    assert late.multipass_candidates_discovered >= late.single_pass_candidates_to_llm


def test_truncation_fixture_demonstrates_hidden_context_and_multipass_gain() -> None:
    report = run_evaluation()
    stress = next(item for item in report.fixtures if "context_truncation" in item.edge_cases)
    assert stress.context.hidden_chars > 0
    assert stress.context.hidden_segments > 0
    assert report.aggregate.hidden_chars > 0
    assert report.aggregate.hidden_segments > 0
    comparison = next(
        item
        for item in report.single_vs_multipass.per_fixture
        if item.fixture_id == stress.fixture_id
    )
    assert comparison.single_pass_recall < comparison.multipass_recall


def test_reports_are_byte_identical_across_runs(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_json, first_md = write_reports(run_evaluation(), first_dir)
    second_json, second_md = write_reports(run_evaluation(), second_dir)
    assert first_json.read_bytes() == second_json.read_bytes()
    assert first_md.read_bytes() == second_md.read_bytes()


def test_markdown_report_contains_required_sections(tmp_path: Path) -> None:
    _, markdown = write_reports(run_evaluation(), tmp_path)
    text = markdown.read_text(encoding="utf-8")
    required = [
        "Dataset and fixture coverage",
        "Metric definitions and limitations",
        "Per-content-type results",
        "Aggregate results",
        "Temporal coverage",
        "Candidate survival",
        "Context truncation",
        "Single-pass versus simulated multi-pass",
        "Recommended window configuration",
        "Known limitations",
    ]
    assert all(section in text for section in required)


def test_default_production_limits_remain_unchanged() -> None:
    config = HighlightSettings()
    assert config.llm_max_transcript_chars == 24_000
    assert config.llm_max_candidates == 12
    assert config.max_highlights == 10


def test_observer_seam_does_not_change_production_behavior() -> None:
    corpus = load_corpus_for_test()
    fixture = corpus.fixtures[0]
    config = HighlightSettings()
    kwargs = {
        "job_id": "observer-seam",
        "media_path": Path("unused.mp4"),
        "metadata": fixture.metadata(),
        "transcript": fixture.transcript(),
        "config": config,
        "content_type": fixture.content_type,
        "signals": fixture.signals(),
    }
    baseline = detect_highlights(**kwargs)
    observer = CollectingObserver()
    observed = detect_highlights(**kwargs, observer=observer)
    assert observed == baseline
    assert observer.events
    transcript_event = next(event for event in observer.events if event.stage == "transcript_candidates")
    signal_event = next(event for event in observer.events if event.stage == "signal_candidates")
    assert signal_event.before == transcript_event.after
    assert signal_event.after >= signal_event.before


def test_preparation_observer_does_not_change_candidates() -> None:
    corpus = load_corpus_for_test()
    fixture = corpus.fixtures[1]
    kwargs = {
        "job_id": "prep-seam",
        "media_path": Path("unused.mp4"),
        "metadata": fixture.metadata(),
        "transcript": fixture.transcript(),
        "config": HighlightSettings(),
        "content_type": fixture.content_type,
        "signals": fixture.signals(),
    }
    class _NoCallManager:
        def analyze(self, task: str, **kwargs: object) -> object:
            raise AssertionError(f"unexpected provider call: {task}")

    kwargs["manager"] = _NoCallManager()
    baseline = prepare_highlights(**kwargs)
    observer = CollectingObserver()
    observed = prepare_highlights(**kwargs, observer=observer)
    assert observed.candidates == baseline.candidates
    assert observed.notes == baseline.notes
    assert any(event.stage == "llm_candidate_cap" for event in observer.events)


def load_corpus_for_test():
    from clipping_ai.evaluation.fixtures import load_corpus

    return load_corpus()


def test_build_report_is_deterministic_for_same_inputs() -> None:
    assert build_report(run_evaluation().fixtures) == build_report(run_evaluation().fixtures)


@pytest.mark.parametrize("payload", [None, {}, {"highlights": None}])
def test_malformed_evaluation_payloads_do_not_crash_metrics(payload: object) -> None:
    from clipping_ai.evaluation.metrics import classification_metrics

    metrics = classification_metrics(payload or [], [])
    assert metrics.invalid_prediction_count >= 0


def test_highlight_result_model_still_validates() -> None:
    result = HighlightResult(job_id="x", content_type=ContentType.GENERAL, duration=1.0)
    assert json.loads(result.model_dump_json())["method"] == "deterministic"
