from __future__ import annotations

import pytest

from clipping_ai.analysis.config import HighlightSettings
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.highlights.candidates import (
    Candidate,
    build_signal_candidates,
    build_transcript_candidates,
    dedupe_candidates,
    infer_content_type,
)
from clipping_ai.highlights.classification import classify_content
from clipping_ai.highlights.engine import detect_highlights
from clipping_ai.highlights.llm import refine_with_llm
from clipping_ai.highlights.models import (
    AudioWindow,
    ContentType,
    Highlight,
    MediaSignals,
    MotionSample,
    SceneChange,
)
from clipping_ai.highlights.semantic import consolidate_highlights
from clipping_ai.highlights.signals import analyze_media_signals
from clipping_ai.schemas import TranscriptResult, TranscriptSegment
from clipping_ai.video.models import VideoMetadata
from tests.video_helpers import make_test_video, requires_ffmpeg


def _settings(**overrides) -> HighlightSettings:
    defaults = {
        "min_clip_seconds": 4,
        "max_clip_seconds": 20,
        "candidate_window_seconds": 12,
        "candidate_stride_seconds": 6,
        "min_confidence": 0.1,
        "scene_threshold": 0.2,
        "audio_window_seconds": 0.25,
    }
    defaults.update(overrides)
    return HighlightSettings(**defaults)


def _metadata(duration: float, *, video: bool = True, audio: bool = True) -> VideoMetadata:
    return VideoMetadata(
        duration_seconds=duration,
        size_bytes=1,
        has_video=video,
        has_audio=audio,
    )


def _transcript(segments: list[tuple[float, float, str]], *, duration: float = 30.0):
    return TranscriptResult(
        text=" ".join(text for _, _, text in segments),
        language="en",
        duration=duration,
        segments=[
            TranscriptSegment(start=start, end=end, text=text)
            for start, end, text in segments
        ],
    )


