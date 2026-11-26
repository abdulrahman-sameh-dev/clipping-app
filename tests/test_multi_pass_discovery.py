from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from clipping_ai.analysis.config import (
    AnalysisConfig,
    HighlightSettings,
    MultiPassSettings,
    TranscriptionSettings,
)
from clipping_ai.analysis.recovery import (
    ANALYSIS_JOB_TYPE,
    STAGE_DISCOVERY_AGGREGATE,
    STAGE_DISCOVERY_WINDOW,
)
from clipping_ai.analysis.service import AnalysisService
from clipping_ai.api.app import AnalyzeJobRequest, HighlightJobRequest, create_app
from clipping_ai.cli import build_parser
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.highlights.models import (
    AudioWindow,
    Highlight,
    MediaSignals,
    MotionSample,
    SceneChange,
)
from clipping_ai.highlights.multipass import (
    plan_discovery_windows,
    select_diverse_highlights,
    window_signals,
    window_transcript,
)
from clipping_ai.highlights.observability import StageEvent
from clipping_ai.schemas import TranscriptResult, TranscriptSegment
from tests.video_helpers import make_service, make_test_video, requires_ffmpeg


class RecordingObserver:
    def __init__(self) -> None:
        self.events: list[StageEvent] = []

    def record(self, event: StageEvent) -> None:
        self.events.append(event)


class WindowManager:
    def __init__(self, *, fail_window: int | None = None, settings=None) -> None:
        self.settings = settings
        self.analyze_calls = 0
        self.transcribe_calls = 0
        self.fail_window = fail_window
        self.windows: list[tuple[float, float]] = []

    def transcribe(self, *args, **kwargs):
        self.transcribe_calls += 1
        return {
            "provider": "whisper_cpp",
            "model": "whisper-tiny",
            "transcript": _transcript().model_dump(mode="json"),
        }

    def analyze(self, *args, **kwargs):
        self.analyze_calls += 1
        payload = kwargs.get("payload") or {}
        candidates = payload.get("candidate_windows") or []
        start = min(float(item["start_time"]) for item in candidates) if candidates else 0.0
        end = max(float(item["end_time"]) for item in candidates) if candidates else 1.0
        self.windows.append((start, end))
        if self.fail_window is not None and int(round(start)) == self.fail_window:
            raise AIError(
                ErrorCategory.PROVIDER_UNAVAILABLE,
                "temporary provider failure",
                retryable=True,
                details={"reason": "test_failure"},
            )
        return {
            "data": {
                "highlights": [
                    {
                        "start_time": start,
                        "end_time": end,
                        "suggested_title": f"Moment at {int(start)}",
                        "summary": f"Complete moment at {int(start)} seconds.",
                        "reasoning": f"The transcript highlights this moment at {int(start)}.",
                        "score": 90.0,
                        "category": f"topic_{int(start)}",
                        "transcript_excerpt": "excerpt",
                    }
                ]
            },
            "provider": "fake",
            "model": "fake-v1",
        }


def _config(
    *,
    multi_pass: bool = False,
    window_seconds: float = 1.0,
    overlap_seconds: float = 0.0,
    use_llm: bool = True,
    max_highlights: int = 3,
) -> AnalysisConfig:
    return AnalysisConfig(
        transcription=TranscriptionSettings(min_word_probability=0.5),
        highlights=HighlightSettings(
            min_clip_seconds=0.5,
            max_clip_seconds=1.0,
            candidate_window_seconds=1.0,
            candidate_stride_seconds=0.5,
            min_confidence=0.05,
            use_llm=use_llm,
            max_highlights=max_highlights,
        ),
        multi_pass=MultiPassSettings(
            enabled=multi_pass,
            window_seconds=window_seconds,
            overlap_seconds=overlap_seconds,
        ),
    )


def _transcript() -> TranscriptResult:
    return TranscriptResult(
        text=" ".join(
            [
                "Why did the game change?",
                "Because we won the final round!",
                "What happened next?",
            ]
        ),
        language="en",
        duration=3.0,
        segments=[
            TranscriptSegment(
                id=0, start=0.0, end=1.0, text="Why did the game change?"
            ),
            TranscriptSegment(
                id=1, start=1.0, end=2.0, text="Because we won the final round!"
            ),
            TranscriptSegment(id=2, start=2.0, end=3.0, text="What happened next?"),
        ],
    )


