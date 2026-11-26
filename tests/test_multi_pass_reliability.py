from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from clipping_ai.analysis.recovery import (
    ANALYSIS_JOB_TYPE,
    STAGE_DISCOVERY_AGGREGATE,
    STAGE_DISCOVERY_PLAN,
    STAGE_DISCOVERY_WINDOW,
    CandidateArtifact,
    CandidatePayload,
    DiscoveryWindowArtifact,
)
from clipping_ai.analysis.service import AnalysisService
from clipping_ai.api.app import AnalyzeJobRequest, HighlightJobRequest
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.highlights.models import ContentType, Highlight, LlmOutcome
from clipping_ai.highlights.multipass import aggregate_window_artifacts, plan_discovery_windows
from clipping_ai.recovery.stages import StageStore
from clipping_ai.registry import PolicyConfig, ProviderConfig, RouteCandidate
from tests.helpers import (
    FakeTextProvider,
    completion,
    make_manager,
    make_registry,
    text_model,
)
from tests.test_multi_pass_discovery import (
    _config,
    _patch_signals,
    _ready_job,
    _transcript,
)
from tests.video_helpers import requires_ffmpeg


def _expire_lease(
    store: StageStore,
    job_id: str,
    stage: str,
    unit_key: str = "",
) -> None:
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            UPDATE job_stages
            SET status = ?, lease_owner = ?, lease_expires_at = ?, updated_at = ?
            WHERE job_type = ? AND job_id = ? AND stage = ? AND unit_key = ?
            """,
            (
                "running",
                "dead-worker",
                time.time() - 60.0,
                datetime.now(UTC).isoformat(),
                ANALYSIS_JOB_TYPE,
                job_id,
                stage,
                unit_key,
            ),
        )


def _fresh_service(video, config, manager, store: StageStore) -> AnalysisService:
    return AnalysisService(
        video.settings,
        config=config,
        video_config=video.config,
        store=video.store,
        manager=manager,  # type: ignore[arg-type]
        stage_store=store,
    )


def _provider_config(**overrides: Any) -> ProviderConfig:
    data: dict[str, Any] = {
        "type": "litellm",
        "enabled": True,
        "api_key_env": None,
        "max_retries": 0,
        "requests_per_minute": 0,
        "min_interval_seconds": 0,
    }
    data.update(overrides)
    return ProviderConfig.model_validate(data)


def _real_manager(tmp_path: Path, *, models, routing, providers, policy=None):
    registry = make_registry(
        providers={provider_id: _provider_config() for provider_id in providers},
        models=models,
        routing=routing,
        policy=policy or PolicyConfig(schema_validation_retries=0),
    )
    return make_manager(registry, providers, tmp_path)


def _valid_llm_response(entry, messages):
    content = messages[-1]["content"]
    payload = json.loads(content.split("Input JSON:\n", 1)[1])
    candidates = payload.get("candidate_windows") or []
    start = min(float(item["start_time"]) for item in candidates) if candidates else 0.0
    end = max(float(item["end_time"]) for item in candidates) if candidates else 1.0
    response = {
        "highlights": [
            {
                "start_time": start,
                "end_time": end,
                "suggested_title": f"Moment at {int(start)}",
                "summary": f"Complete moment at {int(start)} seconds.",
                "hook": "Watch this moment.",
                "reasoning": f"The transcript highlights this moment at {int(start)}.",
                "score": 90.0,
                "category": "highlight",
                "transcript_excerpt": "excerpt",
            }
        ],
        "language": "en",
        "notes": None,
    }
    return completion(
        json.dumps(response),
        provider=entry.provider,
        model=entry.id,
    )


def _highlight(start: float, end: float, title: str, confidence: float) -> Highlight:
    return Highlight(
        start_time=start,
        end_time=end,
        title=title,
        description=title,
        selection_reason=title,
        confidence=confidence,
        category="highlight",
        source="llm",
    )


def _window_artifact(
    window,
    *,
    highlights: list[Highlight] | None = None,
    deterministic: tuple[float, float] | None = None,
    error: str | None = None,
) -> DiscoveryWindowArtifact:
    candidates: list[CandidatePayload] = []
    if deterministic is not None:
        candidates.append(
            CandidatePayload(
                start=deterministic[0],
                end=deterministic[1],
                score=0.6,
                category="general",
                reason="deterministic candidate",
            )
        )
    return DiscoveryWindowArtifact(
        job_id="job-1",
        window=window,
        candidate_artifact=CandidateArtifact(job_id="job-1", candidates=candidates),
        outcome=LlmOutcome(
            highlights=highlights or [],
            error=error,
            applied=bool(highlights),
            failed=bool(error),
            retryable=False,
        ),
    )


@requires_ffmpeg
def test_service_revalidates_stored_result_against_aggregate_artifact(tmp_path, monkeypatch):
    _, job, manager, service = _ready_job(tmp_path, _config(multi_pass=True))
    _patch_signals(monkeypatch)

    first = service.highlights(job.id)
    assert first.highlight_result is not None
    expected = first.highlight_result.model_dump(mode="json")
    assert manager.analyze_calls == 3

    aggregate = service.stage_store.get(
        job_type=ANALYSIS_JOB_TYPE,
        job_id=job.id,
        stage=STAGE_DISCOVERY_AGGREGATE,
    )
    assert aggregate is not None and aggregate.artifact_path is not None
    Path(aggregate.artifact_path).write_text("{not-json", encoding="utf-8")

    second = service.highlights(job.id)
    assert second.highlight_result is not None
    assert second.highlight_result.model_dump(mode="json") == expected
    assert manager.analyze_calls == 3


@requires_ffmpeg
def test_service_recomputes_stale_aggregate_when_one_window_is_invalid(tmp_path, monkeypatch):
    video, job, manager, service = _ready_job(tmp_path, _config(multi_pass=True))
    _patch_signals(monkeypatch)

    first = service.highlights(job.id)
    assert first.highlight_result is not None
    expected = first.highlight_result.model_dump(mode="json")

    records = [
        record
        for record in service.stage_store.list_stages(
            job_type=ANALYSIS_JOB_TYPE, job_id=job.id
        )
        if record.stage == STAGE_DISCOVERY_WINDOW
    ]
    target = next(record for record in records if record.unit_key.endswith("000001"))
    assert target.artifact_path is not None
    Path(target.artifact_path).write_text("{not-json", encoding="utf-8")

    second = service.highlights(job.id)
    assert second.highlight_result is not None
    assert second.highlight_result.model_dump(mode="json") == expected
    assert manager.analyze_calls == 3
    repaired = json.loads(Path(target.artifact_path).read_text(encoding="utf-8"))
    assert repaired["stage"] == STAGE_DISCOVERY_WINDOW
    assert video.store.require(job.id).highlight_result is not None


@requires_ffmpeg
def test_expired_aggregate_lease_is_reclaimed_by_fresh_service(tmp_path, monkeypatch):
    video, job, manager, service = _ready_job(tmp_path, _config(multi_pass=True))
    _patch_signals(monkeypatch)

    first = service.highlights(job.id)
    assert first.highlight_result is not None
    expected = first.highlight_result.model_dump(mode="json")
    before = service.stage_store.get(
        job_type=ANALYSIS_JOB_TYPE,
        job_id=job.id,
        stage=STAGE_DISCOVERY_AGGREGATE,
    )
    assert before is not None

    _expire_lease(service.stage_store, job.id, STAGE_DISCOVERY_AGGREGATE)
    video.store.update(job.id, highlight_result=None)
    fresh = _fresh_service(
        video,
        _config(multi_pass=True),
        manager,
        StageStore(video.settings.jobs_db_path),
    )

    second = fresh.highlights(job.id)
    assert second.highlight_result is not None
    assert second.highlight_result.model_dump(mode="json") == expected
    assert manager.analyze_calls == 3
    after = fresh.stage_store.get(
        job_type=ANALYSIS_JOB_TYPE,
        job_id=job.id,
        stage=STAGE_DISCOVERY_AGGREGATE,
    )
    assert after is not None and after.attempt > before.attempt


@requires_ffmpeg
def test_expired_window_lease_is_reclaimed_by_fresh_service(tmp_path, monkeypatch):
    video, job, manager, service = _ready_job(tmp_path, _config(multi_pass=True))
    _patch_signals(monkeypatch)

    first = service.highlights(job.id)
    assert first.highlight_result is not None
    expected = first.highlight_result.model_dump(mode="json")
    window_id = plan_discovery_windows(3.0, 1.0, 0.0)[1].window_id
    before = service.stage_store.get(
        job_type=ANALYSIS_JOB_TYPE,
        job_id=job.id,
        stage=STAGE_DISCOVERY_WINDOW,
        unit_key=window_id,
    )
    assert before is not None

    _expire_lease(service.stage_store, job.id, STAGE_DISCOVERY_WINDOW, window_id)
    video.store.update(job.id, highlight_result=None)
    fresh = _fresh_service(
        video,
        _config(multi_pass=True),
        manager,
        StageStore(video.settings.jobs_db_path),
    )

    second = fresh.highlights(job.id)
    assert second.highlight_result is not None
    assert second.highlight_result.model_dump(mode="json") == expected
    assert manager.analyze_calls == 3
    after = fresh.stage_store.get(
        job_type=ANALYSIS_JOB_TYPE,
        job_id=job.id,
        stage=STAGE_DISCOVERY_WINDOW,
        unit_key=window_id,
    )
    assert after is not None and after.attempt > before.attempt


@requires_ffmpeg
def test_progress_uses_persisted_effective_plan_and_reused_window_states(tmp_path, monkeypatch):
    video, job, _, service = _ready_job(tmp_path, _config())
    _patch_signals(monkeypatch)

    service.highlights(
        job.id,
        multi_pass=True,
        window_seconds=1.0,
        overlap_seconds=0.0,
    )

    plan = service.stage_store.get(
        job_type=ANALYSIS_JOB_TYPE,
        job_id=job.id,
        stage=STAGE_DISCOVERY_PLAN,
    )
    assert plan is not None and plan.artifact_path is not None
    payload = json.loads(Path(plan.artifact_path).read_text(encoding="utf-8"))["payload"]
    assert payload["multi_pass"]["enabled"] is True
    assert payload["multi_pass"]["window_seconds"] == 1.0
    assert len(payload["windows"]) == 3

    progress = video.store.require(job.id)
    assert progress.highlight_result is not None
    reported = service.discovery_progress(job.id)
    assert reported["total"] == 3
    assert reported["complete"] == 3
    assert reported["reused"] == 3
    assert [item["state"] for item in reported["windows"]] == ["reused"] * 3


@pytest.mark.parametrize("request_model", [HighlightJobRequest, AnalyzeJobRequest])
@pytest.mark.parametrize("overlap", [30.0, 31.0])
def test_api_request_models_reject_overlap_at_or_above_window(request_model, overlap):
    with pytest.raises(ValidationError):
        request_model(window_seconds=30.0, overlap_seconds=overlap)


@requires_ffmpeg
def test_service_converts_invalid_window_settings_to_ai_error(tmp_path):
    _, job, _, service = _ready_job(tmp_path, _config(multi_pass=True))

    with pytest.raises(AIError) as exc:
        service.highlights(job.id, window_seconds=30.0, overlap_seconds=30.0)

    assert exc.value.category is ErrorCategory.INVALID_REQUEST
    assert exc.value.details["validation"]


@requires_ffmpeg
def test_multipass_explicit_paid_model_rejected_without_provider_call(
    tmp_path, monkeypatch
):
    video, job, _, service = _ready_job(tmp_path, _config(multi_pass=True))
    _patch_signals(monkeypatch)
    provider = FakeTextProvider("p1", [])
    manager = _real_manager(
        tmp_path / "manager",
        models=[
            text_model(
                "paid-1",
                provider="p1",
                free=False,
                tasks=["highlight_detection"],
            )
        ],
        routing={"highlight_detection": [RouteCandidate(provider="p1", model="paid-1")]},
        providers={"p1": provider},
    )
    service = _fresh_service(video, _config(multi_pass=True), manager, service.stage_store)

    with pytest.raises(AIError) as exc:
        service.highlights(job.id, model="paid-1")

    assert exc.value.category is ErrorCategory.POLICY_VIOLATION
    assert provider.calls == []


@requires_ffmpeg
def test_multipass_falls_back_between_free_fake_providers(tmp_path, monkeypatch):
    video, job, _, service = _ready_job(tmp_path, _config(multi_pass=True))
    _patch_signals(monkeypatch)
    first = FakeTextProvider(
        "p1",
        [
            AIError(
                ErrorCategory.RATE_LIMIT_EXCEEDED,
                "rate limited",
                retryable=True,
            ),
            _valid_llm_response,
            _valid_llm_response,
            _valid_llm_response,
        ],
    )
    second = FakeTextProvider("p2", [_valid_llm_response] * 4)
    manager = _real_manager(
        tmp_path / "manager",
        models=[
            text_model("free-1", provider="p1", tasks=["highlight_detection"]),
            text_model("free-2", provider="p2", tasks=["highlight_detection"]),
        ],
        routing={
            "highlight_detection": [
                RouteCandidate(provider="p1", model="free-1"),
                RouteCandidate(provider="p2", model="free-2"),
            ]
        },
        providers={"p1": first, "p2": second},
    )
    service = _fresh_service(video, _config(multi_pass=True), manager, service.stage_store)

    result = service.highlights(job.id)

    assert result.highlight_result is not None
    assert first.calls
    assert second.calls
    assert all(item.source == "llm" for item in result.highlight_result.highlights)


def test_aggregate_window_artifacts_combines_llm_and_deterministic_candidates():
    windows = plan_discovery_windows(3.0, 1.0, 0.0)
    artifacts = [
        _window_artifact(
            windows[0],
            highlights=[_highlight(0.0, 1.0, "early", 0.9)],
        ),
        _window_artifact(
            windows[1],
            deterministic=(1.0, 2.0),
            error="provider unavailable",
        ),
        _window_artifact(
            windows[2],
            highlights=[_highlight(2.0, 3.0, "late", 0.8)],
        ),
    ]

    result = aggregate_window_artifacts(
        artifacts,
        job_id="job-1",
        content_type=ContentType.AUTO,
        duration=3.0,
        transcript=_transcript(),
        config=_config().highlights,
    )

    assert [item.start_time for item in result.highlights] == [0.0, 1.0, 2.0]
    assert [item.title for item in result.highlights] == ["early", "General", "late"]
    assert result.method == "llm"
    assert any("provider unavailable" in note for note in result.notes)


@requires_ffmpeg
def test_single_pass_golden_fixture_records_current_behavior(tmp_path, monkeypatch):
    _, job, _, service = _ready_job(tmp_path, _config())
    _patch_signals(monkeypatch)

    result = service.highlights(job.id, use_llm=False)
    assert result.highlight_result is not None
    payload = result.highlight_result.model_dump(mode="json")
    payload["job_id"] = "golden-job"
    fixture = Path(__file__).parent / "fixtures" / "single_pass_golden.json"
    expected = json.loads(fixture.read_text(encoding="utf-8"))

    assert payload == expected