class FakeManager:
    def __init__(self, response: dict | None = None, error: AIError | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def analyze(self, task: str, **kwargs):
        self.calls.append((task, kwargs))
        if self.error is not None:
            raise self.error
        return self.response or {"data": {"highlights": []}}


@requires_ffmpeg
def test_media_signal_analysis_uses_ffmpeg(tmp_path):
    source = make_test_video(tmp_path / "signals.mp4", duration=2.0)
    metadata = _metadata(2.0)
    signals = analyze_media_signals(
        source,
        metadata,
        config=_settings(),
        ffmpeg_binary="ffmpeg",
    )
    assert signals.audio_analyzed is True
    assert signals.audio_windows
    assert signals.video_analyzed is True
    assert signals.motion_analyzed is True
    assert signals.motion_samples
    assert signals.duration == 2.0


def test_transcript_candidates_support_english_and_arabic():
    config = _settings()
    english = _transcript(
        [
            (0.0, 5.0, "Why did the game change completely?"),
            (5.0, 11.0, "Because the final round was completely different."),
            (12.0, 18.0, "We won the match!"),
        ]
    )
    english_candidates = build_transcript_candidates(
        english, config, content_type=ContentType.PODCAST
    )
    assert english_candidates
    assert any(candidate.category == "question_answer" for candidate in english_candidates)
    assert all(4 <= candidate.end - candidate.start <= 20 for candidate in english_candidates)

    arabic = TranscriptResult(
        text="لماذا حدث هذا؟ لأن المباراة تغيرت. لقد فزنا!",
        language="ar",
        duration=20,
        segments=[
            TranscriptSegment(start=0, end=5, text="لماذا حدث هذا؟"),
            TranscriptSegment(start=5, end=11, text="لأن المباراة تغيرت."),
            TranscriptSegment(start=12, end=18, text="لقد فزنا!"),
        ],
    )
    arabic_candidates = build_transcript_candidates(
        arabic, config, content_type=ContentType.PODCAST
    )
    assert arabic_candidates
    assert any(candidate.category == "question_answer" for candidate in arabic_candidates)
    assert all(candidate.transcript_segments for candidate in arabic_candidates)


def test_signal_candidates_cover_gaming_events_without_speech():
    config = _settings(min_clip_seconds=3, max_clip_seconds=15, event_merge_seconds=2)
    signals = MediaSignals(
        duration=40,
        audio_analyzed=True,
        video_analyzed=True,
        audio_windows=[
            AudioWindow(
                start=float(index),
                end=float(index + 1),
                rms_dbfs=-18.0 if index in {10, 11, 12} else -55.0,
                peak_dbfs=-8.0,
                energy=0.9 if index in {10, 11, 12} else 0.1,
                silence=index not in {10, 11, 12},
            )
            for index in range(40)
        ],
        scene_changes=[SceneChange(time=30.0, score=0.85)],
    )
    candidates = build_signal_candidates(
        signals, None, config, content_type=ContentType.GAMING
    )
    assert candidates
    assert all(candidate.source == "signals" for candidate in candidates)
    assert any(candidate.signal_score and candidate.signal_score > 0.5 for candidate in candidates)
    assert all(3 <= candidate.end - candidate.start <= 15 for candidate in candidates)


def test_infer_content_type_distinguishes_podcast_and_gaming():
    podcast = _transcript([(0.0, 9.0, "This is a podcast episode with a long explanation about the topic.")])
    silent = MediaSignals(
        duration=10,
        audio_windows=[
            AudioWindow(
                start=0,
                end=1,
                rms_dbfs=-70,
                peak_dbfs=-60,
                energy=0.0,
                silence=True,
            )
        ],
    )
    assert infer_content_type(podcast, silent, ContentType.AUTO) == ContentType.PODCAST

    gameplay = MediaSignals(
        duration=10,
        audio_windows=[
            AudioWindow(
                start=float(index),
                end=float(index + 1),
                rms_dbfs=-20,
                peak_dbfs=-10,
                energy=0.9,
            )
            for index in range(10)
        ],
        scene_changes=[SceneChange(time=2, score=0.8), SceneChange(time=7, score=0.9)],
    )
    assert infer_content_type(None, gameplay, ContentType.AUTO) == ContentType.GAMING
    assert infer_content_type(None, gameplay, ContentType.LIVE) == ContentType.LIVE


def _active_audio_windows(
    duration: int,
    *,
    spike_indices: set[int] | None = None,
) -> list[AudioWindow]:
    spike_indices = spike_indices or set()
    return [
        AudioWindow(
            start=float(index),
            end=float(index + 1),
            rms_dbfs=-18.0 if index in spike_indices else -28.0,
            peak_dbfs=-8.0,
            energy=0.95 if index in spike_indices else 0.6,
            silence=False,
        )
        for index in range(duration)
    ]


def test_classification_detects_gaming_from_visual_audio_and_source_hint():
    config = _settings()
    signals = MediaSignals(
        duration=30,
        audio_analyzed=True,
        video_analyzed=True,
        motion_analyzed=True,
        audio_windows=_active_audio_windows(30, spike_indices={10, 11, 20}),
        scene_changes=[
            SceneChange(time=5, score=0.8),
            SceneChange(time=12, score=0.9),
            SceneChange(time=24, score=0.7),
        ],
        motion_samples=[
            MotionSample(time=float(index), score=0.7 if 8 <= index <= 14 else 0.1)
            for index in range(30)
        ],
    )
    classification = classify_content(
        None,
        signals,
        ContentType.AUTO,
        source_hint="boss_fight_gameplay.mp4",
        config=config,
    )
    assert classification.content_type == ContentType.GAMING
    assert classification.confidence >= config.classification_min_confidence
    assert "mean motion" in " ".join(classification.reasons)

    candidates = build_signal_candidates(
        signals, None, config, content_type=classification.content_type
    )
    assert candidates
    assert any(candidate.category == "gameplay_event" for candidate in candidates)
    assert any(candidate.signals["motion"] > 0 for candidate in candidates)
    assert all(candidate.reason for candidate in candidates)


def test_classification_uses_live_stream_hint_and_returns_unknown_without_evidence():
    config = _settings()
    live_signals = MediaSignals(
        duration=1800,
        audio_analyzed=True,
        video_analyzed=True,
        motion_analyzed=True,
        audio_windows=_active_audio_windows(60),
        motion_samples=[MotionSample(time=float(index), score=0.1) for index in range(60)],
    )
    live = classify_content(
        None,
        live_signals,
        ContentType.AUTO,
        source_hint="https://twitch.tv/example",
        config=config,
    )
    assert live.content_type == ContentType.LIVE
    assert any("live-stream platform" in reason for reason in live.reasons)

    unknown = classify_content(None, MediaSignals(duration=10), ContentType.AUTO, config=config)
    assert unknown.content_type == ContentType.UNKNOWN
    assert unknown.confidence < config.classification_min_confidence


def test_classification_marks_close_spoken_and_action_evidence_as_mixed():
    config = _settings()
    transcript = _transcript(
        [
            (
                0.0,
                60.0,
                "Welcome to the podcast interview episode with our guest host "
                "about this game match.",
            )
        ],
        duration=60.0,
    )
    signals = MediaSignals(
        duration=60,
        audio_analyzed=True,
        video_analyzed=True,
        motion_analyzed=True,
        audio_windows=_active_audio_windows(60),
        scene_changes=[SceneChange(time=float(index), score=0.9) for index in range(1, 60)],
        motion_samples=[MotionSample(time=float(index), score=0.8) for index in range(60)],
    )
    classification = classify_content(transcript, signals, ContentType.AUTO, config=config)
    assert classification.content_type == ContentType.MIXED
    assert classification.ambiguous is True


def test_classification_defaults_dialogue_heavy_media_to_general_without_keywords():
    config = _settings()
    transcript = _transcript(
        [(0.0, 30.0, "They argued about the family and the old house all night.")],
        duration=30.0,
    )
    signals = MediaSignals(
        duration=30,
        audio_analyzed=True,
        video_analyzed=True,
        motion_analyzed=True,
        audio_windows=_active_audio_windows(30),
        motion_samples=[MotionSample(time=float(index), score=0.1) for index in range(30)],
    )
    classification = classify_content(transcript, signals, ContentType.AUTO, config=config)
    assert classification.content_type == ContentType.GENERAL
    assert classification.content_type != ContentType.PODCAST


def test_classification_uses_educational_keywords_for_lectures():
    config = _settings()
    transcript = _transcript(
        [
            (
                0.0,
                30.0,
                "Welcome to this lesson. I will explain how to complete the course project.",
            )
        ],
        duration=30.0,
    )
    signals = MediaSignals(
        duration=30,
        audio_analyzed=True,
        video_analyzed=True,
        motion_analyzed=True,
        audio_windows=_active_audio_windows(30),
        motion_samples=[MotionSample(time=float(index), score=0.1) for index in range(30)],
    )
    classification = classify_content(transcript, signals, ContentType.AUTO, config=config)
    assert classification.content_type == ContentType.EDUCATIONAL


def test_audio_peak_and_silence_transition_generate_detection_signals():
    config = _settings(min_clip_seconds=3, max_clip_seconds=15, event_merge_seconds=2)
    windows = [
        AudioWindow(
            start=float(index),
            end=float(index + 1),
            rms_dbfs=-60.0,
            peak_dbfs=-50.0,
            energy=0.0,
            silence=True,
        )
        for index in range(20)
    ]
    for index in (10, 11):
        windows[index] = AudioWindow(
            start=float(index),
            end=float(index + 1),
            rms_dbfs=-12.0,
            peak_dbfs=-3.0,
            energy=0.98,
            silence=False,
        )
    signals = MediaSignals(
        duration=20,
        audio_analyzed=True,
        video_analyzed=True,
        audio_windows=windows,
    )
    candidates = build_signal_candidates(
        signals, None, config, content_type=ContentType.GAMING
    )
    assert candidates
    candidate = candidates[0]
    assert candidate.signals["audio_peak"] > 0
    assert candidate.signals["silence_transition"] == 1.0
    assert candidate.category in {"audio_spike", "sound_event", "activity_spike"}


def test_dedupe_candidates_honors_configurable_overlap_ratio():
    candidates = [
        Candidate(start=0, end=10, score=0.9, category="a", reason="a"),
        Candidate(start=1, end=11, score=0.8, category="b", reason="b"),
        Candidate(start=20, end=30, score=0.7, category="c", reason="c"),
    ]
    assert len(dedupe_candidates(candidates, overlap_ratio=0.55)) == 2
    assert len(dedupe_candidates(candidates, overlap_ratio=0.99)) == 3


def test_engine_records_classification_and_detection_signals(monkeypatch):
    config = _settings(min_clip_seconds=1, max_clip_seconds=5, use_llm=False)
    candidate = Candidate(
        start=0.0,
        end=2.0,
        score=0.9,
        category="gameplay_event",
        reason="Sudden audio peak and motion.",
        signals={"audio_peak": 0.8, "motion": 0.7},
    )
    monkeypatch.setattr(
        "clipping_ai.highlights.engine.build_transcript_candidates",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "clipping_ai.highlights.engine.build_signal_candidates",
        lambda *args, **kwargs: [candidate],
    )
    signals = MediaSignals(
        duration=10.0,
        audio_analyzed=True,
        video_analyzed=True,
        motion_analyzed=True,
        audio_windows=_active_audio_windows(10, spike_indices={2}),
        scene_changes=[SceneChange(time=2, score=0.8)],
        motion_samples=[MotionSample(time=float(index), score=0.7) for index in range(10)],
    )
    result = detect_highlights(
        job_id="c" * 32,
        media_path=__file__,  # type: ignore[arg-type]
        metadata=_metadata(10.0),
        transcript=None,
        config=config,
        manager=None,
        signals=signals,
        source_hint="gameplay.mp4",
    )
    assert result.classification is not None
    assert result.classification.content_type == ContentType.GAMING
    assert result.highlights
    assert result.highlights[0].detection_signals == {
        "audio_peak": 0.8,
        "motion": 0.7,
    }


def test_llm_refinement_validates_and_clamps_timestamps():
    config = _settings(min_clip_seconds=4, max_clip_seconds=20)
    transcript = _transcript(
        [
            (0.0, 6.0, "This is the first complete moment."),
            (10.0, 16.0, "This is another complete moment."),
        ],
        duration=18.0,
    )
    candidate = build_transcript_candidates(
        transcript, config, content_type=ContentType.PODCAST
    )[0]
    manager = FakeManager(
        {
            "data": {
                "highlights": [
                    {
                        "start_time": 10,
                        "end_time": 200,
                        "suggested_title": "The second moment",
                        "summary": "A complete moment.",
                        "reasoning": "It is self-contained.",
                        "score": 91,
                        "category": "storytelling",
                        "signals": {"audio_peak": 1.4, "scene_change": -0.2},
                    },
                    {
                        "start_time": 15,
                        "end_time": 5,
                        "suggested_title": "Invalid",
                        "summary": "Invalid range.",
                        "reasoning": "Invalid.",
                        "score": 50,
                        "category": "invalid",
                    },
                ]
            }
        }
    )
    highlights, error = refine_with_llm(
        manager,  # type: ignore[arg-type]
        transcript=transcript,
        candidates=[candidate],
        content_type=ContentType.PODCAST,
        signals=None,
        config=config,
    )
    assert error is None
    assert len(highlights) == 1
    highlight = highlights[0]
    assert 10 <= highlight.start_time < highlight.end_time <= 18
    assert highlight.confidence == pytest.approx(0.91)
    assert highlight.detection_signals == {"audio_peak": 1.0, "scene_change": 0.0}
    assert manager.calls[0][0] == "highlight_detection"


def test_engine_does_not_apply_llm_candidate_limit_without_llm(monkeypatch):
    config = _settings(
        min_clip_seconds=1,
        max_clip_seconds=5,
        max_highlights=5,
        llm_max_candidates=2,
        use_llm=False,
    )
    candidates = [
        Candidate(
            start=float(index * 2),
            end=float(index * 2 + 1),
            score=0.9 - index * 0.1,
            category="storytelling",
            reason="A complete moment.",
        )
        for index in range(5)
    ]
    monkeypatch.setattr(
        "clipping_ai.highlights.engine.build_transcript_candidates",
        lambda *args, **kwargs: list(candidates),
    )
    monkeypatch.setattr(
        "clipping_ai.highlights.engine.build_signal_candidates",
        lambda *args, **kwargs: [],
    )
    result = detect_highlights(
        job_id="b" * 32,
        media_path=__file__,  # type: ignore[arg-type]
        metadata=_metadata(10.0),
        transcript=_transcript([(0.0, 1.0, "A complete moment.")]),
        config=config,
        manager=None,
        signals=MediaSignals(duration=10.0, audio_analyzed=True, video_analyzed=True),
    )
    assert len(result.highlights) == 5


def test_engine_falls_back_to_deterministic_when_llm_fails():
    config = _settings(min_clip_seconds=4, max_clip_seconds=20)
    transcript = _transcript([(0.0, 6.0, "This is a complete moment.")], duration=10.0)
    signals = MediaSignals(duration=10.0, audio_analyzed=True, video_analyzed=True)
    manager = FakeManager(
        error=AIError(
            ErrorCategory.PROVIDER_UNAVAILABLE,
            "offline",
            retryable=False,
        )
    )
    result = detect_highlights(
        job_id="a" * 32,
        media_path=__file__,  # type: ignore[arg-type]
        metadata=_metadata(10.0),
        transcript=transcript,
        config=config,
        manager=manager,  # type: ignore[arg-type]
        signals=signals,
    )
    assert result.method == "deterministic"
    assert result.highlights
    assert any("Deterministic candidates were used" in note for note in result.notes)


def _highlight(
    start: float,
    end: float,
    title: str,
    description: str,
    *,
    category: str = "explanation",
    confidence: float = 0.7,
    source: str = "llm",
    segments: list[TranscriptSegment] | None = None,
):
    return Highlight(
        start_time=start,
        end_time=end,
        title=title,
        description=description,
        selection_reason="A meaningful moment.",
        confidence=confidence,
        category=category,
        transcript_segments=segments or [],
        detection_signals={},
        source=source,
    )


def test_semantic_consolidation_merges_related_fragments_across_bridge():
    config = _settings(
        min_clip_seconds=4,
        max_clip_seconds=60,
        semantic_bridge_gap_seconds=20,
        semantic_merge_gap_seconds=3,
        semantic_topic_overlap=0.2,
    )
    transcript = _transcript(
        [
            (0.0, 10.0, "This example explains why the cache fails when it is full."),
            (10.0, 20.0, "The cache keeps stale data because the eviction rule is wrong."),
            (20.0, 30.0, "So the system fails when the cache is cleared and rebuilt."),
        ],
        duration=30.0,
    )
    first = _highlight(
        0.0,
        10.0,
        "Cache problem",
        "The cache fails when it is full.",
        segments=[transcript.segments[0]],
    )
    second = _highlight(
        22.0,
        30.0,
        "Cache fix",
        "The system fails when the cache is cleared and rebuilt.",
        segments=[transcript.segments[2]],
    )

    result = consolidate_highlights(
        [first, second], transcript, config, content_type=ContentType.EDUCATIONAL
    )

    assert len(result) == 1
    assert result[0].start_time == 0.0
    assert result[0].end_time == 30.0
    assert result[0].detection_signals["semantic_completeness"] == 1.0
    assert "consolidated" in result[0].selection_reason.lower()


def test_semantic_consolidation_keeps_unrelated_nearby_highlights_separate():
    config = _settings(
        min_clip_seconds=4,
        max_clip_seconds=60,
        semantic_bridge_gap_seconds=20,
        semantic_merge_gap_seconds=3,
        semantic_topic_overlap=0.2,
    )
    transcript = _transcript(
        [
            (0.0, 10.0, "The boss fight needed a perfect dodge and a fast counter."),
            (10.0, 20.0, "Cooking pasta needs salted water and a little patience."),
        ],
        duration=20.0,
    )
    first = _highlight(
        0.0,
        10.0,
        "Boss fight",
        "The boss fight needed a perfect dodge.",
        segments=[transcript.segments[0]],
    )
    second = _highlight(
        12.0,
        20.0,
        "Cooking pasta",
        "Cooking pasta needs salted water and patience.",
        category="storytelling",
        segments=[transcript.segments[1]],
    )

    result = consolidate_highlights(
        [first, second], transcript, config, content_type=ContentType.GENERAL
    )

    assert len(result) == 2
    assert [item.start_time for item in result] == [0.0, 10.0]


def test_semantic_consolidation_ignores_tiny_bridge_overlap():
    config = _settings(
        min_clip_seconds=4,
        max_clip_seconds=60,
        semantic_bridge_gap_seconds=20,
        semantic_merge_gap_seconds=3,
        semantic_topic_overlap=0.2,
    )
    transcript = _transcript(
        [
            (
                0.0,
                10.0,
                "The first lesson explains how a doctor uses a prescription and warns "
                "readers about acting without training.",
            ),
            (10.0, 14.0, "the hadith"),
            (
                14.0,
                24.0,
                "The second lesson discusses a hadith about hair oil and perfume and grooming.",
            ),
        ],
        duration=24.0,
    )
    first = _highlight(
        0.0,
        10.0,
        "Medical example",
        "A doctor uses a prescription.",
        segments=[transcript.segments[0]],
    )
    second = _highlight(
        14.0,
        24.0,
        "Hair example",
        "A hadith about hair oil and perfume.",
        segments=[transcript.segments[2]],
    )

    result = consolidate_highlights(
        [first, second], transcript, config, content_type=ContentType.EDUCATIONAL
    )

    assert len(result) == 2
    assert [item.start_time for item in result] == [0.0, 14.0]


def test_semantic_consolidation_preserves_self_contained_short_highlight():
    config = _settings(
        min_clip_seconds=4,
        max_clip_seconds=60,
        semantic_boundary_context_seconds=1.0,
    )
    transcript = _transcript(
        [
            (0.0, 10.0, "Unrelated introduction about the weather."),
            (20.0, 38.0, "This is a complete eighteen second thought with a clear ending."),
            (50.0, 60.0, "Unrelated closing remarks about the weather."),
        ],
        duration=60.0,
    )
    highlight = _highlight(
        20.0,
        38.0,
        "Complete thought",
        "This is a complete eighteen second thought.",
        segments=[transcript.segments[1]],
    )

    result = consolidate_highlights(
        [highlight], transcript, config, content_type=ContentType.GENERAL
    )

    assert len(result) == 1
    assert result[0].start_time == 20.0
    assert result[0].end_time == 38.0


def test_semantic_consolidation_expands_to_sentence_boundaries():
    config = _settings(min_clip_seconds=4, max_clip_seconds=60)
    transcript = _transcript(
        [
            (0.0, 5.0, "Intro."),
            (5.0, 12.0, "The full explanation starts here and continues."),
            (12.0, 18.0, "The conclusion ends here."),
        ],
        duration=18.0,
    )
    highlight = _highlight(
        6.0,
        11.0,
        "Explanation",
        "The full explanation starts here.",
        segments=[transcript.segments[1]],
    )

    result = consolidate_highlights(
        [highlight], transcript, config, content_type=ContentType.EDUCATIONAL
    )

    assert result[0].start_time == 5.0
    assert result[0].end_time == 12.0


def test_semantic_consolidation_does_not_truncate_long_explanation():
    config = _settings(min_clip_seconds=4, max_clip_seconds=180)
    transcript = _transcript(
        [
            (0.0, 60.0, "The first part of the explanation introduces the problem."),
            (60.0, 120.0, "The second part resolves the problem with a complete conclusion."),
        ],
        duration=120.0,
    )
    highlight = _highlight(
        0.0,
        120.0,
        "Full explanation",
        "The complete explanation.",
        segments=list(transcript.segments),
    )

    result = consolidate_highlights(
        [highlight], transcript, config, content_type=ContentType.EDUCATIONAL
    )

    assert result[0].start_time == 0.0
    assert result[0].end_time == 120.0


def test_engine_keeps_highest_confidence_highlights_before_max_truncation(monkeypatch):
    config = _settings(
        min_clip_seconds=1,
        max_clip_seconds=5,
        max_highlights=2,
        use_llm=False,
    )
    candidates = [
        Candidate(start=0.0, end=2.0, score=0.90, category="a", reason="first"),
        Candidate(start=10.0, end=12.0, score=0.80, category="b", reason="second"),
        Candidate(start=20.0, end=22.0, score=0.95, category="c", reason="third"),
    ]
    monkeypatch.setattr(
        "clipping_ai.highlights.engine.build_transcript_candidates",
        lambda *args, **kwargs: list(candidates),
    )
    monkeypatch.setattr(
        "clipping_ai.highlights.engine.build_signal_candidates",
        lambda *args, **kwargs: [],
    )

    result = detect_highlights(
        job_id="d" * 32,
        media_path=__file__,  # type: ignore[arg-type]
        metadata=_metadata(30.0),
        transcript=_transcript([(0.0, 30.0, "A complete moment.")], duration=30.0),
        config=config,
        manager=None,
        signals=MediaSignals(duration=30.0, audio_analyzed=True, video_analyzed=True),
    )

    assert [item.start_time for item in result.highlights] == [0.0, 20.0]


def test_engine_consolidates_llm_fragments_into_one_complete_moment():
    config = _settings(
        min_clip_seconds=4,
        max_clip_seconds=60,
        semantic_bridge_gap_seconds=20,
        semantic_merge_gap_seconds=3,
        semantic_topic_overlap=0.2,
    )
    transcript = _transcript(
        [
            (0.0, 10.0, "This example explains why the cache fails when it is full."),
            (10.0, 20.0, "The cache keeps stale data because the eviction rule is wrong."),
            (20.0, 30.0, "So the system fails when the cache is cleared and rebuilt."),
        ],
        duration=30.0,
    )
    manager = FakeManager(
        {
            "data": {
                "highlights": [
                    {
                        "start_time": 0,
                        "end_time": 10,
                        "suggested_title": "Cache problem",
                        "summary": "The cache fails when it is full.",
                        "reasoning": "It introduces the problem.",
                        "score": 90,
                        "category": "explanation",
                    },
                    {
                        "start_time": 22,
                        "end_time": 30,
                        "suggested_title": "Cache fix",
                        "summary": "The system fails when the cache is cleared.",
                        "reasoning": "It completes the example.",
                        "score": 85,
                        "category": "explanation",
                    },
                ]
            }
        }
    )

    result = detect_highlights(
        job_id="e" * 32,
        media_path=__file__,  # type: ignore[arg-type]
        metadata=_metadata(30.0),
        transcript=transcript,
        config=config,
        manager=manager,  # type: ignore[arg-type]
        signals=MediaSignals(duration=30.0, audio_analyzed=True, video_analyzed=True),
    )

    assert result.method == "llm"
    assert len(result.highlights) == 1
    assert result.highlights[0].start_time == 0.0
    assert result.highlights[0].end_time == 30.0


def test_transcript_candidates_include_semantic_window_beyond_short_limit():
    config = _settings(
        min_clip_seconds=4,
        max_clip_seconds=60,
        candidate_window_seconds=12,
        candidate_stride_seconds=6,
        semantic_gap_seconds=2,
    )
    transcript = _transcript(
        [
            (0.0, 10.0, "This introduces the problem with the cache."),
            (10.0, 20.0, "This explains why the cache becomes stale."),
            (20.0, 30.0, "This shows the system failing under load."),
            (30.0, 40.0, "This concludes with the complete fix."),
        ],
        duration=40.0,
    )

    candidates = build_transcript_candidates(
        transcript, config, content_type=ContentType.EDUCATIONAL
    )

    assert any(
        candidate.source == "semantic" and candidate.end - candidate.start > 12
        for candidate in candidates
    )


def test_semantic_consolidation_handles_arabic_bridge_content():
    config = _settings(
        min_clip_seconds=4,
        max_clip_seconds=60,
        semantic_bridge_gap_seconds=20,
        semantic_merge_gap_seconds=3,
        semantic_topic_overlap=0.2,
    )
    transcript = _transcript(
        [
            (0.0, 10.0, "أن العلماء قالوا شيئا عن الحديث،"),
            (10.0, 20.0, "وفي وسط الكلام شرح الحديث وقال إن العلماء يفهمون الحديث."),
            (20.0, 30.0, "ثم قال أنا فهم الحديث ولذلك لا أقبل هذا الكلام."),
        ],
        duration=30.0,
    )
    first = _highlight(
        0.0,
        10.0,
        "العلماء والحديث",
        "أن العلماء قالوا شيئا عن الحديث.",
        segments=[transcript.segments[0]],
    )
    second = _highlight(
        22.0,
        30.0,
        "فهم الحديث",
        "أنا فهم الحديث ولذلك لا أقبل هذا الكلام.",
        segments=[transcript.segments[2]],
    )

    result = consolidate_highlights(
        [first, second], transcript, config, content_type=ContentType.GENERAL
    )

    assert len(result) == 1
    assert result[0].start_time == 0.0
    assert result[0].end_time == 30.0
