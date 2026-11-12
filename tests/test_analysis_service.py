from __future__ import annotations

import sqlite3

import pytest

from clipping_ai.analysis.config import (
    AnalysisConfig,
    HighlightSettings,
    TranscriptionSettings,
)
from clipping_ai.analysis.service import AnalysisService
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.schemas import TranscriptResult, TranscriptSegment, TranscriptWord
from clipping_ai.settings import Settings
from clipping_ai.video.store import VideoJobStore
from tests.video_helpers import make_service, make_test_video, requires_ffmpeg


class FakeManager:
    def __init__(
        self,
        *,
        transcript: dict | None = None,
        analysis: dict | None = None,
        transcribe_error: AIError | None = None,
    ) -> None:
        self.transcript = transcript or {
            "text": "This is a complete moment about the game.",
            "language": "en",
            "duration": 2.0,
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "This is a complete moment"},
                {"start": 1.0, "end": 2.0, "text": "about the game."},
            ],
        }
        self.analysis = analysis
        self.transcribe_error = transcribe_error
        self.transcribe_calls = 0
        self.analyze_calls = 0

    def transcribe(self, *args, **kwargs):
        self.transcribe_calls += 1
        if self.transcribe_error is not None:
            raise self.transcribe_error
        return {
            "provider": "whisper_cpp",
            "model": "whisper-tiny",
            "transcript": self.transcript,
        }

    def analyze(self, *args, **kwargs):
        self.analyze_calls += 1
        return self.analysis or {"data": {"highlights": []}}


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


def _analysis_service(tmp_path, video, manager, *, config: AnalysisConfig | None = None):
    return AnalysisService(
        video.settings,
        config=config or _config(),
        video_config=video.config,
        store=video.store,
        manager=manager,  # type: ignore[arg-type]
    )


@requires_ffmpeg
def test_transcribe_persists_and_normalises_transcript(tmp_path):
    video = make_service(tmp_path)
    job = video.prepare_local(make_test_video(tmp_path / "sample.mp4", duration=2.0))
    manager = FakeManager(
        transcript={
            "text": "A sentence that is longer than the media.",
            "language": "en",
            "duration": 30.0,
            "segments": [
                {
                    "start": 0.0,
                    "end": 30.0,
                    "text": "A sentence that is longer than the media.",
                    "words": [
                        {"word": "A", "start": 0.0, "end": 0.4, "probability": 0.9},
                        {"word": "low", "start": 0.4, "end": 0.8, "probability": 0.1},
                    ],
                }
            ],
            "words": [
                {"word": "A", "start": 0.0, "end": 0.4, "probability": 0.9},
                {"word": "low", "start": 0.4, "end": 0.8, "probability": 0.1},
            ],
        }
    )
    service = _analysis_service(tmp_path, video, manager)
    updated = service.transcribe(job.id)

    assert updated.transcript is not None
    assert updated.transcript.duration == pytest.approx(2.0)
    assert updated.transcript.segments[0].end == pytest.approx(2.0)
    assert updated.transcript.segments[0].words is not None
    assert [word.word for word in updated.transcript.segments[0].words] == ["A"]
    assert updated.transcript.words
    assert [word.word for word in updated.transcript.words] == ["A"]
    assert updated.transcript_provider == "whisper_cpp"
    assert updated.transcript_model == "whisper-tiny"
    assert updated.transcript_error is None

    service.transcribe(job.id)
    assert manager.transcribe_calls == 1


@requires_ffmpeg
def test_transcribe_records_provider_errors(tmp_path):
    video = make_service(tmp_path)
    job = video.prepare_local(make_test_video(tmp_path / "sample.mp4", duration=2.0))
    manager = FakeManager(
        transcribe_error=AIError(
            ErrorCategory.PROVIDER_UNAVAILABLE,
            "whisper.cpp is not available",
            retryable=False,
        )
    )
    service = _analysis_service(tmp_path, video, manager)
    with pytest.raises(AIError):
        service.transcribe(job.id)
    stored = video.store.require(job.id)
    assert stored.transcript is None
    assert stored.transcript_error == "whisper.cpp is not available"


@requires_ffmpeg
def test_highlights_and_analyze_persist_results(tmp_path):
    video = make_service(tmp_path)
    job = video.prepare_local(make_test_video(tmp_path / "sample.mp4", duration=3.0))
    manager = FakeManager(
        transcript={
            "text": "Why did the game change? Because we won the final round!",
            "language": "en",
            "duration": 3.0,
            "segments": [
                {"start": 0.0, "end": 1.5, "text": "Why did the game change?"},
                {"start": 1.5, "end": 3.0, "text": "Because we won the final round!"},
            ],
        }
    )
    service = _analysis_service(tmp_path, video, manager)

    highlighted = service.highlights(job.id, use_llm=False)
    assert highlighted.highlight_result is not None
    assert highlighted.highlight_result.highlights
    assert highlighted.highlight_result.method == "deterministic"
    for highlight in highlighted.highlight_result.highlights:
        assert 0 <= highlight.start_time < highlight.end_time <= 3.0

    analyzed = service.analyze(job.id, use_llm=False)
    assert analyzed.transcript is not None
    assert analyzed.highlight_result is not None
    assert manager.transcribe_calls == 1
    assert manager.analyze_calls == 0