def _ready_job(tmp_path: Path, config: AnalysisConfig):
    video = make_service(tmp_path)
    job = video.prepare_local(make_test_video(tmp_path / "sample.mp4", duration=3.0))
    job = video.store.update(job.id, transcript=_transcript())
    manager = WindowManager()
    service = AnalysisService(
        video.settings,
        config=config,
        video_config=video.config,
        store=video.store,
        manager=manager,  # type: ignore[arg-type]
    )
    return video, job, manager, service


def _patch_signals(monkeypatch) -> None:
    monkeypatch.setattr(
        "clipping_ai.analysis.recovery.analyze_audio_signals",
        lambda *args, **kwargs: ([], None),
    )
    monkeypatch.setattr(
        "clipping_ai.analysis.recovery.analyze_scene_changes",
        lambda *args, **kwargs: ([], None),
    )
    monkeypatch.setattr(
        "clipping_ai.analysis.recovery.analyze_motion",
        lambda *args, **kwargs: ([], None),
    )


def _window_records(service: AnalysisService, job_id: str):
    return [
        record
        for record in service.stage_store.list_stages(
            job_type=ANALYSIS_JOB_TYPE, job_id=job_id
        )
        if record.stage == STAGE_DISCOVERY_WINDOW
    ]


def test_multi_pass_settings_default_off_and_validate_overlap():
    assert AnalysisConfig().multi_pass.enabled is False
    assert AnalysisConfig().multi_pass.window_seconds == 600.0
    assert AnalysisConfig().multi_pass.overlap_seconds == 0.0
    with pytest.raises(ValidationError):
        MultiPassSettings(window_seconds=60.0, overlap_seconds=60.0)


def test_plan_discovery_windows_is_deterministic_and_handles_partial_final_window():
    windows = plan_discovery_windows(1300.0, 600.0, 0.0)
    assert [(item.start, item.end) for item in windows] == [
        (0.0, 600.0),
        (600.0, 1200.0),
        (1200.0, 1300.0),
    ]
    assert [item.window_id for item in windows] == [
        "w0000_000000_000600",
        "w0001_000600_001200",
        "w0002_001200_001300",
    ]
    assert windows == plan_discovery_windows(1300.0, 600.0, 0.0)

    overlapped = plan_discovery_windows(1250.0, 600.0, 50.0)
    assert [(item.start, item.end) for item in overlapped] == [
        (0.0, 600.0),
        (550.0, 1150.0),
        (1100.0, 1250.0),
    ]


def test_window_transcript_keeps_absolute_timestamps_and_whole_crossing_segments():
    transcript = TranscriptResult(
        text="all",
        language="en",
        duration=1300.0,
        segments=[
            TranscriptSegment(id=0, start=0.0, end=2.0, text="before"),
            TranscriptSegment(id=1, start=598.0, end=602.0, text="crossing one"),
            TranscriptSegment(id=2, start=601.0, end=603.0, text="crossing two"),
            TranscriptSegment(id=3, start=1200.0, end=1202.0, text="after"),
        ],
    )
    window = plan_discovery_windows(1300.0, 600.0, 0.0)[1]
    result = window_transcript(transcript, window)
    assert result.language == "en"
    assert result.duration == window.end
    assert [segment.id for segment in result.segments] == [1, 2]
    assert [segment.start for segment in result.segments] == [598.0, 601.0]
    assert "crossing one" in result.text
    assert "before" not in result.text
    assert "after" not in result.text


