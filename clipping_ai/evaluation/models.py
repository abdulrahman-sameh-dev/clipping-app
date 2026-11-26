"""Evaluation-only data models for offline highlight discovery baselines."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from clipping_ai.highlights.models import ContentType, MediaSignals
from clipping_ai.highlights.observability import StageEvent
from clipping_ai.schemas import TranscriptResult, TranscriptSegment

__all__ = ["StageEvent"]

if TYPE_CHECKING:
    from clipping_ai.video.models import VideoMetadata


class StrictEvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Span(StrictEvaluationModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    topic: str | None = None
    label: str | None = None

    @model_validator(mode="after")
    def _valid_interval(self) -> Span:
        if self.end <= self.start:
            raise ValueError("span end must be greater than start")
        return self


class EvaluationFixture(StrictEvaluationModel):
    fixture_id: str = Field(min_length=1, pattern=r"^fixture-[a-z0-9-]+$")
    content_type: ContentType
    language: str
    duration: float = Field(gt=0)
    segments: list[TranscriptSegment]
    gold_spans: list[Span]
    topic_labels: list[str] = Field(default_factory=list)
    edge_cases: list[str]
    annotation_notes: list[str]
    canned_response_mode: Literal["top_visible", "empty"] = "top_visible"

    @model_validator(mode="after")
    def _within_duration(self) -> EvaluationFixture:
        for segment in self.segments:
            if segment.end > self.duration:
                raise ValueError("segment end exceeds fixture duration")
        for span in self.gold_spans:
            if span.end > self.duration:
                raise ValueError("gold span end exceeds fixture duration")
        return self

    def transcript(self) -> TranscriptResult:
        return TranscriptResult(
            text=" ".join(segment.text for segment in self.segments),
            language=self.language,
            duration=self.duration,
            segments=self.segments,
        )

    def metadata(self) -> VideoMetadata:
        from clipping_ai.video.models import VideoMetadata

        return VideoMetadata(
            duration_seconds=self.duration,
            size_bytes=1,
            has_video=True,
            has_audio=True,
        )

    def signals(self) -> MediaSignals:
        if "no_signals" in self.edge_cases:
            return MediaSignals.model_validate(
                {
                    "duration": self.duration,
                    "audio_windows": [],
                    "motion_samples": [],
                    "audio_analyzed": False,
                    "video_analyzed": False,
                    "motion_analyzed": False,
                }
            )
        quiet = "quiet_low_motion" in self.edge_cases
        step = 30.0 if self.duration > 1800 else 10.0
        windows = []
        start = 0.0
        while start < self.duration:
            end = min(start + step, self.duration)
            windows.append(
                {
                    "start": start,
                    "end": end,
                    "rms_dbfs": -70.0 if quiet else -30.0,
                    "peak_dbfs": -60.0 if quiet else -12.0,
                    "energy": 0.01 if quiet else 0.55,
                    "silence": quiet,
                }
            )
            start = end
        motion_samples = [] if quiet else [
            {"time": float(index), "score": 0.2} for index in range(0, int(self.duration), 30)
        ]
        return MediaSignals.model_validate(
            {
                "duration": self.duration,
                "audio_windows": windows,
                "motion_samples": motion_samples,
                "audio_analyzed": True,
                "video_analyzed": True,
                "motion_analyzed": not quiet,
            }
        )


class EvaluationCorpus(StrictEvaluationModel):
    version: int
    labels_are_synthetic: bool = True
    interpretation_warning: str
    annotation_rules: list[str]
    fixtures: list[EvaluationFixture]

    @model_validator(mode="after")
    def _unique_ids(self) -> EvaluationCorpus:
        ids = [fixture.fixture_id for fixture in self.fixtures]
        if len(ids) != len(set(ids)):
            raise ValueError("fixture ids must be unique")
        return self


class SpanMatch(StrictEvaluationModel):
    prediction_index: int
    gold_index: int
    iou: float
    intersection: float
    start_error: float
    end_error: float


class MatchResult(StrictEvaluationModel):
    matches: list[SpanMatch]
    unmatched_prediction_indices: list[int]
    unmatched_gold_indices: list[int]
    valid_prediction_count: int
    valid_gold_count: int
    invalid_prediction_count: int
    invalid_gold_count: int


class ClassificationMetrics(StrictEvaluationModel):
    precision: float
    recall: float
    f1: float
    true_positives: int
    predicted_count: int
    gold_count: int
    invalid_prediction_count: int
    invalid_gold_count: int


class BoundaryMetrics(StrictEvaluationModel):
    matched_count: int
    mean_start_error: float
    mean_end_error: float
    max_start_error: float
    max_end_error: float


class QuartileStat(StrictEvaluationModel):
    quartile: int
    start: float
    end: float
    gold_seconds: float = 0.0
    selected_seconds: float = 0.0
    gold_count: int = 0
    selected_count: int = 0
    coverage_ratio: float = 0.0


class TemporalCoverage(StrictEvaluationModel):
    bins: list[QuartileStat]
    midpoint_counts: list[int]
    covered_quartiles: int


class OverlapPair(StrictEvaluationModel):
    left_index: int
    right_index: int
    iou: float
    overlap_seconds: float


class RedundancyMetrics(StrictEvaluationModel):
    pairwise_comparisons: int
    overlap_count: int
    mean_iou: float
    max_iou: float
    overlapping_pairs: list[OverlapPair]


class TopicCoverage(StrictEvaluationModel):
    topics: dict[str, bool]
    covered_topics: int
    total_topics: int
    coverage_ratio: float


class StageCount(StrictEvaluationModel):
    stage: str
    before: int
    after: int
    removed: int
    details: dict[str, Any] = Field(default_factory=dict)


class StageSurvival(StrictEvaluationModel):
    stage: str
    before: int
    after: int
    stage_survival_ratio: float
    cumulative_survival_ratio: float


class ContextStatistics(StrictEvaluationModel):
    transcript_chars: int
    visible_chars: int
    hidden_chars: int
    text_truncated: bool
    transcript_segments: int
    visible_segments: int
    hidden_segments: int
    segments_truncated: bool
    transcript_duration: float
    visible_segment_end: float
    hidden_seconds: float
    segment_coverage_seconds: float
    segment_coverage_ratio: float


class CapStatistics(StrictEvaluationModel):
    candidates_before_llm_cap: int
    candidates_to_llm: int
    llm_candidate_limit: int
    llm_candidates_hidden: int
    final_candidates_before_cap: int
    final_candidates: int
    max_highlights: int
    final_candidates_hidden: int
    fallback_used: bool


class FallbackStatistics(StrictEvaluationModel):
    evaluated_calls: int
    fallback_calls: int
    fallback_rate: float


class WindowPlan(StrictEvaluationModel):
    window_id: str
    index: int
    start: float
    end: float


class SegmentWindowAssignment(StrictEvaluationModel):
    window: WindowPlan
    segment: TranscriptSegment


class WindowRecord(StrictEvaluationModel):
    window_id: str
    index: int
    start: float
    end: float
    status: Literal[
        "complete",
        "skipped_complete",
        "resumed",
        "pending",
        "rejected",
    ]
    attempts: int
    segment_ids: list[int]
    candidates_before_cap: int
    candidates_to_llm: int
    candidates_after_window: int
    error: str | None = None


class MultiPassSummary(StrictEvaluationModel):
    window_seconds: float
    overlap_seconds: float
    windows_total: int
    windows_processed: int
    windows_skipped: int
    windows_resumed: int
    windows_rejected: int
    segments_assigned: int
    segments_unassigned: int
    candidates_discovered_before_cap: int
    candidates_to_llm_total: int
    candidates_after_global_dedupe: int
    final_highlights: int
    selected_spans: list[Span]
    classification: ClassificationMetrics
    redundancy: RedundancyMetrics
    records: list[WindowRecord]


class FixtureResult(StrictEvaluationModel):
    fixture_id: str
    content_type: ContentType
    language: str
    duration: float
    edge_cases: list[str]
    gold_spans: list[Span]
    selected_spans: list[Span]
    classification: ClassificationMetrics
    boundary: BoundaryMetrics
    temporal_coverage: TemporalCoverage
    redundancy: RedundancyMetrics
    topic_coverage: TopicCoverage
    context: ContextStatistics
    caps: CapStatistics
    stage_counts: list[StageCount]
    candidate_survival: list[StageSurvival]
    method: str
    notes: list[str]
    canned_llm_mode: str
    fallback_used: bool
    multipass: MultiPassSummary | None = None


class AggregateMetrics(StrictEvaluationModel):
    fixture_count: int
    precision: float
    recall: float
    f1: float
    mean_start_error: float
    mean_end_error: float
    mean_redundancy_iou: float
    topic_coverage_ratio: float
    covered_quartiles: int
    total_quartiles: int
    hidden_chars: int
    hidden_segments: int
    hidden_seconds: float
    llm_candidates_hidden: int
    final_candidates_hidden: int
    fallback_calls: int


class LimitDemonstration(StrictEvaluationModel):
    name: str
    configured_limit: int
    observed_value: int
    evidence: str
    interpretation: str


class SingleVsMultiPassFixture(StrictEvaluationModel):
    fixture_id: str
    single_pass_recall: float
    multipass_recall: float
    single_pass_f1: float
    multipass_f1: float
    single_pass_candidates_to_llm: int
    multipass_candidates_discovered: int
    multipass_hidden_segments: int


class AggregateComparison(StrictEvaluationModel):
    single_pass_recall: float
    multipass_recall: float
    single_pass_f1: float
    multipass_f1: float
    recall_delta: float
    f1_delta: float
    multipass_hidden_segments: int


class SingleVsMultiPassReport(StrictEvaluationModel):
    per_fixture: list[SingleVsMultiPassFixture]
    aggregate: AggregateComparison


class WindowExperiment(StrictEvaluationModel):
    window_seconds: float
    overlap_seconds: float
    f1: float
    recall: float
    covered_quartiles: int
    redundancy_iou: float
    windows_total: int
    candidates_discovered: int
    score: float


class RecommendedWindow(StrictEvaluationModel):
    window_seconds: float
    overlap_seconds: float
    rationale: str


class WindowExperimentSet(StrictEvaluationModel):
    experiments: list[WindowExperiment]
    recommended: RecommendedWindow


class EvaluationReport(StrictEvaluationModel):
    report_version: int
    corpus_version: int
    synthetic_results: bool
    live_provider_calls: int
    fixtures: list[FixtureResult]
    aggregate: AggregateMetrics
    aggregate_by_content_type: dict[str, AggregateMetrics]
    candidate_survival_aggregate: list[StageSurvival]
    temporal_coverage_aggregate: list[QuartileStat]
    fallback: FallbackStatistics
    limit_demonstrations: list[LimitDemonstration]
    single_vs_multipass: SingleVsMultiPassReport
    window_experiments: WindowExperimentSet
    metric_definitions: dict[str, str]
    interpretation_warnings: list[str]
    known_limitations: list[str]
    multi_pass_design: list[str]
    future_implementation_breakdown: list[str]
