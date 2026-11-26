from __future__ import annotations

from pathlib import Path

import pytest

from clipping_ai.analysis.config import (
    AnalysisConfig,
    HighlightSettings,
    TranscriptionSettings,
)
from clipping_ai.analysis.recovery import ANALYSIS_JOB_TYPE, STAGE_LLM, STAGE_SIGNALS_AUDIO
from clipping_ai.analysis.service import AnalysisService
from clipping_ai.api.app import create_app
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.highlights.models import AudioWindow
from clipping_ai.recovery.stages import StageStatus
from clipping_ai.schemas import TranscriptResult, TranscriptSegment
from tests.test_analysis_service import FakeManager
from tests.video_helpers import make_service, make_test_video, requires_ffmpeg


def _config(**highlight_overrides) -> AnalysisConfig:
    defaults = {
        "min_clip_seconds": 1.0,
        "max_clip_seconds": 5.0,
        "candidate_window_seconds": 5.0,
        "candidate_stride_seconds": 2.0,
        "use_llm": False,
        "min_confidence": 0.1,
    }
    defaults.update(highlight_overrides)
    return AnalysisConfig(
        transcription=TranscriptionSettings(min_word_probability=0.5),
        highlights=HighlightSettings(**defaults),
    )


def _service(video, manager, *, config: AnalysisConfig | None = None) -> AnalysisService:
    return AnalysisService(
        video.settings,
        config=config or _config(),
        video_config=video.config,
        store=video.store,
        manager=manager,  # type: ignore[arg-type]
    )


def _ready_job(tmp_path: Path):
    video = make_service(tmp_path)
    job = video.prepare_local(make_test_video(tmp_path / "sample.mp4", duration=3.0))
    return video, job


def _transcript() -> TranscriptResult:
    return TranscriptResult(
        text="Why did the game change? Because we won the final round!",
        language="en",
        duration=3.0,
        segments=[
            TranscriptSegment(start=0.0, end=1.5, text="Why did the game change?"),
            TranscriptSegment(start=1.5, end=3.0, text="Because we won the final round!"),
        ],
    )


def _with_transcript(video, job):
    return video.store.update(job.id, transcript=_transcript())


def _patch_signals(monkeypatch, *, audio_note: str | None = None) -> dict[str, int]:
    calls = {"audio": 0, "scene": 0, "motion": 0}

    def audio(*args, **kwargs):
        calls["audio"] += 1
        windows = (
            [AudioWindow(start=0.0, end=0.5, rms_dbfs=-20.0, peak_dbfs=-10.0, energy=0.7)]
            if audio_note
            else []
        )
        return windows, audio_note

    def scene(*args, **kwargs):
        calls["scene"] += 1
        return [], None

    def motion(*args, **kwargs):
        calls["motion"] += 1
        return [], None

    monkeypatch.setattr("clipping_ai.analysis.recovery.analyze_audio_signals", audio)
    monkeypatch.setattr("clipping_ai.analysis.recovery.analyze_scene_changes", scene)
    monkeypatch.setattr("clipping_ai.analysis.recovery.analyze_motion", motion)
    return calls


class FailingAnalyzeManager(FakeManager):
    def __init__(self) -> None:
        super().__init__()
        self.analyze_calls = 0

    def analyze(self, *args, **kwargs):
        self.analyze_calls += 1
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "llm provider rejected the request",
            retryable=False,
        )


@requires_ffmpeg
def test_analysis_artifacts_survive_lost_database_update(tmp_path, monkeypatch):
    video, job = _ready_job(tmp_path)
    _with_transcript(video, job)
    manager = FakeManager()
    service = _service(video, manager)
    signal_calls = _patch_signals(monkeypatch)

    first = service.highlights(job.id, use_llm=False)
    assert first.highlight_result is not None
    expected = first.highlight_result.model_dump(mode="json")
    after_first = dict(signal_calls)

    video.store.update(job.id, highlight_result=None)
    original_update = video.store.update

    def fail_update(*args, **kwargs):
        if "highlight_result" in kwargs:
            raise RuntimeError("database unavailable")
        return original_update(*args, **kwargs)

    monkeypatch.setattr(video.store, "update", fail_update)
    with pytest.raises(AIError):
        service.highlights(job.id, use_llm=False)
    monkeypatch.setattr(video.store, "update", original_update)

    second = service.highlights(job.id, use_llm=False)
    assert second.highlight_result is not None
    assert second.highlight_result.model_dump(mode="json") == expected
    assert signal_calls == after_first
    assert manager.analyze_calls == 0


@requires_ffmpeg
def test_corrupt_assemble_artifact_is_recomputed_without_replaying_signals(
    tmp_path, monkeypatch
):
    video, job = _ready_job(tmp_path)
    _with_transcript(video, job)
    service = _service(video, FakeManager())
    signal_calls = _patch_signals(monkeypatch)

    first = service.highlights(job.id, use_llm=False)
    assert first.highlight_result is not None
    expected = first.highlight_result.model_dump(mode="json")
    after_first = dict(signal_calls)

    artifact = video.settings.data_dir / "videos" / "jobs" / job.id / "analysis" / "assemble.json"
    artifact.write_text("{not-json", encoding="utf-8")
    video.store.update(job.id, highlight_result=None)

    second = service.highlights(job.id, use_llm=False)
    assert second.highlight_result is not None
    assert second.highlight_result.model_dump(mode="json") == expected
    assert signal_calls == after_first