def test_window_signals_filters_by_absolute_time_without_rescaling():
    signals = MediaSignals(
        duration=1300.0,
        audio_windows=[
            AudioWindow(start=0.0, end=1.0, rms_dbfs=-20, peak_dbfs=-10, energy=0.5),
            AudioWindow(start=599.0, end=601.0, rms_dbfs=-20, peak_dbfs=-10, energy=0.5),
            AudioWindow(start=1300.0, end=1301.0, rms_dbfs=-20, peak_dbfs=-10, energy=0.5),
        ],
        scene_changes=[
            SceneChange(time=10.0, score=0.5),
            SceneChange(time=700.0, score=0.8),
        ],
        motion_samples=[MotionSample(time=650.0, score=0.7), MotionSample(time=1299.0, score=0.2)],
        audio_analyzed=True,
        video_analyzed=True,
    )
    window = plan_discovery_windows(1300.0, 600.0, 0.0)[1]
    result = window_signals(signals, window)
    assert [item.start for item in result.audio_windows] == [599.0]
    assert [item.time for item in result.scene_changes] == [700.0]
    assert [item.time for item in result.motion_samples] == [650.0]
    assert result.audio_analyzed is True
    assert result.duration == window.end


def test_select_diverse_highlights_applies_final_limit_only():
    config = _config().highlights
    highlights = [
        Highlight(
            start_time=0.0,
            end_time=1.0,
            title="first",
            description="first",
            selection_reason="first",
            confidence=0.9,
            category="education",
        ),
        Highlight(
            start_time=0.1,
            end_time=1.1,
            title="overlap",
            description="overlap",
            selection_reason="overlap",
            confidence=0.88,
            category="education",
        ),
        Highlight(
            start_time=20.0,
            end_time=21.0,
            title="third",
            description="third",
            selection_reason="third",
            confidence=0.7,
            category="gaming",
        ),
        Highlight(
            start_time=40.0,
            end_time=41.0,
            title="fourth",
            description="fourth",
            selection_reason="fourth",
            confidence=0.6,
            category="gaming",
        ),
    ]
    selected = select_diverse_highlights(highlights, 2, config)
    assert [item.title for item in selected] == ["first", "third"]


@requires_ffmpeg
def test_multipass_is_opt_in_and_default_path_stays_single_pass(tmp_path, monkeypatch):
    _, job, manager, service = _ready_job(tmp_path, _config())
    _patch_signals(monkeypatch)

    service.highlights(job.id, use_llm=False)
    assert _window_records(service, job.id) == []
    assert manager.analyze_calls == 0

    service = AnalysisService(
        service.video_settings if hasattr(service, "video_settings") else service.settings,
        config=_config(),
        video_config=service.video_config,
        store=service.store,
        manager=manager,  # type: ignore[arg-type]
    )
    result = service.highlights(job.id, use_llm=False, multi_pass=True, refresh=True)
    assert result.highlight_result is not None
    assert len(_window_records(service, job.id)) == 3


@requires_ffmpeg
def test_multipass_resume_reruns_only_invalid_windows_and_reuses_llm_artifacts(
    tmp_path, monkeypatch
):
    _, job, manager, service = _ready_job(tmp_path, _config(multi_pass=True))
    _patch_signals(monkeypatch)

    first = service.highlights(job.id)
    assert first.highlight_result is not None
    expected = first.highlight_result.model_dump(mode="json")
    assert manager.analyze_calls == 3
    assert manager.windows == [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)]
    assert len(_window_records(service, job.id)) == 3

    service.store.update(job.id, highlight_result=None)
    second = service.highlights(job.id)
    assert second.highlight_result is not None
    assert second.highlight_result.model_dump(mode="json") == expected
    assert manager.analyze_calls == 3

    records = _window_records(service, job.id)
    record = records[1]
    assert record.artifact_path is not None
    Path(record.artifact_path).write_text("{not-json", encoding="utf-8")
    aggregate = service.stage_store.get(
        job_type=ANALYSIS_JOB_TYPE,
        job_id=job.id,
        stage=STAGE_DISCOVERY_AGGREGATE,
    )
    assert aggregate is not None and aggregate.artifact_path is not None
    Path(aggregate.artifact_path).write_text("{not-json", encoding="utf-8")
    service.store.update(job.id, highlight_result=None)

    third = service.highlights(job.id)
    assert third.highlight_result is not None
    assert third.highlight_result.model_dump(mode="json") == expected
    assert manager.analyze_calls == 3
    repaired = Path(record.artifact_path).read_text(encoding="utf-8")
    assert json.loads(repaired)["stage"] == STAGE_DISCOVERY_WINDOW