@requires_ffmpeg
def test_highlights_and_analyze_share_semantic_consolidation(tmp_path):
    video = make_service(tmp_path)
    job = video.prepare_local(make_test_video(tmp_path / "sample.mp4", duration=3.0))
    manager = FakeManager(
        transcript={
            "text": (
                "This example explains why the cache fails. "
                "The cache keeps stale data because the eviction rule is wrong. "
                "So the system fails when the cache is cleared."
            ),
            "language": "en",
            "duration": 3.0,
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "This example explains why the cache fails."},
                {
                    "start": 1.0,
                    "end": 2.0,
                    "text": "The cache keeps stale data because the eviction rule is wrong.",
                },
                {
                    "start": 2.0,
                    "end": 3.0,
                    "text": "So the system fails when the cache is cleared.",
                },
            ],
        }
    )
    service = _analysis_service(
        tmp_path,
        video,
        manager,
        config=_config(
            min_clip_seconds=1.0,
            max_clip_seconds=5.0,
            candidate_window_seconds=5.0,
            candidate_stride_seconds=2.0,
            semantic_merge_gap_seconds=1.0,
            semantic_bridge_gap_seconds=2.0,
        ),
    )

    standalone = service.highlights(job.id, use_llm=False)
    analyzed = service.analyze(job.id, use_llm=False, refresh=True)

    assert standalone.highlight_result is not None
    assert analyzed.highlight_result is not None
    standalone_ranges = [
        (highlight.start_time, highlight.end_time)
        for highlight in standalone.highlight_result.highlights
    ]
    analyzed_ranges = [
        (highlight.start_time, highlight.end_time)
        for highlight in analyzed.highlight_result.highlights
    ]
    assert standalone_ranges == analyzed_ranges
    assert analyzed_ranges == [(0.0, 3.0)]


@requires_ffmpeg
def test_analyze_continues_when_transcription_fails(tmp_path):
    video = make_service(tmp_path)
    job = video.prepare_local(make_test_video(tmp_path / "sample.mp4", duration=2.0))
    manager = FakeManager(
        transcribe_error=AIError(
            ErrorCategory.PROVIDER_UNAVAILABLE,
            "offline",
            retryable=False,
        )
    )
    service = _analysis_service(tmp_path, video, manager)
    analyzed = service.analyze(job.id, use_llm=False)
    assert analyzed.transcript is None
    assert analyzed.transcript_error == "offline"
    assert analyzed.highlight_result is not None


def test_normalise_filters_words_when_clamping_is_disabled(tmp_path):
    settings = Settings(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    settings.ensure_dirs()
    service = AnalysisService(
        settings,
        config=AnalysisConfig(
            transcription=TranscriptionSettings(
                clamp_timestamps=False,
                max_transcript_chars=4,
                min_word_probability=0.5,
            ),
            highlights=HighlightSettings(),
        ),
    )
    transcript = TranscriptResult(
        text="keep drop",
        duration=10.0,
        segments=[
            TranscriptSegment(
                start=0.0,
                end=10.0,
                text="keep drop",
                words=[
                    TranscriptWord(word="keep", start=0.0, end=1.0, probability=0.9),
                    TranscriptWord(word="drop", start=1.0, end=2.0, probability=0.1),
                ],
            )
        ],
        words=[
            TranscriptWord(word="keep", start=0.0, end=1.0, probability=0.9),
            TranscriptWord(word="drop", start=1.0, end=2.0, probability=0.1),
        ],
    )
    normalised = service._normalise_transcript(transcript, duration=2.0)
    assert normalised.duration == 10.0
    assert normalised.text == "keep"
    assert [word.word for word in normalised.words] == ["keep"]
    assert normalised.segments[0].words is not None
    assert [word.word for word in normalised.segments[0].words] == ["keep"]


def test_store_migrates_existing_video_jobs_table(tmp_path):
    db_path = tmp_path / "jobs.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE video_jobs (
                id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_value TEXT NOT NULL,
                original_filename TEXT,
                source_hash TEXT,
                input_path TEXT,
                metadata_json TEXT,
                audio_path TEXT,
                audio_chunks_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0,
                error_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            )
            """
        )
    store = VideoJobStore(db_path)
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(video_jobs)")}
    assert {
        "transcript_json",
        "transcript_error",
        "transcript_provider",
        "transcript_model",
        "transcript_at",
        "highlight_json",
        "highlight_error",
        "highlight_at",
    } <= columns
    assert store.count() == 0
