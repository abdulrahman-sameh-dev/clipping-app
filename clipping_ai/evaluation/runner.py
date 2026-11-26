"""Deterministic offline evaluation runner for highlight discovery."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from clipping_ai.analysis.config import HighlightSettings
from clipping_ai.evaluation.fixtures import load_corpus
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
)
from clipping_ai.evaluation.models import (
    AggregateComparison,
    AggregateMetrics,
    EvaluationReport,
    FixtureResult,
    LimitDemonstration,
    QuartileStat,
    SingleVsMultiPassFixture,
    SingleVsMultiPassReport,
    Span,
    StageCount,
    StageEvent,
    WindowExperimentSet,
)
from clipping_ai.evaluation.multipass import (
    WindowConfig,
    run_window_experiments,
    run_window_simulation,
)
from clipping_ai.highlights.engine import detect_highlights, prepare_highlights
from clipping_ai.highlights.llm import parse_llm_response
from clipping_ai.highlights.models import LlmOutcome
from clipping_ai.schemas import TranscriptResult

if TYPE_CHECKING:
    from clipping_ai.manager import ProviderManager

REPORT_VERSION = 1
DEFAULT_OUTPUT_DIR = Path("evaluation-output")


class _StageCollector:
    def __init__(self) -> None:
        self.events: list[StageEvent] = []

    def record(self, event: StageEvent) -> None:
        self.events.append(event)


class _CannedManager:
    def analyze(self, task: str, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(f"offline evaluation must not call provider task {task}")


def _event_counts(events: Sequence[StageEvent]) -> list[StageCount]:
    return [
        StageCount(
            stage=event.stage,
            before=event.before,
            after=event.after,
            removed=max(0, event.before - event.after),
            details=event.details,
        )
        for event in events
    ]


def _stage_value(counts: Sequence[StageCount], stage: str, field: str) -> int:
    for count in counts:
        if count.stage == stage:
            return int(getattr(count, field))
    return 0


def _canned_outcome(
    *,
    fixture: Any,
    candidates: Sequence[Any],
    transcript: TranscriptResult,
    config: HighlightSettings,
) -> LlmOutcome:
    visible_segments = list(transcript.segments[:2000])
    visible_ids = {segment.id for segment in visible_segments if segment.id is not None}
    visible_end = max((segment.end for segment in visible_segments), default=0.0)
    visible: list[Any] = []
    for candidate in candidates[: config.llm_max_candidates]:
        if candidate.transcript_segments:
            if any(
                segment.id is not None and segment.id in visible_ids
                for segment in candidate.transcript_segments
            ):
                visible.append(candidate)
        elif candidate.start <= visible_end:
            visible.append(candidate)
    if fixture.canned_response_mode == "empty":
        response_data: dict[str, Any] = {"highlights": []}
    else:
        raw_highlights = []
        for candidate in visible:
            excerpt = " ".join(segment.text.strip() for segment in candidate.transcript_segments)
            raw_highlights.append(
                {
                    "start_time": candidate.start,
                    "end_time": candidate.end,
                    "suggested_title": candidate.reason[:90] or "Candidate moment",
                    "summary": excerpt[:300] or candidate.category,
                    "reasoning": candidate.reason or "Canned offline candidate selection.",
                    "score": round(min(100.0, candidate.score * 100), 4),
                    "category": candidate.category,
                    "transcript_excerpt": excerpt[:300],
                }
            )
        response_data = {"highlights": raw_highlights}
    parsed_highlights, error = parse_llm_response(
        response_data,
        transcript=transcript,
        candidates=visible,
        signals=None,
        config=config,
    )
    return LlmOutcome(
        highlights=parsed_highlights,
        error=error,
        provider="canned",
        model="offline-canned",
        applied=True,
        failed=error is not None,
        retryable=error is not None,
    )


def _spans_from_highlights(highlights: Sequence[Any]) -> list[Span]:
    return [
        Span(start=float(item.start_time), end=float(item.end_time))
        for item in highlights
        if item.end_time > item.start_time
    ]


def run_fixture(
    fixture: Any,
    *,
    config: HighlightSettings,
    window_config: WindowConfig,
) -> FixtureResult:
    collector = _StageCollector()
    manager = cast("ProviderManager", _CannedManager())
    transcript = fixture.transcript()
    metadata = fixture.metadata()
    signals = fixture.signals()

    preparation = prepare_highlights(
        job_id=fixture.fixture_id,
        media_path=Path("unused.mp4"),
        metadata=metadata,
        transcript=transcript,
        config=config,
        content_type=fixture.content_type,
        manager=manager,
        signals=signals,
        observer=collector,
    )
    outcome = _canned_outcome(
        fixture=fixture,
        candidates=preparation.candidates,
        transcript=transcript,
        config=config,
    )
    result = detect_highlights(
        job_id=fixture.fixture_id,
        media_path=Path("unused.mp4"),
        metadata=metadata,
        transcript=transcript,
        config=config,
        content_type=fixture.content_type,
        manager=manager,
        signals=signals,
        preparation=preparation,
        llm_outcome=outcome,
        observer=collector,
    )

    counts = _event_counts(collector.events)
    selected_spans = _spans_from_highlights(result.highlights)
    matches = match_spans(selected_spans, fixture.gold_spans)
    simulation = run_window_simulation(
        fixture,
        highlight_config=config,
        window_config=window_config,
    )
    fallback_used = bool(outcome.failed or not outcome.applied)
    cap_stats = cap_statistics(
        candidates_before_llm_cap=_stage_value(counts, "llm_candidate_cap", "before")
        or len(preparation.candidates),
        candidates_to_llm=len(preparation.candidates),
        final_candidates_before_cap=_stage_value(counts, "final_highlight_cap", "before")
        or len(result.highlights),
        final_candidates=len(result.highlights),
        config=config,
        fallback_used=fallback_used,
    )
    return FixtureResult(
        fixture_id=fixture.fixture_id,
        content_type=fixture.content_type,
        language=fixture.language,
        duration=fixture.duration,
        edge_cases=list(fixture.edge_cases),
        gold_spans=list(fixture.gold_spans),
        selected_spans=selected_spans,
        classification=classification_metrics(selected_spans, fixture.gold_spans),
        boundary=boundary_metrics(matches),
        temporal_coverage=quartile_coverage(
            selected_spans,
            fixture.gold_spans,
            duration=fixture.duration,
        ),
        redundancy=redundancy_metrics(selected_spans),
        topic_coverage=distinct_topic_coverage(fixture.gold_spans, selected_spans),
        context=context_statistics(transcript, config),
        caps=cap_stats,
        stage_counts=counts,
        candidate_survival=candidate_survival(counts),
        method=result.method,
        notes=list(result.notes),
        canned_llm_mode=fixture.canned_response_mode,
        fallback_used=fallback_used,
        multipass=simulation,
    )


def _aggregate(results: Sequence[FixtureResult]) -> AggregateMetrics:
    count = len(results)
    if not count:
        return AggregateMetrics(
            fixture_count=0,
            precision=0.0,
            recall=0.0,
            f1=0.0,
            mean_start_error=0.0,
            mean_end_error=0.0,
            mean_redundancy_iou=0.0,
            topic_coverage_ratio=0.0,
            covered_quartiles=0,
            total_quartiles=0,
            hidden_chars=0,
            hidden_segments=0,
            hidden_seconds=0.0,
            llm_candidates_hidden=0,
            final_candidates_hidden=0,
            fallback_calls=0,
        )

    def mean(values: Sequence[float]) -> float:
        return round(sum(values) / count, 6)

    return AggregateMetrics(
        fixture_count=count,
        precision=mean([item.classification.precision for item in results]),
        recall=mean([item.classification.recall for item in results]),
        f1=mean([item.classification.f1 for item in results]),
        mean_start_error=mean([item.boundary.mean_start_error for item in results]),
        mean_end_error=mean([item.boundary.mean_end_error for item in results]),
        mean_redundancy_iou=mean([item.redundancy.mean_iou for item in results]),
        topic_coverage_ratio=mean([item.topic_coverage.coverage_ratio for item in results]),
        covered_quartiles=sum(
            item.temporal_coverage.covered_quartiles for item in results
        ),
        total_quartiles=count * 4,
        hidden_chars=sum(item.context.hidden_chars for item in results),
        hidden_segments=sum(item.context.hidden_segments for item in results),
        hidden_seconds=round(sum(item.context.hidden_seconds for item in results), 6),
        llm_candidates_hidden=sum(item.caps.llm_candidates_hidden for item in results),
        final_candidates_hidden=sum(item.caps.final_candidates_hidden for item in results),
        fallback_calls=sum(1 for item in results if item.fallback_used),
    )


def _aggregate_stage_counts(results: Sequence[FixtureResult]) -> list[StageCount]:
    stages: dict[str, list[int]] = {}
    for result in results:
        for stage in result.stage_counts:
            bucket = stages.setdefault(stage.stage, [0, 0])
            bucket[0] += stage.before
            bucket[1] += stage.after
    return [
        StageCount(stage=name, before=before, after=after, removed=max(0, before - after))
        for name, (before, after) in stages.items()
    ]


def _aggregate_temporal(results: Sequence[FixtureResult]) -> list[QuartileStat]:
    bins: list[dict[str, float | int]] = []
    for result in results:
        for index, item in enumerate(result.temporal_coverage.bins):
            while len(bins) <= index:
                bins.append(
                    {
                        "quartile": index + 1,
                        "start": item.start,
                        "end": item.end,
                        "gold_seconds": 0.0,
                        "selected_seconds": 0.0,
                        "gold_count": 0,
                        "selected_count": 0,
                    }
                )
            bins[index]["gold_seconds"] = float(bins[index]["gold_seconds"]) + item.gold_seconds
            bins[index]["selected_seconds"] = (
                float(bins[index]["selected_seconds"]) + item.selected_seconds
            )
            bins[index]["gold_count"] = int(bins[index]["gold_count"]) + item.gold_count
            bins[index]["selected_count"] = int(bins[index]["selected_count"]) + item.selected_count
    output: list[QuartileStat] = []
    for bin_data in bins:
        gold = float(bin_data["gold_seconds"])
        selected = float(bin_data["selected_seconds"])
        output.append(
            QuartileStat(
                quartile=int(bin_data["quartile"]),
                start=float(bin_data["start"]),
                end=float(bin_data["end"]),
                gold_seconds=round(gold, 6),
                selected_seconds=round(selected, 6),
                gold_count=int(bin_data["gold_count"]),
                selected_count=int(bin_data["selected_count"]),
                coverage_ratio=round(min(1.0, selected / gold), 6) if gold > 0 else 0.0,
            )
        )
    return output


def _limit_demonstrations(results: Sequence[FixtureResult]) -> list[LimitDemonstration]:
    stress = next((item for item in results if "context_truncation" in item.edge_cases), None)
    chars = stress.context.transcript_chars if stress else 24_001
    hidden_chars = stress.context.hidden_chars if stress else 1
    segments = stress.context.transcript_segments if stress else 2_001
    hidden_segments = stress.context.hidden_segments if stress else 1
    candidates = max((item.caps.candidates_before_llm_cap for item in results), default=13)
    finalists = max((item.caps.final_candidates_before_cap for item in results), default=11)
    return [
        LimitDemonstration(
            name="llm_transcript_chars",
            configured_limit=24_000,
            observed_value=chars,
            evidence=f"A synthetic {chars} character transcript exposes {hidden_chars} hidden characters.",
            interpretation="Late context cannot be selected by the single LLM pass.",
        ),
        LimitDemonstration(
            name="llm_segments",
            configured_limit=2_000,
            observed_value=segments,
            evidence=f"A synthetic {segments} segment transcript exposes {hidden_segments} hidden segments.",
            interpretation="Segment visibility ends before the end of a long video.",
        ),
        LimitDemonstration(
            name="llm_candidates",
            configured_limit=12,
            observed_value=candidates,
            evidence=f"A fixture with {candidates} surviving candidates sends only 12 to the canned LLM.",
            interpretation="Candidate caps are global in the current single-pass path.",
        ),
        LimitDemonstration(
            name="final_highlights",
            configured_limit=10,
            observed_value=finalists,
            evidence=f"A fixture produces {finalists} candidates before the final highlight cap.",
            interpretation="Output selection cannot express more than the configured cap.",
        ),
    ]


def _comparison(results: Sequence[FixtureResult]) -> SingleVsMultiPassReport:
    per_fixture = []
    for result in results:
        if result.multipass is None:
            continue
        per_fixture.append(
            SingleVsMultiPassFixture(
                fixture_id=result.fixture_id,
                single_pass_recall=result.classification.recall,
                multipass_recall=result.multipass.classification.recall,
                single_pass_f1=result.classification.f1,
                multipass_f1=result.multipass.classification.f1,
                single_pass_candidates_to_llm=result.caps.candidates_to_llm,
                multipass_candidates_discovered=(
                    result.multipass.candidates_discovered_before_cap
                ),
                multipass_hidden_segments=result.context.hidden_segments,
            )
        )
    count = len(per_fixture)
    if not count:
        aggregate = AggregateComparison(
            single_pass_recall=0.0,
            multipass_recall=0.0,
            single_pass_f1=0.0,
            multipass_f1=0.0,
            recall_delta=0.0,
            f1_delta=0.0,
            multipass_hidden_segments=0,
        )
    else:
        single_recall = sum(item.single_pass_recall for item in per_fixture) / count
        multi_recall = sum(item.multipass_recall for item in per_fixture) / count
        single_f1 = sum(item.single_pass_f1 for item in per_fixture) / count
        multi_f1 = sum(item.multipass_f1 for item in per_fixture) / count
        aggregate = AggregateComparison(
            single_pass_recall=round(single_recall, 6),
            multipass_recall=round(multi_recall, 6),
            single_pass_f1=round(single_f1, 6),
            multipass_f1=round(multi_f1, 6),
            recall_delta=round(multi_recall - single_recall, 6),
            f1_delta=round(multi_f1 - single_f1, 6),
            multipass_hidden_segments=sum(
                item.multipass_hidden_segments for item in per_fixture
            ),
        )
    return SingleVsMultiPassReport(per_fixture=per_fixture, aggregate=aggregate)


def _metric_definitions() -> dict[str, str]:
    return {
        "span_iou": "Intersection divided by union of two absolute-time spans; invalid intervals score zero.",
        "precision": "Matched predictions divided by valid predictions; 1.0 when both prediction and gold sets are empty.",
        "recall": "Matched gold spans divided by valid gold spans; 1.0 when gold is empty.",
        "f1": "Harmonic mean of precision and recall.",
        "boundary_error": "Absolute start and end timestamp differences for greedily matched spans.",
        "quartile_coverage": "Selected span seconds intersecting each quartile divided by gold seconds in that quartile, capped at 1.0.",
        "temporal_distribution": "Counts and seconds of selected spans by midpoint quartile.",
        "redundancy": "Pairwise IoU among selected spans, including count of pairs with positive overlap.",
        "distinct_topic_coverage": "Fraction of authored gold topics with at least one matched selected span.",
        "candidate_survival": "Stage after-count divided by stage before-count and by the largest stage before-count.",
        "context_truncation": "Difference between full transcript text/segments and the first 24,000 characters or first 2,000 segments.",
        "cap_statistics": "Candidates hidden by the 12-candidate LLM cap and highlights hidden by the max_highlights cap.",
        "fallback_frequency": "Fraction of canned LLM calls that produced no usable highlights and used deterministic fallback.",
    }


def _warnings() -> list[str]:
    return [
        "SYNTHETIC EVALUATION ONLY: results are not real-world performance measurements.",
        "Gold spans are authored annotations for controlled scenarios, not objective human judgments.",
        "Canned LLM responses simulate selection from visible candidates; no live provider was called.",
        "Topic labels are diagnostics and were not supplied to production ranking.",
        "Sparse transcripts lower segment-coverage ratios by design and do not measure ASR quality.",
    ]


def _limitations() -> list[str]:
    return [
        "The corpus is small and cannot estimate production quality across real content distributions.",
        "Boundary errors are measured only for matched spans; unmatched moments are not localized.",
        "Greedy IoU matching can prefer a slightly better overlapping pair over a globally optimal assignment.",
        "Religious fixtures test context-preservation structure only; no religious profile is implemented.",
        "Simulated multi-pass discovery reuses deterministic candidates and does not exercise a real LLM.",
    ]


def _multi_pass_design() -> list[str]:
    return [
        "Partition the complete transcript into sequential windows with configurable overlap.",
        "Preserve absolute timestamps for every segment and candidate; never rebase window-local times.",
        "Analyze each window independently with bounded text, segment, and candidate input.",
        "Persist window status, attempts, fingerprint, and candidate artifact in the existing StageStore.",
        "Resume from the first incomplete or invalid window while retaining completed window results.",
        "Aggregate all successful window candidates before global deduplication and semantic consolidation.",
        "Apply final ranking, diversity selection, and max_highlights only after full-video discovery.",
        "Treat 24,000 characters and 2,000 segments as per-window limits, not global discovery limits.",
    ]


def _future_breakdown() -> list[str]:
    return [
        "Define durable window stage fingerprints and deterministic window IDs.",
        "Add a single-pass-compatible multi-pass runner behind an explicit opt-in setting.",
        "Persist per-window artifact metadata through StageStore without a second job system.",
        "Add global aggregation, cross-window deduplication, and diversity selection.",
        "Add provider-budget accounting and context-error fallback for window-level requests.",
        "Verify parity for existing single-pass users and progressive rollout behavior.",
    ]


def build_report(
    fixture_results: Sequence[FixtureResult],
    window_experiments: WindowExperimentSet | None = None,
) -> EvaluationReport:
    corpus = load_corpus()
    experiments = window_experiments or run_window_experiments(corpus.fixtures)
    grouped: dict[str, list[FixtureResult]] = {}
    for result in fixture_results:
        grouped.setdefault(result.content_type.value, []).append(result)
    return EvaluationReport(
        report_version=REPORT_VERSION,
        corpus_version=corpus.version,
        synthetic_results=True,
        live_provider_calls=0,
        fixtures=list(fixture_results),
        aggregate=_aggregate(fixture_results),
        aggregate_by_content_type={
            name: _aggregate(items) for name, items in sorted(grouped.items())
        },
        candidate_survival_aggregate=candidate_survival(
            _aggregate_stage_counts(fixture_results)
        ),
        temporal_coverage_aggregate=_aggregate_temporal(fixture_results),
        fallback=fallback_statistics([item.fallback_used for item in fixture_results]),
        limit_demonstrations=_limit_demonstrations(fixture_results),
        single_vs_multipass=_comparison(fixture_results),
        window_experiments=experiments,
        metric_definitions=_metric_definitions(),
        interpretation_warnings=_warnings(),
        known_limitations=_limitations(),
        multi_pass_design=_multi_pass_design(),
        future_implementation_breakdown=_future_breakdown(),
    )


def run_evaluation(config: HighlightSettings | None = None) -> EvaluationReport:
    settings = config or HighlightSettings()
    corpus = load_corpus()
    experiments = run_window_experiments(corpus.fixtures)
    recommended = experiments.recommended
    window_config = WindowConfig(
        window_seconds=recommended.window_seconds,
        overlap_seconds=recommended.overlap_seconds,
    )
    results = [
        run_fixture(fixture, config=settings, window_config=window_config)
        for fixture in corpus.fixtures
    ]
    return build_report(results, experiments)


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    header = "| " + " | ".join(headers) + " |"
    divider = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(str(value) for value in row) + " |" for row in rows]
    return "\n".join([header, divider, *body])


def render_markdown(report: EvaluationReport) -> str:
    fixture_rows = [
        [
            result.fixture_id,
            result.content_type.value,
            f"{result.classification.precision:.3f}",
            f"{result.classification.recall:.3f}",
            f"{result.classification.f1:.3f}",
            result.context.hidden_segments,
            result.caps.llm_candidates_hidden,
            result.caps.final_candidates_hidden,
        ]
        for result in report.fixtures
    ]
    content_rows = [
        [
            name,
            metrics.fixture_count,
            f"{metrics.precision:.3f}",
            f"{metrics.recall:.3f}",
            f"{metrics.f1:.3f}",
            f"{metrics.mean_redundancy_iou:.3f}",
        ]
        for name, metrics in report.aggregate_by_content_type.items()
    ]
    quartile_rows = [
        [
            item.quartile,
            f"{item.gold_seconds:.1f}",
            f"{item.selected_seconds:.1f}",
            f"{item.coverage_ratio:.3f}",
        ]
        for item in report.temporal_coverage_aggregate
    ]
    survival_rows = [
        [item.stage, item.before, item.after, f"{item.cumulative_survival_ratio:.3f}"]
        for item in report.candidate_survival_aggregate
    ]
    limit_rows = [
        [item.name, item.configured_limit, item.observed_value, item.interpretation]
        for item in report.limit_demonstrations
    ]
    multipass_rows = [
        [
            item.fixture_id,
            f"{item.single_pass_recall:.3f}",
            f"{item.multipass_recall:.3f}",
            item.single_pass_candidates_to_llm,
            item.multipass_candidates_discovered,
        ]
        for item in report.single_vs_multipass.per_fixture
    ]
    experiment_rows = [
        [
            item.window_seconds,
            item.overlap_seconds,
            f"{item.f1:.3f}",
            f"{item.recall:.3f}",
            f"{item.redundancy_iou:.3f}",
            item.windows_total,
            f"{item.score:.6f}",
        ]
        for item in report.window_experiments.experiments
    ]
    lines = [
        "# Offline Highlight Discovery Baseline",
        "",
        "SYNTHETIC EVALUATION RESULTS ONLY. These numbers are not real-world performance.",
        "",
        "## 1. Dataset and fixture coverage",
        f"Corpus version: {report.corpus_version}. Fixtures: {len(report.fixtures)}.",
        "Fixtures are synthetic or manually authored; no private videos or copyrighted transcripts were used.",
        "",
        "## 2. Metric definitions and limitations",
        *[f"- **{name}**: {definition}" for name, definition in report.metric_definitions.items()],
        "",
        "## 3. Per-content-type results",
        _markdown_table(
            ["content_type", "fixtures", "precision", "recall", "f1", "redundancy"],
            content_rows,
        ),
        "",
        "## 4. Aggregate results",
        f"Precision {report.aggregate.precision:.3f}; recall {report.aggregate.recall:.3f}; "
        f"F1 {report.aggregate.f1:.3f}; topic coverage "
        f"{report.aggregate.topic_coverage_ratio:.3f}.",
        f"Fallback calls: {report.fallback.fallback_calls}/{report.fallback.evaluated_calls}.",
        "",
        "## 5. Temporal coverage",
        _markdown_table(
            ["quartile", "gold_seconds", "selected_seconds", "coverage_ratio"],
            quartile_rows,
        ),
        "",
        "### Per-fixture metrics",
        _markdown_table(
            ["fixture", "type", "precision", "recall", "f1", "hidden_segments", "llm_hidden", "final_hidden"],
            fixture_rows,
        ),
        "",
        "## 6. Candidate survival",
        _markdown_table(
            ["stage", "before", "after", "cumulative_survival"],
            survival_rows,
        ),
        "",
        "## 7. Context truncation",
        _markdown_table(
            ["limit", "configured", "observed", "interpretation"],
            limit_rows,
        ),
        f"Aggregate hidden text characters: {report.aggregate.hidden_chars}. "
        f"Aggregate hidden segments: {report.aggregate.hidden_segments}.",
        "",
        "## 8. Single-pass versus simulated multi-pass",
        _markdown_table(
            ["fixture", "single_recall", "multipass_recall", "single_llm_candidates", "multipass_discovered"],
            multipass_rows,
        ),
        f"Aggregate recall delta: {report.single_vs_multipass.aggregate.recall_delta:.3f}.",
        "",
        "## 9. Recommended window configuration",
        f"Window seconds: {report.window_experiments.recommended.window_seconds}. "
        f"Overlap seconds: {report.window_experiments.recommended.overlap_seconds}.",
        report.window_experiments.recommended.rationale,
        _markdown_table(
            ["window_seconds", "overlap", "f1", "recall", "redundancy", "windows", "score"],
            experiment_rows,
        ),
        "",
        "## 10. Known limitations",
        *[f"- {item}" for item in report.known_limitations],
        "",
        "## Interpretation warnings",
        *[f"- {item}" for item in report.interpretation_warnings],
        "",
        "## Proposed multi-pass architecture",
        *[f"- {item}" for item in report.multi_pass_design],
        "",
        "## Future implementation breakdown",
        *[f"- {item}" for item in report.future_implementation_breakdown],
        "",
    ]
    return "\n".join(lines)


def write_reports(report: EvaluationReport, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "highlight-baseline.json"
    markdown_path = output_dir / "highlight-baseline.md"
    payload = json.dumps(
        report.model_dump(mode="json"),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    json_path.write_text(payload + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path