@requires_ffmpeg
def test_failed_llm_stage_is_not_done_and_fallback_is_reused(tmp_path, monkeypatch):
    video, job = _ready_job(tmp_path)
    _with_transcript(video, job)
    manager = FailingAnalyzeManager()
    service = _service(video, manager, config=_config(use_llm=True))
    signal_calls = _patch_signals(monkeypatch)

    first = service.highlights(job.id)
    assert first.highlight_result is not None
    assert first.highlight_result.method == "deterministic"
    expected = first.highlight_result.model_dump(mode="json")
    assert manager.analyze_calls == 1

    llm_stage = service.stage_store.get(
        job_type=ANALYSIS_JOB_TYPE,
        job_id=job.id,
        stage=STAGE_LLM,
    )
    assert llm_stage is not None
    assert llm_stage.status is not StageStatus.DONE
    assert isinstance(llm_stage.checkpoint, dict)
    assert llm_stage.checkpoint.get("status") == "fallback"

    video.store.update(job.id, highlight_result=None)
    after_first = dict(signal_calls)
    second = service.highlights(job.id)
    assert second.highlight_result is not None
    assert second.highlight_result.model_dump(mode="json") == expected
    assert manager.analyze_calls == 1
    assert signal_calls == after_first


@requires_ffmpeg
def test_refresh_invalidates_analysis_stages(tmp_path, monkeypatch):
    video, job = _ready_job(tmp_path)
    _with_transcript(video, job)
    service = _service(video, FakeManager())
    signal_calls = _patch_signals(monkeypatch)

    service.highlights(job.id, use_llm=False)
    after_first = dict(signal_calls)
    service.highlights(job.id, use_llm=False, refresh=True)

    assert signal_calls["audio"] == after_first["audio"] * 2
    assert signal_calls["scene"] == after_first["scene"] * 2
    assert signal_calls["motion"] == after_first["motion"] * 2


@requires_ffmpeg
def test_partial_ffmpeg_output_is_not_persisted_as_success(tmp_path, monkeypatch):
    video, job = _ready_job(tmp_path)
    _with_transcript(video, job)
    service = _service(video, FakeManager())
    _patch_signals(
        monkeypatch,
        audio_note="Audio activity analysis timed out.",
    )

    with pytest.raises(AIError) as exc:
        service.highlights(job.id, use_llm=False)

    assert exc.value.retryable is True
    stage = service.stage_store.get(
        job_type=ANALYSIS_JOB_TYPE,
        job_id=job.id,
        stage=STAGE_SIGNALS_AUDIO,
    )
    assert stage is not None
    assert stage.status is StageStatus.PENDING
    assert stage.artifact_path is None


@requires_ffmpeg
def test_live_lease_blocks_analysis_stage(tmp_path, monkeypatch):
    video, job = _ready_job(tmp_path)
    _with_transcript(video, job)
    service = _service(video, FakeManager())
    signal_calls = _patch_signals(monkeypatch)

    service.stage_store.ensure(
        job_type=ANALYSIS_JOB_TYPE,
        job_id=job.id,
        stage=STAGE_SIGNALS_AUDIO,
        input_fingerprint="other-fingerprint",
    )
    claimed = service.stage_store.claim_next(
        job_type=ANALYSIS_JOB_TYPE,
        job_id=job.id,
        stage=STAGE_SIGNALS_AUDIO,
        owner="other-worker",
        lease_seconds=300,
    )
    assert claimed is not None

    with pytest.raises(AIError) as exc:
        service.highlights(job.id, use_llm=False)
    assert exc.value.details.get("stage") == STAGE_SIGNALS_AUDIO
    assert signal_calls == {"audio": 0, "scene": 0, "motion": 0}


@requires_ffmpeg
def test_normalized_transcript_artifact_survives_database_loss(tmp_path, monkeypatch):
    video, job = _ready_job(tmp_path)
    manager = FakeManager()
    service = _service(video, manager)
    calls = {"normalise": 0}
    original = service._normalise_transcript

    def counting(value, *, duration):
        calls["normalise"] += 1
        return original(value, duration=duration)

    monkeypatch.setattr(service, "_normalise_transcript", counting)

    first = service.transcribe(job.id)
    assert first.transcript is not None
    video.store.update(job.id, transcript=None)
    manager.transcribe_calls = 0

    second = service.transcribe(job.id)
    assert second.transcript is not None
    assert second.transcript.model_dump(mode="json") == first.transcript.model_dump(mode="json")
    assert manager.transcribe_calls == 1
    assert calls["normalise"] == 1


def test_app_analysis_service_shares_stage_store(tmp_path):
    video = make_service(tmp_path)
    manager = FakeManager()
    manager.settings = video.settings
    app = create_app(
        manager,  # type: ignore[arg-type]
        video_service=video,
        start_worker=False,
        start_clip_worker=False,
    )
    assert app.state.analysis.stage_store is app.state.stage_store