@requires_ffmpeg
def test_failed_window_preserves_completed_windows_and_is_retried_alone(
    tmp_path, monkeypatch
):
    video, job, _, service = _ready_job(tmp_path, _config(multi_pass=True))
    _patch_signals(monkeypatch)
    failing_manager = WindowManager(fail_window=1)
    service = AnalysisService(
        video.settings,
        config=_config(multi_pass=True),
        video_config=video.config,
        store=video.store,
        manager=failing_manager,  # type: ignore[arg-type]
    )

    with pytest.raises(AIError):
        service.highlights(job.id)

    records = {item.unit_key: item for item in _window_records(service, job.id)}
    assert len(records) == 2
    done_keys = [key for key, item in records.items() if item.status.value == "done"]
    failed_or_pending = [key for key, item in records.items() if item.status.value != "done"]
    assert len(done_keys) == 1
    assert len(failed_or_pending) == 1
    assert service.stage_store.get(
        job_type=ANALYSIS_JOB_TYPE,
        job_id=job.id,
        stage=STAGE_DISCOVERY_AGGREGATE,
    ) is None

    failing_manager.fail_window = None
    result = service.highlights(job.id)
    assert result.highlight_result is not None
    assert failing_manager.windows == [(0.0, 1.0), (1.0, 2.0), (1.0, 2.0), (2.0, 3.0)]
    assert all(
        item.status.value == "done" for item in _window_records(service, job.id)
    )


@requires_ffmpeg
def test_progress_and_observer_report_window_discovery(tmp_path, monkeypatch):
    _, job, _, service = _ready_job(tmp_path, _config(multi_pass=True))
    _patch_signals(monkeypatch)
    observer = RecordingObserver()

    service.highlights(job.id, observer=observer)
    progress = service.discovery_progress(job.id)
    assert progress["total"] == 3
    assert progress["complete"] == 3
    assert [item["status"] for item in progress["windows"]] == ["complete"] * 3
    assert "multi_pass_windows" in {event.stage for event in observer.events}
    assert "multi_pass_aggregate" in {event.stage for event in observer.events}


@requires_ffmpeg
def test_api_reports_progress_and_accepts_multipass_options(tmp_path):
    source = make_test_video(tmp_path / "sample.mp4", duration=3.0)
    video = make_service(tmp_path)
    manager = WindowManager(settings=video.settings)
    analysis = AnalysisService(
        video.settings,
        config=_config(),
        video_config=video.config,
        store=video.store,
        manager=manager,  # type: ignore[arg-type]
    )
    app = create_app(
        manager,  # type: ignore[arg-type]
        video_service=video,
        analysis_service=analysis,
        start_worker=False,
    )
    client = TestClient(app)
    upload = client.post(
        "/api/video/upload",
        files={"file": ("sample.mp4", source.read_bytes(), "video/mp4")},
    )
    assert upload.status_code == 200
    job_id = upload.json()["job"]["id"]
    video.process(job_id)

    response = client.post(
        f"/api/video/jobs/{job_id}/highlights",
        json={
            "multi_pass": True,
            "window_seconds": 1.0,
            "overlap_seconds": 0.0,
            "max_highlights": 2,
            "use_llm": False,
        },
    )
    assert response.status_code == 200
    stored = client.get(f"/api/video/jobs/{job_id}/highlights")
    assert stored.status_code == 200
    assert stored.json()["discovery_progress"]["total"] == 3
    assert stored.json()["discovery_progress"]["complete"] == 3


def test_request_models_and_cli_flags_accept_multipass_options():
    request = HighlightJobRequest(
        multi_pass=True,
        window_seconds=30.0,
        overlap_seconds=5.0,
        max_highlights=4,
    )
    assert request.multi_pass is True
    assert request.window_seconds == 30.0
    assert request.overlap_seconds == 5.0
    assert request.max_highlights == 4
    assert AnalyzeJobRequest().multi_pass is None

    parser = build_parser()
    args = parser.parse_args(
        [
            "analyze-job",
            "job-1",
            "--multi-pass",
            "--window-seconds",
            "30",
            "--overlap-seconds",
            "5",
            "--max-highlights",
            "4",
        ]
    )
    assert args.multi_pass is True
    assert args.window_seconds == 30.0
    assert args.overlap_seconds == 5.0
    assert args.max_highlights == 4
