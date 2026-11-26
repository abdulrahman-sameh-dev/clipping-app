"""Durable checkpoints for post-transcription analysis stages."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from clipping_ai.analysis.config import HighlightSettings, MultiPassSettings
from clipping_ai.analysis.transcription import stage_retryable
from clipping_ai.audio import hash_file
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.highlights.candidates import Candidate
from clipping_ai.highlights.engine import prepare_highlights
from clipping_ai.highlights.llm import refine_with_llm_outcome
from clipping_ai.highlights.models import (
    AudioWindow,
    ContentType,
    HighlightResult,
    LlmOutcome,
    MediaClassification,
    MediaSignals,
    MotionSample,
    SceneChange,
)
from clipping_ai.highlights.multipass import (
    DiscoveryWindow,
    aggregate_window_artifacts,
    plan_discovery_windows,
    window_signals,
    window_transcript,
)
from clipping_ai.highlights.observability import DiscoveryObserver, StageEvent
from clipping_ai.highlights.signals import (
    analyze_audio_signals,
    analyze_motion,
    analyze_scene_changes,
)
from clipping_ai.recovery.idempotency import stable_fingerprint
from clipping_ai.recovery.stages import (
    StageError,
    StageOwnershipError,
    StageRecord,
    StageStatus,
    StageStore,
)
from clipping_ai.schemas import TranscriptResult, TranscriptSegment
from clipping_ai.video.models import VideoMetadata

if TYPE_CHECKING:
    from clipping_ai.manager import ProviderManager
    from clipping_ai.settings import Settings

ANALYSIS_JOB_TYPE = "analysis"
STAGE_NORMALIZE = "normalize"
STAGE_SIGNALS_AUDIO = "signals.audio"
STAGE_SIGNALS_SCENE = "signals.scene"
STAGE_SIGNALS_MOTION = "signals.motion"
STAGE_CANDIDATES = "candidates"
STAGE_LLM = "llm"
STAGE_ASSEMBLE = "assemble"
STAGE_DISCOVERY_PLAN = "discovery.plan"
STAGE_DISCOVERY_WINDOW = "discovery.window"
STAGE_DISCOVERY_AGGREGATE = "discovery.aggregate"

ANALYSIS_STAGES = (
    STAGE_NORMALIZE,
    STAGE_SIGNALS_AUDIO,
    STAGE_SIGNALS_SCENE,
    STAGE_SIGNALS_MOTION,
    STAGE_CANDIDATES,
    STAGE_LLM,
    STAGE_ASSEMBLE,
    STAGE_DISCOVERY_PLAN,
    STAGE_DISCOVERY_AGGREGATE,
)

_ARTIFACT_VERSION = 1
_LEASE_SECONDS = 900.0
_HEARTBEAT_SECONDS = 60.0

T = TypeVar("T", bound=BaseModel)


def _emit_discovery(
    observer: DiscoveryObserver | None,
    stage: str,
    before: int,
    after: int,
    details: dict[str, Any] | None = None,
) -> None:
    if observer is not None:
        observer.record(
            StageEvent(stage=stage, before=before, after=after, details=details or {})
        )


class AudioSignalsArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    duration: float
    windows: list[AudioWindow] = Field(default_factory=list)
    note: str | None = None


class SceneSignalsArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    duration: float
    changes: list[SceneChange] = Field(default_factory=list)
    note: str | None = None


class MotionSignalsArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    duration: float
    samples: list[MotionSample] = Field(default_factory=list)
    note: str | None = None


class CandidatePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    start: float
    end: float
    score: float
    category: str
    reason: str
    transcript_segments: list[TranscriptSegment] = Field(default_factory=list)
    signal_score: float | None = None
    signals: dict[str, float] = Field(default_factory=dict)
    source: str = "deterministic"

    @classmethod
    def from_candidate(cls, candidate: Candidate) -> CandidatePayload:
        return cls(
            start=candidate.start,
            end=candidate.end,
            score=candidate.score,
            category=candidate.category,
            reason=candidate.reason,
            transcript_segments=list(candidate.transcript_segments),
            signal_score=candidate.signal_score,
            signals=dict(candidate.signals),
            source=candidate.source,
        )

    def to_candidate(self) -> Candidate:
        return Candidate(
            start=self.start,
            end=self.end,
            score=self.score,
            category=self.category,
            reason=self.reason,
            transcript_segments=list(self.transcript_segments),
            signal_score=self.signal_score,
            signals=dict(self.signals),
            source=self.source,
        )


class CandidateArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    job_id: str
    notes: list[str] = Field(default_factory=list)
    classification: MediaClassification | None = None
    candidates: list[CandidatePayload] = Field(default_factory=list)
    final_result: HighlightResult | None = None

    def candidate_objects(self) -> list[Candidate]:
        return [item.to_candidate() for item in self.candidates]


class DiscoveryWindowArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    job_id: str
    window: DiscoveryWindow
    candidate_artifact: CandidateArtifact
    outcome: LlmOutcome


class DiscoveryPlanArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    job_id: str
    duration: float
    multi_pass: MultiPassSettings
    highlight_config: HighlightSettings
    content_type: ContentType
    provider: str | None = None
    model: str | None = None
    windows: list[DiscoveryWindow] = Field(default_factory=list)
    plan_sha256: str
    window_fingerprints: dict[str, dict[str, str]] = Field(default_factory=dict)


@dataclass(frozen=True)
class LlmStageResult:
    outcome: LlmOutcome
    error: AIError | None = None


@dataclass(frozen=True)
class AnalysisStageRequest:
    job_id: str
    media_path: Path
    audio_path: Path
    metadata: VideoMetadata
    transcript: TranscriptResult | None
    config: HighlightSettings
    content_type: ContentType
    provider: str | None
    model: str | None
    ffmpeg_binary: str
    source_hint: str | None
    manager: ProviderManager | None = None
    multi_pass: MultiPassSettings = field(default_factory=MultiPassSettings)


@dataclass
class DurableAnalysisRunner:
    stage_store: StageStore
    job_id: str
    artifact_dir: Path
    owner: str = field(default_factory=lambda: f"analysis-highlights-{uuid4().hex}")
    lease_seconds: float = _LEASE_SECONDS
    heartbeat_seconds: float = _HEARTBEAT_SECONDS

    @classmethod
    def for_settings(cls, settings: Settings, job_id: str, stage_store: StageStore) -> DurableAnalysisRunner:
        return cls(
            stage_store=stage_store,
            job_id=job_id,
            artifact_dir=settings.data_dir / "videos" / "jobs" / job_id / "analysis",
        )

    def reset_analysis_stages(self) -> None:
        for stage in ANALYSIS_STAGES:
            record = self._get(stage)
            if record is None:
                continue
            if record.status is StageStatus.RUNNING and not record.lease_expired:
                raise self._busy_error(stage, "Stage is actively owned by another worker.")
            self._reset(stage, "manual-refresh", clear_checkpoint=True)
        for record in self.stage_store.list_stages(
            job_type=ANALYSIS_JOB_TYPE, job_id=self.job_id
        ):
            if record.stage not in {STAGE_DISCOVERY_WINDOW, STAGE_LLM} or not record.unit_key:
                continue
            if record.status is StageStatus.RUNNING and not record.lease_expired:
                raise self._busy_error(record.stage, "Stage is actively owned by another worker.")
            self._reset(
                record.stage,
                "manual-refresh",
                clear_checkpoint=True,
                unit_key=record.unit_key,
            )

    def run_normalization(
        self,
        transcript: TranscriptResult,
        *,
        duration: float | None,
        config: Any,
        normalizer: Callable[[TranscriptResult, float | None], TranscriptResult],
    ) -> TranscriptResult:
        fingerprint = normalization_fingerprint(
            job_id=self.job_id,
            transcript=transcript,
            duration=duration,
            config=config,
        )
        result, _ = self._run_simple(
            STAGE_NORMALIZE,
            fingerprint=fingerprint,
            work=lambda: normalizer(transcript, duration),
            validate=lambda payload: TranscriptResult.model_validate(payload),
            input_hashes={
                "transcript_sha256": _result_sha256(transcript),
                "duration": duration or 0.0,
                "config_sha256": _hash_value(config),
            },
            heartbeat=False,
        )
        return result

    def run_highlights(
        self,
        request: AnalysisStageRequest,
        *,
        prepare: Callable[[MediaSignals], CandidateArtifact],
        run_llm: Callable[[list[Candidate], MediaSignals], LlmStageResult],
        assemble: Callable[
            [CandidateArtifact, MediaSignals, LlmOutcome], HighlightResult
        ],
    ) -> HighlightResult:
        hashes = _analysis_hashes(request)
        fingerprints = analysis_fingerprints(request, hashes)

        assembled = self._cached_result(
            STAGE_ASSEMBLE,
            fingerprints["assemble"],
            lambda payload: HighlightResult.model_validate(payload),
        )
        if assembled is not None:
            return assembled

        signals = self._run_signals(request, fingerprints, hashes)
        candidate_artifact, _ = self._run_simple(
            STAGE_CANDIDATES,
            fingerprint=fingerprints["candidates"],
            work=lambda: prepare(signals),
            validate=lambda payload: CandidateArtifact.model_validate(payload),
            input_hashes={
                "transcript_sha256": hashes["transcript"],
                "signals_audio": fingerprints["signals.audio"],
                "signals_scene": fingerprints["signals.scene"],
                "signals_motion": fingerprints["signals.motion"],
                "content_type": request.content_type.value,
                "source_hint_sha256": hashes["source_hint"],
            },
            heartbeat=False,
        )

        candidates = candidate_artifact.candidate_objects()
        outcome = self._run_llm_stage(
            request,
            fingerprint=fingerprints["llm"],
            candidates=candidates,
            signals=signals,
            skip=candidate_artifact.final_result is not None,
            run_llm=run_llm,
        )

        result, _ = self._run_simple(
            STAGE_ASSEMBLE,
            fingerprint=fingerprints["assemble"],
            work=lambda: (
                candidate_artifact.final_result
                if candidate_artifact.final_result is not None
                else assemble(candidate_artifact, signals, outcome)
            ),
            validate=lambda payload: HighlightResult.model_validate(payload),
            input_hashes={
                "transcript_sha256": hashes["transcript"],
                "candidates": fingerprints["candidates"],
                "llm": fingerprints["llm"],
                "content_type": request.content_type.value,
            },
            heartbeat=True,
        )
        return result

    def _multi_pass_fingerprints(
        self,
        request: AnalysisStageRequest,
        hashes: dict[str, str],
        fingerprints: dict[str, str],
        windows: Sequence[DiscoveryWindow],
    ) -> tuple[str, dict[str, dict[str, str]], str]:
        plan = {
            "window_seconds": request.multi_pass.window_seconds,
            "overlap_seconds": request.multi_pass.overlap_seconds,
            "windows": [
                {
                    "window_id": window.window_id,
                    "index": window.index,
                    "start": window.start,
                    "end": window.end,
                }
                for window in windows
            ],
        }
        plan_hash = _hash_value(plan)
        window_fingerprints: dict[str, dict[str, str]] = {}
        for window in windows:
            scoped_transcript = window_transcript(request.transcript, window)
            scoped_transcript_hash = (
                _result_sha256(scoped_transcript) if scoped_transcript is not None else ""
            )
            candidates = stable_fingerprint(
                scope="analysis.discovery.candidates",
                version=_ARTIFACT_VERSION,
                job_id=self.job_id,
                window_id=window.window_id,
                window_transcript_sha256=scoped_transcript_hash,
                transcript_sha256=hashes["transcript"],
                signals_audio=fingerprints["signals.audio"],
                signals_scene=fingerprints["signals.scene"],
                signals_motion=fingerprints["signals.motion"],
                content_type=request.content_type.value,
                source_hint_sha256=hashes["source_hint"],
                config_sha256=hashes["config"],
                plan_sha256=plan_hash,
            )
            llm = stable_fingerprint(
                scope="analysis.discovery.llm",
                version=_ARTIFACT_VERSION,
                job_id=self.job_id,
                window_id=window.window_id,
                candidates=candidates,
                window_transcript_sha256=scoped_transcript_hash,
                use_llm=request.config.use_llm,
                routing_sha256=hashes["routing"],
                requested_provider=request.provider or "",
                requested_model=request.model or "",
                llm_max_transcript_chars=request.config.llm_max_transcript_chars,
                llm_max_candidates=request.config.llm_max_candidates,
                plan_sha256=plan_hash,
            )
            window_fingerprints[window.window_id] = {
                "fingerprint": stable_fingerprint(
                    scope="analysis.discovery.window",
                    version=_ARTIFACT_VERSION,
                    job_id=self.job_id,
                    window_id=window.window_id,
                    candidates=candidates,
                    llm=llm,
                    config_sha256=hashes["config"],
                    content_type=request.content_type.value,
                    plan_sha256=plan_hash,
                ),
                "candidates": candidates,
                "llm": llm,
            }
        aggregate = stable_fingerprint(
            scope="analysis.discovery.aggregate",
            version=_ARTIFACT_VERSION,
            job_id=self.job_id,
            transcript_sha256=hashes["transcript"],
            config_sha256=hashes["config"],
            content_type=request.content_type.value,
            plan_sha256=plan_hash,
            window_fingerprints={
                window_id: values["fingerprint"]
                for window_id, values in window_fingerprints.items()
            },
        )
        return plan_hash, window_fingerprints, aggregate

    def _multi_pass_inputs(
        self, request: AnalysisStageRequest
    ) -> tuple[
        dict[str, str],
        dict[str, str],
        list[DiscoveryWindow],
        str,
        dict[str, dict[str, str]],
        str,
    ]:
        hashes = _analysis_hashes(request)
        fingerprints = analysis_fingerprints(request, hashes)
        windows = plan_discovery_windows(
            request.metadata.duration_seconds,
            request.multi_pass.window_seconds,
            request.multi_pass.overlap_seconds,
        )
        plan_hash, window_fingerprints, aggregate_fingerprint = self._multi_pass_fingerprints(
            request, hashes, fingerprints, windows
        )
        return (
            hashes,
            fingerprints,
            windows,
            plan_hash,
            window_fingerprints,
            aggregate_fingerprint,
        )

    def _aggregate_input_hashes(
        self,
        request: AnalysisStageRequest,
        hashes: dict[str, str],
        plan_hash: str,
        windows: Sequence[DiscoveryWindow],
        window_fingerprints: dict[str, dict[str, str]],
    ) -> dict[str, Any]:
        return {
            "plan_sha256": plan_hash,
            "transcript_sha256": hashes["transcript"],
            "config_sha256": hashes["config"],
            "content_type": request.content_type.value,
            "multi_pass": request.multi_pass.model_dump(mode="json"),
            "highlight_config": request.config.model_dump(mode="json"),
            "windows": [
                {
                    "window_id": window.window_id,
                    "fingerprint": window_fingerprints[window.window_id]["fingerprint"],
                }
                for window in windows
            ],
        }

    def _discovery_plan_fingerprint(
        self,
        request: AnalysisStageRequest,
        hashes: dict[str, str],
        plan_hash: str,
    ) -> str:
        return stable_fingerprint(
            scope="analysis.discovery.plan",
            version=_ARTIFACT_VERSION,
            job_id=self.job_id,
            plan_sha256=plan_hash,
            transcript_sha256=hashes["transcript"],
            config_sha256=hashes["config"],
            multi_pass=request.multi_pass.model_dump(mode="json"),
            highlight_config=request.config.model_dump(mode="json"),
            content_type=request.content_type.value,
            routing_sha256=hashes["routing"],
            requested_provider=request.provider or "",
            requested_model=request.model or "",
        )

    def _persist_discovery_plan(
        self,
        request: AnalysisStageRequest,
        hashes: dict[str, str],
        windows: Sequence[DiscoveryWindow],
        plan_hash: str,
        window_fingerprints: dict[str, dict[str, str]],
    ) -> DiscoveryPlanArtifact:
        fingerprint = self._discovery_plan_fingerprint(request, hashes, plan_hash)

        def work() -> DiscoveryPlanArtifact:
            return DiscoveryPlanArtifact(
                job_id=self.job_id,
                duration=request.metadata.duration_seconds,
                multi_pass=request.multi_pass,
                highlight_config=request.config,
                content_type=request.content_type,
                provider=request.provider,
                model=request.model,
                windows=list(windows),
                plan_sha256=plan_hash,
                window_fingerprints={
                    key: dict(values) for key, values in window_fingerprints.items()
                },
            )

        artifact, _ = self._run_simple(
            STAGE_DISCOVERY_PLAN,
            fingerprint=fingerprint,
            work=work,
            validate=lambda payload: DiscoveryPlanArtifact.model_validate(payload),
            input_hashes={
                "plan_sha256": plan_hash,
                "transcript_sha256": hashes["transcript"],
                "config_sha256": hashes["config"],
                "multi_pass": request.multi_pass.model_dump(mode="json"),
                "highlight_config": request.config.model_dump(mode="json"),
                "content_type": request.content_type.value,
                "routing_sha256": hashes["routing"],
                "requested_provider": request.provider or "",
                "requested_model": request.model or "",
            },
            heartbeat=False,
        )
        return artifact

    def _stored_discovery_plan(self) -> DiscoveryPlanArtifact | None:
        record = self._get(STAGE_DISCOVERY_PLAN)
        if record is None or record.status is not StageStatus.DONE:
            return None
        fingerprint = record.input_fingerprint
        if not fingerprint:
            return None
        return self._cached_result(
            STAGE_DISCOVERY_PLAN,
            fingerprint,
            lambda payload: DiscoveryPlanArtifact.model_validate(payload),
            record=record,
        )

    def _cached_discovery_aggregate(
        self,
        aggregate_fingerprint: str,
        aggregate_input_hashes: dict[str, Any],
    ) -> HighlightResult | None:
        record = self._get(STAGE_DISCOVERY_AGGREGATE)
        if (
            record is None
            or record.status is not StageStatus.DONE
            or record.input_fingerprint != aggregate_fingerprint
        ):
            return None
        return self._cached_result(
            STAGE_DISCOVERY_AGGREGATE,
            aggregate_fingerprint,
            lambda payload: HighlightResult.model_validate(payload),
            record=record,
            input_hashes_override=aggregate_input_hashes,
        )

    def _discovery_window_is_valid(
        self,
        window: DiscoveryWindow,
        fingerprint: str,
    ) -> bool:
        record = self._get(STAGE_DISCOVERY_WINDOW, unit_key=window.window_id)
        if (
            record is None
            or record.status is not StageStatus.DONE
            or record.input_fingerprint != fingerprint
        ):
            return False
        return (
            self._cached_result(
                STAGE_DISCOVERY_WINDOW,
                fingerprint,
                lambda payload: DiscoveryWindowArtifact.model_validate(payload),
                record=record,
                unit_key=window.window_id,
            )
            is not None
        )

    def cached_multi_pass_result(self, request: AnalysisStageRequest) -> HighlightResult | None:
        (
            hashes,
            _,
            windows,
            plan_hash,
            window_fingerprints,
            aggregate_fingerprint,
        ) = self._multi_pass_inputs(request)
        aggregate_input_hashes = self._aggregate_input_hashes(
            request, hashes, plan_hash, windows, window_fingerprints
        )
        aggregate = self._cached_discovery_aggregate(
            aggregate_fingerprint, aggregate_input_hashes
        )
        if aggregate is None:
            return None
        for window in windows:
            if not self._discovery_window_is_valid(
                window, window_fingerprints[window.window_id]["fingerprint"]
            ):
                return None
        return aggregate

    def clear_multi_pass_result(self) -> None:
        record = self._get(STAGE_DISCOVERY_AGGREGATE)
        if record is None:
            return
        self._reset(
            STAGE_DISCOVERY_AGGREGATE,
            record.input_fingerprint or "",
            clear_checkpoint=True,
        )

    def run_multi_pass_highlights(
        self,
        request: AnalysisStageRequest,
        *,
        prepare_window: Callable[
            [MediaSignals, TranscriptResult | None], CandidateArtifact
        ],
        run_llm_window: Callable[
            [list[Candidate], MediaSignals, TranscriptResult | None], LlmStageResult
        ],
        observer: DiscoveryObserver | None = None,
    ) -> HighlightResult:
        (
            hashes,
            fingerprints,
            windows,
            plan_hash,
            window_fingerprints,
            aggregate_fingerprint,
        ) = self._multi_pass_inputs(request)
        _emit_discovery(
            observer,
            "multi_pass_windows",
            0,
            len(windows),
            {
                "window_seconds": request.multi_pass.window_seconds,
                "overlap_seconds": request.multi_pass.overlap_seconds,
            },
        )
        self._persist_discovery_plan(
            request, hashes, windows, plan_hash, window_fingerprints
        )
        aggregate_input_hashes = self._aggregate_input_hashes(
            request, hashes, plan_hash, windows, window_fingerprints
        )
        cached = self._cached_discovery_aggregate(
            aggregate_fingerprint, aggregate_input_hashes
        )
        if cached is not None:
            windows_valid = all(
                self._discovery_window_is_valid(
                    window, window_fingerprints[window.window_id]["fingerprint"]
                )
                for window in windows
            )
            if windows_valid:
                _emit_discovery(
                    observer, "multi_pass_aggregate", len(windows), len(cached.highlights)
                )
                return cached
            self._reset(
                STAGE_DISCOVERY_AGGREGATE,
                aggregate_fingerprint,
                clear_checkpoint=True,
            )

        signals = self._run_signals(request, fingerprints, hashes)
        artifacts: list[DiscoveryWindowArtifact] = []
        for window in windows:
            window_id = window.window_id
            scoped_transcript = window_transcript(request.transcript, window)
            scoped_signals = window_signals(signals, window)
            values = window_fingerprints[window_id]
            input_hashes = {
                "plan_sha256": plan_hash,
                "candidates": values["candidates"],
                "llm": values["llm"],
                "window_transcript_sha256": (
                    _result_sha256(scoped_transcript) if scoped_transcript is not None else ""
                ),
                "signals_audio": fingerprints["signals.audio"],
                "signals_scene": fingerprints["signals.scene"],
                "signals_motion": fingerprints["signals.motion"],
                "content_type": request.content_type.value,
            }

            def window_work(
                current_window: DiscoveryWindow = window,
                current_transcript: TranscriptResult | None = scoped_transcript,
                current_signals: MediaSignals = scoped_signals,
                current_values: dict[str, str] = values,
                current_hashes: dict[str, Any] = input_hashes,
            ) -> DiscoveryWindowArtifact:
                candidate_artifact = prepare_window(current_signals, current_transcript)
                candidates = candidate_artifact.candidate_objects()

                def llm_call(
                    llm_candidates: list[Candidate], llm_signals: MediaSignals
                ) -> LlmStageResult:
                    return run_llm_window(llm_candidates, llm_signals, current_transcript)

                outcome = self._run_llm_stage(
                    request,
                    fingerprint=current_values["llm"],
                    candidates=candidates,
                    signals=current_signals,
                    skip=candidate_artifact.final_result is not None,
                    run_llm=llm_call,
                    unit_key=current_window.window_id,
                    strict=True,
                )
                if outcome.applied and outcome.highlights:
                    valid_highlights = [
                        highlight
                        for highlight in outcome.highlights
                        if highlight.start_time >= current_window.start - 0.001
                        and highlight.end_time <= current_window.end + 0.001
                    ]
                    if not valid_highlights:
                        raise AIError(
                            ErrorCategory.INVALID_RESPONSE,
                            f"LLM highlights fell outside discovery window '{current_window.window_id}'.",
                            retryable=True,
                            details={"reason": "window_bounds", "stage": STAGE_DISCOVERY_WINDOW},
                        )
                    outcome = outcome.model_copy(update={"highlights": valid_highlights})
                return DiscoveryWindowArtifact(
                    job_id=self.job_id,
                    window=current_window,
                    candidate_artifact=candidate_artifact,
                    outcome=outcome,
                )

            artifact, _ = self._run_simple(
                STAGE_DISCOVERY_WINDOW,
                fingerprint=values["fingerprint"],
                work=window_work,
                validate=lambda payload: DiscoveryWindowArtifact.model_validate(payload),
                input_hashes=input_hashes,
                heartbeat=False,
                unit_key=window_id,
            )
            artifacts.append(artifact)
            _emit_discovery(
                observer,
                "multi_pass_window",
                0,
                len(artifact.candidate_artifact.candidates),
                {
                    "window_id": window_id,
                    "start": window.start,
                    "end": window.end,
                },
            )

        aggregate, _ = self._run_simple(
            STAGE_DISCOVERY_AGGREGATE,
            fingerprint=aggregate_fingerprint,
            work=lambda: aggregate_window_artifacts(
                artifacts,
                job_id=self.job_id,
                content_type=request.content_type,
                duration=request.metadata.duration_seconds,
                transcript=request.transcript,
                config=request.config,
            ),
            validate=lambda payload: HighlightResult.model_validate(payload),
            input_hashes=aggregate_input_hashes,
            heartbeat=True,
        )
        _emit_discovery(
            observer, "multi_pass_aggregate", len(artifacts), len(aggregate.highlights)
        )
        return aggregate

    def discovery_progress(self, request: AnalysisStageRequest) -> dict[str, Any]:
        plan = self._stored_discovery_plan()
        if plan is not None and abs(plan.duration - request.metadata.duration_seconds) <= 1e-6:
            request = replace(
                request,
                multi_pass=plan.multi_pass,
                config=plan.highlight_config,
                content_type=plan.content_type,
                provider=plan.provider,
                model=plan.model,
            )
        else:
            plan = None
            aggregate = self._get(STAGE_DISCOVERY_AGGREGATE)
            checkpoint = aggregate.checkpoint if aggregate is not None else None
            if isinstance(checkpoint, dict):
                input_hashes = checkpoint.get("input_hashes")
                if isinstance(input_hashes, dict):
                    multi_pass_raw = input_hashes.get("multi_pass")
                    if isinstance(multi_pass_raw, dict) and multi_pass_raw.get("enabled"):
                        request = replace(
                            request,
                            multi_pass=MultiPassSettings.model_validate(multi_pass_raw),
                        )
                    config_raw = input_hashes.get("highlight_config")
                    if isinstance(config_raw, dict):
                        request = replace(
                            request, config=HighlightSettings.model_validate(config_raw)
                        )
                    content_type_raw = input_hashes.get("content_type")
                    if isinstance(content_type_raw, str):
                        request = replace(request, content_type=ContentType(content_type_raw))
        (
            hashes,
            _,
            planned_windows,
            plan_hash,
            window_fingerprints,
            aggregate_fingerprint,
        ) = self._multi_pass_inputs(request)
        windows = planned_windows
        if plan is not None and [item.window_id for item in plan.windows] == [
            item.window_id for item in planned_windows
        ]:
            windows = plan.windows
        aggregate_input_hashes = self._aggregate_input_hashes(
            request, hashes, plan_hash, windows, window_fingerprints
        )
        aggregate_cached = (
            self._cached_discovery_aggregate(aggregate_fingerprint, aggregate_input_hashes)
            is not None
        )
        valid_by_id = {
            window.window_id: self._discovery_window_is_valid(
                window, window_fingerprints[window.window_id]["fingerprint"]
            )
            for window in windows
        }
        aggregate_ready = aggregate_cached and all(valid_by_id.values())
        entries: list[dict[str, Any]] = []
        counts = {
            "complete": 0,
            "pending": 0,
            "running": 0,
            "failed": 0,
            "stale": 0,
            "planned": 0,
            "completed": 0,
            "reused": 0,
        }
        for window in windows:
            fingerprint = window_fingerprints[window.window_id]["fingerprint"]
            record = self._get(STAGE_DISCOVERY_WINDOW, unit_key=window.window_id)
            if record is None:
                status = "pending"
                state = "planned"
                reused = False
            elif record.input_fingerprint != fingerprint or not valid_by_id[window.window_id]:
                status = "stale"
                state = "stale"
                reused = False
            elif record.status is StageStatus.DONE:
                status = "complete"
                reused = aggregate_ready
                state = "reused" if reused else "completed"
            else:
                status = record.status.value
                state = status
                reused = False
            counts[status] = counts.get(status, 0) + 1
            counts[state] = counts.get(state, 0) + 1
            entries.append(
                {
                    "window_id": window.window_id,
                    "index": window.index,
                    "start": window.start,
                    "end": window.end,
                    "status": status,
                    "state": state,
                    "reused": reused,
                }
            )
        next_window = next(
            (entry["window_id"] for entry in entries if entry["status"] != "complete"),
            None,
        )
        return {
            "enabled": request.multi_pass.enabled,
            "total": len(windows),
            "complete": counts["complete"],
            "pending": counts["pending"],
            "running": counts["running"],
            "failed": counts["failed"],
            "stale": counts["stale"],
            "planned": counts["planned"],
            "completed": counts["completed"],
            "reused": counts["reused"],
            "next_window_id": next_window,
            "windows": entries,
        }

    def _run_signals(
        self,
        request: AnalysisStageRequest,
        fingerprints: dict[str, str],
        hashes: dict[str, str],
    ) -> MediaSignals:
        common_hashes = {
            "media_sha256": hashes["media"],
            "audio_sha256": hashes["audio"],
            "duration": request.metadata.duration_seconds,
            "config_sha256": hashes["config"],
            "ffmpeg_binary": request.ffmpeg_binary,
        }

        def audio_work() -> AudioSignalsArtifact:
            note = None
            windows: list[AudioWindow] = []
            if request.metadata.has_audio:
                windows, note = analyze_audio_signals(
                    request.media_path,
                    duration=request.metadata.duration_seconds,
                    config=request.config,
                    ffmpeg_binary=request.ffmpeg_binary,
                )
            else:
                note = "No audio stream was available for activity analysis."
            failure = _signal_failure(note)
            if failure is not None:
                raise failure
            return AudioSignalsArtifact(
                duration=request.metadata.duration_seconds,
                windows=windows,
                note=note,
            )

        audio, _ = self._run_simple(
            STAGE_SIGNALS_AUDIO,
            fingerprint=fingerprints["signals.audio"],
            work=audio_work,
            validate=lambda payload: AudioSignalsArtifact.model_validate(payload),
            input_hashes={**common_hashes, "has_audio": request.metadata.has_audio},
            heartbeat=True,
        )

        def scene_work() -> SceneSignalsArtifact:
            note = None
            changes: list[SceneChange] = []
            if request.metadata.has_video:
                changes, note = analyze_scene_changes(
                    request.media_path,
                    config=request.config,
                    ffmpeg_binary=request.ffmpeg_binary,
                )
            else:
                note = "No video stream was available for scene or motion analysis."
            failure = _signal_failure(note)
            if failure is not None:
                raise failure
            return SceneSignalsArtifact(
                duration=request.metadata.duration_seconds,
                changes=changes,
                note=note,
            )

        scene, _ = self._run_simple(
            STAGE_SIGNALS_SCENE,
            fingerprint=fingerprints["signals.scene"],
            work=scene_work,
            validate=lambda payload: SceneSignalsArtifact.model_validate(payload),
            input_hashes={**common_hashes, "has_video": request.metadata.has_video},
            heartbeat=True,
        )

        def motion_work() -> MotionSignalsArtifact:
            note = None
            samples: list[MotionSample] = []
            if request.metadata.has_video:
                samples, note = analyze_motion(
                    request.media_path,
                    config=request.config,
                    ffmpeg_binary=request.ffmpeg_binary,
                )
            failure = _signal_failure(note)
            if failure is not None:
                raise failure
            return MotionSignalsArtifact(
                duration=request.metadata.duration_seconds,
                samples=samples,
                note=note,
            )

        motion, _ = self._run_simple(
            STAGE_SIGNALS_MOTION,
            fingerprint=fingerprints["signals.motion"],
            work=motion_work,
            validate=lambda payload: MotionSignalsArtifact.model_validate(payload),
            input_hashes={
                **common_hashes,
                "has_video": request.metadata.has_video,
                "analyze_motion": request.config.analyze_motion,
            },
            heartbeat=True,
        )

        notes = [
            note
            for note in (audio.note, scene.note, motion.note)
            if note and not note.startswith("No video stream")
        ]
        return MediaSignals(
            duration=request.metadata.duration_seconds,
            audio_windows=audio.windows,
            scene_changes=scene.changes,
            motion_samples=motion.samples,
            audio_analyzed=request.metadata.has_audio and audio.note is None,
            video_analyzed=request.metadata.has_video and scene.note is None,
            motion_analyzed=(
                request.metadata.has_video
                and request.config.analyze_motion
                and motion.note is None
            ),
            notes=notes,
        )

    def _run_llm_stage(
        self,
        request: AnalysisStageRequest,
        *,
        fingerprint: str,
        candidates: list[Candidate],
        signals: MediaSignals,
        skip: bool,
        run_llm: Callable[[list[Candidate], MediaSignals], LlmStageResult],
        unit_key: str = "",
        strict: bool = False,
    ) -> LlmOutcome:
        record = self._get(STAGE_LLM, unit_key=unit_key)
        if record is None:
            record = self._ensure(STAGE_LLM, fingerprint, unit_key=unit_key)
        elif record.input_fingerprint != fingerprint:
            record = self._reset(
                STAGE_LLM, fingerprint, clear_checkpoint=True, unit_key=unit_key
            )
        if record.status is StageStatus.RUNNING and not record.lease_expired:
            raise self._busy_error(STAGE_LLM, "Stage is actively owned by another worker.")

        if record.status is StageStatus.DONE:
            cached = self._cached_result(
                STAGE_LLM,
                fingerprint,
                lambda payload: LlmOutcome.model_validate(payload),
                record=record,
                unit_key=unit_key,
            )
            if cached is not None:
                return cached
            record = self._reset(
                STAGE_LLM, fingerprint, clear_checkpoint=True, unit_key=unit_key
            )

        failure_snapshot = self._llm_failure_snapshot(record, fingerprint)
        if failure_snapshot is not None:
            outcome, next_retry_at, retryable = failure_snapshot
            waiting = next_retry_at is not None and time.time() < next_retry_at
            if strict and next_retry_at is not None and waiting:
                raise self._retry_wait_error(
                    STAGE_LLM, record, float(next_retry_at) - time.time()
                )
            if strict and not retryable:
                raise self._stored_error(STAGE_LLM, record)
            if not strict and (not retryable or waiting):
                return outcome

        if record.status is StageStatus.FAILED:
            raise self._stored_error(STAGE_LLM, record)
        wait = self._retry_wait(record)
        if wait > 0 and failure_snapshot is None:
            raise self._retry_wait_error(STAGE_LLM, record, wait)

        claimed = self._claim(STAGE_LLM, unit_key=unit_key)
        with self._heartbeat(STAGE_LLM, unit_key=unit_key):
            try:
                if skip:
                    stage_result = LlmStageResult(LlmOutcome())
                else:
                    stage_result = run_llm(candidates, signals)
            except StageOwnershipError as exc:
                raise self._lease_lost_error(STAGE_LLM, exc) from exc
            except AIError as exc:
                fallback = self._failed_llm_outcome(exc)
                self._persist_llm_failure(
                    claimed, fingerprint, fallback, exc, unit_key=unit_key
                )
                if strict:
                    raise
                return fallback
            except Exception as exc:
                error = AIError(
                    ErrorCategory.INTERNAL_ERROR,
                    f"LLM refinement failed unexpectedly: {exc}",
                    retryable=True,
                    cause=exc,
                )
                fallback = self._failed_llm_outcome(error)
                self._persist_llm_failure(
                    claimed, fingerprint, fallback, error, unit_key=unit_key
                )
                if strict:
                    raise error from exc
                return fallback

            outcome = self._safe_outcome(stage_result.outcome)
            if stage_result.error is not None:
                self._persist_llm_failure(
                    claimed, fingerprint, outcome, stage_result.error, unit_key=unit_key
                )
                if strict:
                    raise stage_result.error
                return outcome
            self._complete_artifact(
                STAGE_LLM, claimed, fingerprint, outcome, unit_key=unit_key
            )
            return outcome

    def _persist_llm_failure(
        self,
        record: StageRecord,
        fingerprint: str,
        outcome: LlmOutcome,
        error: AIError,
        *,
        unit_key: str = "",
    ) -> None:
        retryable = stage_retryable(error)
        now = time.time()
        next_retry_at = now + error.retry_after if error.retry_after else None
        safe_error = self._error_payload(error, record, retryable, next_retry_at)
        checkpoint = {
            "version": _ARTIFACT_VERSION,
            "job_id": self.job_id,
            "stage": STAGE_LLM,
            "fingerprint": fingerprint,
            "payload_sha256": _outcome_sha256(outcome),
            "status": "fallback",
            "outcome": outcome.model_dump(mode="json"),
            "next_retry_at": next_retry_at,
            "error": safe_error,
        }
        try:
            self.stage_store.save_checkpoint(
                job_type=ANALYSIS_JOB_TYPE,
                job_id=self.job_id,
                stage=STAGE_LLM,
                unit_key=unit_key,
                owner=self.owner,
                checkpoint=checkpoint,
            )
            self.stage_store.fail(
                job_type=ANALYSIS_JOB_TYPE,
                job_id=self.job_id,
                stage=STAGE_LLM,
                unit_key=unit_key,
                owner=self.owner,
                error=safe_error,
                retryable=retryable,
            )
        except StageOwnershipError as exc:
            raise self._lease_lost_error(STAGE_LLM, exc) from exc

    @staticmethod
    def _failed_llm_outcome(error: AIError) -> LlmOutcome:
        return LlmOutcome(
            error=f"{error.message} Deterministic candidates were used instead.",
            provider=error.provider,
            model=error.model,
            applied=True,
            failed=True,
            retryable=stage_retryable(error),
            retry_after=error.retry_after,
        )

    @staticmethod
    def _safe_outcome(outcome: LlmOutcome) -> LlmOutcome:
        return outcome.model_copy(update={"response_data": None})

    def _llm_failure_snapshot(
        self, record: StageRecord, fingerprint: str
    ) -> tuple[LlmOutcome, float | None, bool] | None:
        if record.status not in {StageStatus.PENDING, StageStatus.FAILED}:
            return None
        checkpoint = record.checkpoint
        if not isinstance(checkpoint, dict):
            return None
        if checkpoint.get("version") != _ARTIFACT_VERSION:
            return None
        if checkpoint.get("stage") != STAGE_LLM or checkpoint.get("fingerprint") != fingerprint:
            return None
        if checkpoint.get("status") != "fallback":
            return None
        raw_outcome = checkpoint.get("outcome")
        if not isinstance(raw_outcome, dict):
            return None
        if checkpoint.get("payload_sha256") != _hash_value(raw_outcome):
            return None
        try:
            outcome = LlmOutcome.model_validate(raw_outcome)
        except ValidationError:
            return None
        if not outcome.failed:
            return None
        next_retry_at = checkpoint.get("next_retry_at")
        if not isinstance(next_retry_at, (int, float)):
            next_retry_at = None
        return outcome, next_retry_at, outcome.retryable

    def _run_simple(
        self,
        stage: str,
        *,
        fingerprint: str,
        work: Callable[[], T],
        validate: Callable[[Any], T],
        input_hashes: dict[str, Any],
        heartbeat: bool,
        unit_key: str = "",
    ) -> tuple[T, str]:
        record, cached = self._prepare_simple(
            stage,
            fingerprint=fingerprint,
            validate=validate,
            input_hashes=input_hashes,
            unit_key=unit_key,
        )
        if cached is not None:
            checkpoint = record.checkpoint
            assert isinstance(checkpoint, dict)
            return cached, str(checkpoint.get("payload_sha256") or "")

        claimed = self._claim(stage, unit_key=unit_key)
        with self._heartbeat(stage, unit_key=unit_key) if heartbeat else _null_context():
            try:
                value = work()
                payload = value.model_dump(mode="json")
                checkpoint = self._complete_artifact(
                    stage,
                    claimed,
                    fingerprint,
                    payload,
                    input_hashes=input_hashes,
                    unit_key=unit_key,
                )
                return value, str(checkpoint.get("payload_sha256") or "")
            except StageOwnershipError as exc:
                raise self._lease_lost_error(stage, exc) from exc
            except AIError as exc:
                self._record_failure(
                    stage, claimed, fingerprint, exc, input_hashes, unit_key=unit_key
                )
                raise
            except Exception as exc:
                error = AIError(
                    ErrorCategory.INTERNAL_ERROR,
                    f"Unexpected analysis failure in '{stage}': {exc}",
                    retryable=True,
                    cause=exc,
                    details={"stage": stage},
                )
                self._record_failure(
                    stage, claimed, fingerprint, error, input_hashes, unit_key=unit_key
                )
                raise error from exc

    def _prepare_simple(
        self,
        stage: str,
        *,
        fingerprint: str,
        validate: Callable[[Any], T],
        input_hashes: dict[str, Any],
        unit_key: str = "",
    ) -> tuple[StageRecord, T | None]:
        record = self._ensure(stage, fingerprint, unit_key=unit_key)
        if record.input_fingerprint != fingerprint:
            if record.status is StageStatus.RUNNING and not record.lease_expired:
                raise self._busy_error(stage, "Stage is actively owned by another worker.")
            record = self._reset(
                stage, fingerprint, clear_checkpoint=True, unit_key=unit_key
            )

        if record.status is StageStatus.RUNNING and not record.lease_expired:
            raise self._busy_error(stage, "Stage is actively owned by another worker.")

        if record.status is StageStatus.DONE:
            cached = self._cached_result(
                stage, fingerprint, validate, record=record, unit_key=unit_key
            )
            if cached is not None:
                return record, cached
            record = self._reset(
                stage, fingerprint, clear_checkpoint=True, unit_key=unit_key
            )

        if record.status is StageStatus.FAILED:
            raise self._stored_error(stage, record)
        wait = self._retry_wait(record)
        if wait > 0:
            raise self._retry_wait_error(stage, record, wait)
        return record, None

    def _cached_result(
        self,
        stage: str,
        fingerprint: str,
        validate: Callable[[Any], T],
        *,
        record: StageRecord | None = None,
        unit_key: str = "",
        input_hashes_override: dict[str, Any] | None = None,
    ) -> T | None:
        record = record or self._get(stage)
        if record is None or record.status is not StageStatus.DONE:
            return None
        checkpoint = record.checkpoint
        if not isinstance(checkpoint, dict):
            return None
        if checkpoint.get("version") != _ARTIFACT_VERSION:
            return None
        if checkpoint.get("job_id") != self.job_id:
            return None
        if checkpoint.get("stage") != stage or checkpoint.get("fingerprint") != fingerprint:
            return None
        artifact = self._artifact_path(stage, unit_key=unit_key)
        if record.artifact_path is None or Path(record.artifact_path) != artifact:
            return None
        if record.artifact_sha256 is None or not artifact.is_file():
            return None
        try:
            if hash_file(artifact) != record.artifact_sha256:
                return None
        except OSError:
            return None
        envelope = self._read_artifact(stage, artifact, fingerprint)
        if envelope is None:
            return None
        payload = envelope["payload"]
        expected_hash = str(checkpoint.get("payload_sha256") or "")
        if expected_hash != envelope["payload_sha256"]:
            return None
        expected_input_hashes = (
            input_hashes_override
            if input_hashes_override is not None
            else checkpoint.get("input_hashes")
        )
        if expected_input_hashes != envelope["input_hashes"]:
            return None
        try:
            return validate(payload)
        except (ValidationError, TypeError, ValueError):
            return None

    def _complete_artifact(
        self,
        stage: str,
        record: StageRecord,
        fingerprint: str,
        payload: Any,
        *,
        input_hashes: dict[str, Any] | None = None,
        unit_key: str = "",
    ) -> dict[str, Any]:
        normalized_payload = (
            json.loads(payload.model_dump_json())
            if isinstance(payload, BaseModel)
            else json.loads(_canonical_json(payload))
        )
        artifact = self._artifact_path(stage, unit_key=unit_key)
        payload_sha256 = _hash_value(normalized_payload)
        envelope = {
            "version": _ARTIFACT_VERSION,
            "job_id": self.job_id,
            "stage": stage,
            "fingerprint": fingerprint,
            "payload": normalized_payload,
            "payload_sha256": payload_sha256,
            "input_hashes": input_hashes or {},
            "created_at": datetime.now(UTC).isoformat(),
        }
        self._write_json_atomic(artifact, envelope)
        checkpoint = {
            "version": _ARTIFACT_VERSION,
            "job_id": self.job_id,
            "stage": stage,
            "fingerprint": fingerprint,
            "payload_sha256": payload_sha256,
            "input_hashes": input_hashes or {},
            "attempt": record.attempt,
        }
        try:
            self.stage_store.complete(
                job_type=ANALYSIS_JOB_TYPE,
                job_id=self.job_id,
                stage=stage,
                unit_key=unit_key,
                owner=self.owner,
                checkpoint=checkpoint,
                artifact_path=artifact,
                require_artifact=True,
            )
        except StageOwnershipError as exc:
            raise self._lease_lost_error(stage, exc) from exc
        return checkpoint

    def _write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = (_canonical_json(payload) + "\n").encode("utf-8")
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.part")
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _read_artifact(self, stage: str, path: Path, fingerprint: str) -> dict[str, Any] | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        if raw.get("version") != _ARTIFACT_VERSION:
            return None
        if raw.get("job_id") != self.job_id or raw.get("stage") != stage:
            return None
        if raw.get("fingerprint") != fingerprint:
            return None
        payload = raw.get("payload")
        expected = raw.get("payload_sha256")
        if not isinstance(expected, str) or expected != _hash_value(payload):
            return None
        input_hashes = raw.get("input_hashes")
        if not isinstance(input_hashes, dict):
            return None
        return {
            "payload": payload,
            "payload_sha256": expected,
            "input_hashes": input_hashes,
        }

    def _artifact_path(self, stage: str, *, unit_key: str = "") -> Path:
        safe_stage = stage.replace("/", "_").replace(".", "_")
        if not unit_key:
            return self.artifact_dir / f"{safe_stage}.json"
        safe_unit = "".join(char if char.isalnum() or char in "-_" else "_" for char in unit_key)
        return self.artifact_dir / f"{safe_stage}__{safe_unit}.json"

    def _get(self, stage: str, *, unit_key: str = "") -> StageRecord | None:
        return self.stage_store.get(
            job_type=ANALYSIS_JOB_TYPE,
            job_id=self.job_id,
            stage=stage,
            unit_key=unit_key,
        )

    def _ensure(self, stage: str, fingerprint: str, *, unit_key: str = "") -> StageRecord:
        return self.stage_store.ensure(
            job_type=ANALYSIS_JOB_TYPE,
            job_id=self.job_id,
            stage=stage,
            unit_key=unit_key,
            input_fingerprint=fingerprint,
        )

    def _reset(
        self,
        stage: str,
        fingerprint: str,
        *,
        clear_checkpoint: bool,
        unit_key: str = "",
    ) -> StageRecord:
        try:
            return self.stage_store.reset(
                job_type=ANALYSIS_JOB_TYPE,
                job_id=self.job_id,
                stage=stage,
                unit_key=unit_key,
                input_fingerprint=fingerprint,
                clear_checkpoint=clear_checkpoint,
            )
        except StageOwnershipError as exc:
            raise self._busy_error(stage, "Stage is actively owned by another worker.") from exc

    def _claim(self, stage: str, *, unit_key: str = "") -> StageRecord:
        try:
            claimed = self.stage_store.claim_next(
                job_type=ANALYSIS_JOB_TYPE,
                job_id=self.job_id,
                stage=stage,
                unit_key=unit_key,
                owner=self.owner,
                lease_seconds=self.lease_seconds,
            )
        except StageError as exc:
            raise self._busy_error(stage, "Stage claim was lost.", exc) from exc
        if claimed is None:
            raise self._busy_error(stage, "Stage could not be claimed.")
        return claimed

    def _record_failure(
        self,
        stage: str,
        record: StageRecord,
        fingerprint: str,
        error: AIError,
        input_hashes: dict[str, Any],
        *,
        unit_key: str = "",
    ) -> None:
        retryable = stage_retryable(error)
        now = time.time()
        next_retry_at = now + error.retry_after if error.retry_after else None
        safe_error = self._error_payload(error, record, retryable, next_retry_at)
        checkpoint: dict[str, Any] = {
            "version": _ARTIFACT_VERSION,
            "job_id": self.job_id,
            "stage": stage,
            "fingerprint": fingerprint,
            "payload_sha256": "",
            "input_hashes": input_hashes,
            "status": "failed",
            "error": safe_error,
            "next_retry_at": next_retry_at,
        }
        try:
            self.stage_store.save_checkpoint(
                job_type=ANALYSIS_JOB_TYPE,
                job_id=self.job_id,
                stage=stage,
                unit_key=unit_key,
                owner=self.owner,
                checkpoint=checkpoint,
            )
            self.stage_store.fail(
                job_type=ANALYSIS_JOB_TYPE,
                job_id=self.job_id,
                stage=stage,
                unit_key=unit_key,
                owner=self.owner,
                error=safe_error,
                retryable=retryable,
            )
        except StageOwnershipError as exc:
            raise self._lease_lost_error(stage, exc) from exc

    def _error_payload(
        self,
        error: AIError,
        record: StageRecord,
        retryable: bool,
        next_retry_at: float | None,
    ) -> dict[str, Any]:
        return {
            "category": error.category.value,
            "message": error.message,
            "retryable": retryable,
            "retry_after": error.retry_after,
            "details": {
                **error.details,
                "stage": record.stage,
                "attempt": record.attempt,
                "provider": error.provider,
                "model": error.model,
                "failed_at": time.time(),
                "next_retry_at": next_retry_at,
            },
        }

    def _stored_error(self, stage: str, record: StageRecord) -> AIError:
        payload = record.error or {}
        try:
            category = ErrorCategory(str(payload.get("category") or "unknown_provider_error"))
        except ValueError:
            category = ErrorCategory.UNKNOWN_PROVIDER_ERROR
        details = payload.get("details")
        if not isinstance(details, dict):
            details = {}
        return AIError(
            category,
            str(payload.get("message") or f"Analysis stage '{stage}' failed."),
            provider=str(details.get("provider") or "") or None,
            model=str(details.get("model") or "") or None,
            retryable=bool(payload.get("retryable")),
            retry_after=payload.get("retry_after")
            if isinstance(payload.get("retry_after"), (int, float))
            else None,
            details={**details, "stage": stage},
        )

    def _retry_wait(self, record: StageRecord) -> float:
        details = (record.error or {}).get("details")
        if not isinstance(details, dict):
            return 0.0
        value = details.get("next_retry_at")
        if not isinstance(value, (int, float)):
            return 0.0
        return max(0.0, float(value) - time.time())

    def _retry_wait_error(self, stage: str, record: StageRecord, wait: float) -> AIError:
        base = self._stored_error(stage, record)
        return AIError(
            base.category,
            f"Analysis stage '{stage}' is waiting for its retry window "
            f"({math.ceil(wait)}s remaining).",
            provider=base.provider,
            model=base.model,
            retryable=True,
            retry_after=wait,
            details={**base.details, "reason": "retry_not_before"},
        )

    @contextmanager
    def _heartbeat(self, stage: str, *, unit_key: str = "") -> Iterator[None]:
        stop = threading.Event()
        thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(stage, unit_key, stop),
            name=f"clipping-analysis-{stage}",
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=1.0)

    def _heartbeat_loop(self, stage: str, unit_key: str, stop: threading.Event) -> None:
        while not stop.wait(self.heartbeat_seconds):
            try:
                if not self.stage_store.heartbeat(
                    job_type=ANALYSIS_JOB_TYPE,
                    job_id=self.job_id,
                    stage=stage,
                    unit_key=unit_key,
                    owner=self.owner,
                    lease_seconds=self.lease_seconds,
                ):
                    return
            except Exception:
                return

    def _busy_error(
        self, stage: str, message: str, exc: Exception | None = None
    ) -> AIError:
        return AIError(
            ErrorCategory.INTERNAL_ERROR,
            message,
            retryable=True,
            details={"stage": stage, "reason": "lease_held"},
            cause=exc,
        )

    @staticmethod
    def _lease_lost_error(stage: str, exc: StageOwnershipError) -> AIError:
        return AIError(
            ErrorCategory.INTERNAL_ERROR,
            f"Analysis stage '{stage}' ownership was lost before it could be committed.",
            retryable=True,
            details={"stage": stage, "reason": "lease_lost"},
            cause=exc,
        )


@contextmanager
def _null_context() -> Iterator[None]:
    yield None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash_value(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _result_sha256(result: TranscriptResult) -> str:
    return _hash_value(result.model_dump(mode="json"))


def _sha256_file(path: Path) -> str:
    try:
        return hash_file(path)
    except OSError as exc:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            f"Analysis input file is missing or unreadable: {path}",
            retryable=False,
            cause=exc,
        ) from exc


def normalization_fingerprint(
    *,
    job_id: str,
    transcript: TranscriptResult,
    duration: float | None,
    config: Any,
) -> str:
    return stable_fingerprint(
        scope="analysis.normalize",
        version=_ARTIFACT_VERSION,
        job_id=job_id,
        transcript_sha256=_result_sha256(transcript),
        duration=duration or 0.0,
        config_sha256=_hash_value(config),
    )


def _analysis_hashes(request: AnalysisStageRequest) -> dict[str, str]:
    transcript = request.transcript
    config_hash = _hash_value(request.config.model_dump(mode="json"))
    source_hint = request.source_hint or ""
    return {
        "media": _sha256_file(request.media_path),
        "audio": _sha256_file(request.audio_path),
        "transcript": _result_sha256(transcript) if transcript is not None else "",
        "config": config_hash,
        "source_hint": hashlib.sha256(source_hint.encode("utf-8")).hexdigest(),
        "routing": _routing_fingerprint(request.manager, request.provider, request.model),
    }


def _routing_fingerprint(
    manager: ProviderManager | None, provider: str | None, model: str | None
) -> str:
    base: dict[str, Any] = {"provider": provider or "", "model": model or ""}
    router = getattr(manager, "router", None)
    resolve = getattr(router, "resolve", None)
    if not callable(resolve):
        return _hash_value(base)
    try:
        routing = resolve("highlight_detection", provider=provider, model=model)
    except Exception:
        return _hash_value(base)
    base["candidates"] = [
        {
            "provider": item.entry.provider,
            "model": item.entry.id,
            "tasks": list(item.entry.tasks),
        }
        for item in routing.candidates
    ]
    base["skipped"] = [
        {
            "provider": item.provider,
            "model": item.model,
            "reason": item.reason,
            "category": item.category.value,
        }
        for item in routing.skipped
    ]
    return _hash_value(base)


def analysis_fingerprints(
    request: AnalysisStageRequest, hashes: dict[str, str]
) -> dict[str, str]:
    common = {
        "job_id": request.job_id,
        "media_sha256": hashes["media"],
        "audio_sha256": hashes["audio"],
        "duration": request.metadata.duration_seconds,
        "config_sha256": hashes["config"],
        "ffmpeg_binary": request.ffmpeg_binary,
    }
    audio = stable_fingerprint(
        scope="analysis.signals.audio", version=_ARTIFACT_VERSION, **common
    )
    scene = stable_fingerprint(
        scope="analysis.signals.scene", version=_ARTIFACT_VERSION, **common
    )
    motion = stable_fingerprint(
        scope="analysis.signals.motion", version=_ARTIFACT_VERSION, **common
    )
    candidates = stable_fingerprint(
        scope="analysis.candidates",
        version=_ARTIFACT_VERSION,
        job_id=request.job_id,
        transcript_sha256=hashes["transcript"],
        signals_audio=audio,
        signals_scene=scene,
        signals_motion=motion,
        content_type=request.content_type.value,
        source_hint_sha256=hashes["source_hint"],
        config_sha256=hashes["config"],
        requested_provider=request.provider or "",
        requested_model=request.model or "",
    )
    llm = stable_fingerprint(
        scope="analysis.llm",
        version=_ARTIFACT_VERSION,
        job_id=request.job_id,
        transcript_sha256=hashes["transcript"],
        candidates=candidates,
        signals_audio=audio,
        signals_scene=scene,
        signals_motion=motion,
        use_llm=request.config.use_llm,
        routing_sha256=hashes["routing"],
        requested_provider=request.provider or "",
        requested_model=request.model or "",
        llm_max_transcript_chars=request.config.llm_max_transcript_chars,
        llm_max_candidates=request.config.llm_max_candidates,
    )
    assemble = stable_fingerprint(
        scope="analysis.assemble",
        version=_ARTIFACT_VERSION,
        job_id=request.job_id,
        transcript_sha256=hashes["transcript"],
        candidates=candidates,
        llm=llm,
        content_type=request.content_type.value,
        config_sha256=hashes["config"],
    )
    return {
        "signals.audio": audio,
        "signals.scene": scene,
        "signals.motion": motion,
        "candidates": candidates,
        "llm": llm,
        "assemble": assemble,
    }


def _outcome_sha256(outcome: LlmOutcome) -> str:
    return _hash_value(outcome.model_dump(mode="json"))


def _signal_failure(note: str | None) -> AIError | None:
    if note is None:
        return None
    if note.startswith("No audio stream") or note.startswith("No video stream"):
        return None
    lowered = note.lower()
    if "not installed" in lowered:
        return AIError(
            ErrorCategory.INVALID_REQUEST,
            note,
            retryable=False,
            details={"reason": "ffmpeg_missing"},
        )
    if "timed out" in lowered or "timeout" in lowered:
        return AIError(
            ErrorCategory.NETWORK_TIMEOUT,
            note,
            retryable=True,
            details={"reason": "ffmpeg_timeout"},
        )
    if "could not" in lowered or "failed" in lowered:
        return AIError(
            ErrorCategory.INTERNAL_ERROR,
            note,
            retryable=True,
            details={"reason": "ffmpeg_failed"},
        )
    return None


def prepare_candidate_artifact(
    *,
    job_id: str,
    media_path: Path,
    metadata: VideoMetadata,
    transcript: TranscriptResult | None,
    config: HighlightSettings,
    content_type: ContentType,
    manager: ProviderManager | None,
    signals: MediaSignals,
    source_hint: str | None,
) -> CandidateArtifact:
    preparation = prepare_highlights(
        job_id=job_id,
        media_path=media_path,
        metadata=metadata,
        transcript=transcript,
        config=config,
        content_type=content_type,
        manager=manager,
        signals=signals,
        source_hint=source_hint,
    )
    return CandidateArtifact(
        job_id=job_id,
        notes=list(preparation.notes),
        classification=preparation.classification,
        candidates=[
            CandidatePayload.from_candidate(candidate) for candidate in preparation.candidates
        ],
        final_result=preparation.result,
    )


def run_llm_stage(
    manager: ProviderManager | None,
    *,
    transcript: TranscriptResult | None,
    candidates: list[Candidate],
    content_type: ContentType,
    signals: MediaSignals,
    config: HighlightSettings,
    provider: str | None,
    model: str | None,
) -> LlmStageResult:
    if transcript is None or not candidates or not config.use_llm:
        return LlmStageResult(LlmOutcome())
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
    error: AIError | None = None
    if outcome.failed:
        category = ErrorCategory.INTERNAL_ERROR if outcome.retryable else ErrorCategory.POLICY_VIOLATION
        error = AIError(
            category,
            outcome.error or "LLM refinement failed.",
            provider=outcome.provider,
            model=outcome.model,
            retryable=outcome.retryable,
            retry_after=outcome.retry_after,
            details={"reason": "llm_failed"},
        )
    return LlmStageResult(outcome=outcome, error=error)
