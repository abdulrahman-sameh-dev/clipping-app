"""Confidence-gated content classification from transcript and media signals."""

from __future__ import annotations

from clipping_ai.analysis.config import HighlightSettings
from clipping_ai.highlights.models import ContentType, MediaClassification, MediaSignals
from clipping_ai.schemas import TranscriptResult

_SPOKEN_TYPES = {ContentType.PODCAST, ContentType.EDUCATIONAL, ContentType.GENERAL}
_ACTION_TYPES = {ContentType.GAMING, ContentType.LIVE}

_PODCAST_KEYWORDS = {
    "podcast",
    "interview",
    "episode",
    "host",
    "guest",
    "conversation",
    "talk show",
    "q&a",
    "microphone",
    "بودكاست",
    "مقابلة",
    "ضيف",
    "حوار",
    "نقاش",
}
_EDUCATIONAL_KEYWORDS = {
    "tutorial",
    "lesson",
    "course",
    "lecture",
    "explain",
    "explained",
    "how to",
    "how-to",
    "guide",
    "learn",
    "teaching",
    "class",
    "training",
    "الدرس",
    "دروس",
    "محاضرة",
    "محاضرات",
    "دورة",
    "دورات",
    "شرح",
    "يشرح",
    "تعليم",
    "تعلم",
    "كورس",
    "تطبيق",
    "تطبيقات",
    "برمجة",
    "برمج",
    "كود",
    "اكواد",
    "اندرويد",
    "جوجل بلاي",
    "مشروع",
}
_GAMING_KEYWORDS = {
    "gameplay",
    "game",
    "boss",
    "kill",
    "match",
    "round",
    "clutch",
    "respawn",
    "loot",
    "fps",
    "speedrun",
    "playthrough",
    "streamer",
    "headshot",
    "quest",
    "raid",
    "level",
    "قيم بلاي",
    "جيمنج",
    "بلايستيشن",
    "بلاي ستيشن",
    "اكس بوكس",
    "ببجي",
    "فورتنايت",
    "ماينكرافت",
    "ستريمر",
}
_LIVE_KEYWORDS = {
    "live",
    "livestream",
    "stream",
    "twitch",
    "kick",
    "youtube live",
    "chat",
    "donation",
    "subscriber",
    "viewer",
    "بث مباشر",
    "تويتش",
    "مشاهدين",
    "دونيشن",
    "سبسكرايب",
}
_LIVE_SOURCE_HINTS = ("twitch.tv", "kick.com", "youtube.com/live", "facebook.com/live", "dlive.tv")


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, value))


def _keyword_hits(text: str, keywords: set[str]) -> int:
    return sum(1 for keyword in keywords if keyword in text)


def speech_ratio(transcript: TranscriptResult | None) -> float:
    if transcript is None or not transcript.segments:
        return 0.0
    duration = max(0.1, (transcript.duration or transcript.segments[-1].end))
    speech = sum(max(0.0, segment.end - segment.start) for segment in transcript.segments)
    return _clamp(speech / duration)


def signal_activity_ratio(signals: MediaSignals | None) -> float:
    if signals is None or not signals.audio_windows:
        return 0.0
    active = sum(1 for window in signals.audio_windows if not window.silence)
    return active / len(signals.audio_windows)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((percentile / 100.0) * (len(ordered) - 1)))))
    return ordered[index]


def _media_evidence(
    transcript: TranscriptResult | None,
    signals: MediaSignals | None,
    config: HighlightSettings,
) -> dict[str, float]:
    speech = speech_ratio(transcript)
    windows = signals.audio_windows if signals else []
    active = [window for window in windows if not window.silence]
    active_ratio = len(active) / len(windows) if windows else 0.0
    silence_ratio = 1.0 - active_ratio if windows else 0.0
    mean_energy = sum(window.energy for window in active) / len(active) if active else 0.0
    peak_threshold = max(
        config.audio_event_min_energy,
        _percentile([window.energy for window in active], config.spike_percentile),
    )
    peak_ratio = (
        sum(1 for window in active if window.energy >= peak_threshold) / len(active)
        if active
        else 0.0
    )

    duration = signals.duration if signals else (transcript.duration if transcript else 0.0)
    scene_rate = len(signals.scene_changes) / duration * 60.0 if signals and duration else 0.0
    motion_scores = [sample.score for sample in signals.motion_samples] if signals else []
    motion_mean = sum(motion_scores) / len(motion_scores) if motion_scores else 0.0
    motion_active_ratio = (
        sum(1 for score in motion_scores if score >= config.motion_event_threshold)
        / len(motion_scores)
        if motion_scores
        else 0.0
    )

    speech_evidence = _clamp(speech / 0.35)
    scene_evidence = _clamp(scene_rate / 12.0)
    motion_evidence = _clamp(motion_mean / 0.35)
    if motion_scores and signals and signals.scene_changes:
        visual_evidence = _clamp(scene_evidence * 0.55 + motion_evidence * 0.45)
    elif motion_scores:
        visual_evidence = motion_evidence
    else:
        visual_evidence = scene_evidence
    audio_evidence = _clamp(active_ratio * (0.55 + mean_energy * 0.45))
    event_evidence = _clamp(peak_ratio * 0.7 + silence_ratio * 0.3)
    action_evidence = _clamp(visual_evidence * 0.55 + event_evidence * 0.45)

    return {
        "speech": speech,
        "speech_evidence": speech_evidence,
        "active_audio_ratio": active_ratio,
        "silence_ratio": silence_ratio,
        "mean_energy": mean_energy,
        "peak_ratio": peak_ratio,
        "scene_rate": scene_rate,
        "motion_mean": motion_mean,
        "motion_active_ratio": motion_active_ratio,
        "visual_evidence": visual_evidence,
        "audio_evidence": audio_evidence,
        "event_evidence": event_evidence,
        "action_evidence": action_evidence,
    }


def classify_content(
    transcript: TranscriptResult | None,
    signals: MediaSignals | None,
    requested: ContentType = ContentType.AUTO,
    *,
    source_hint: str | None = None,
    config: HighlightSettings | None = None,
) -> MediaClassification:
    if requested != ContentType.AUTO:
        return MediaClassification(
            content_type=requested,
            confidence=1.0,
            scores={requested.value: 1.0},
            reasons=["Explicit content_type override was supplied."],
        )

    config = config or HighlightSettings()
    evidence = _media_evidence(transcript, signals, config)
    transcript_text = transcript.text.lower() if transcript and transcript.text else ""
    hint_text = source_hint.lower() if source_hint else ""
    text = f"{transcript_text} {hint_text}".strip()

    podcast_hits = _keyword_hits(text, _PODCAST_KEYWORDS)
    educational_hits = _keyword_hits(text, _EDUCATIONAL_KEYWORDS)
    gaming_hits = _keyword_hits(text, _GAMING_KEYWORDS)
    live_hits = _keyword_hits(text, _LIVE_KEYWORDS)
    live_source = 1.0 if any(marker in hint_text for marker in _LIVE_SOURCE_HINTS) else 0.0
    duration = (
        signals.duration
        if signals
        else (transcript.duration if transcript and transcript.duration else 0.0)
    )
    long_stream = 1.0 if duration >= 1800 else 0.0

    speech = evidence["speech_evidence"]
    visual = evidence["visual_evidence"]
    events = evidence["event_evidence"]
    action = evidence["action_evidence"]

    scores = {
        ContentType.PODCAST.value: _clamp(
            0.04
            + speech * 0.3
            + (1.0 - visual) * 0.07
            + (1.0 - events) * 0.05
            + min(0.45, podcast_hits * 0.15)
        ),
        ContentType.EDUCATIONAL.value: _clamp(
            0.04 + speech * 0.28 + (1.0 - visual) * 0.06 + min(0.45, educational_hits * 0.15)
        ),
        ContentType.GENERAL.value: _clamp(
            0.2 + speech * 0.36 + (1.0 - abs(visual - 0.25)) * 0.08 + (1.0 - events) * 0.04
        ),
        ContentType.GAMING.value: _clamp(
            0.06
            + action * 0.5
            + visual * 0.22
            + (1.0 - speech) * 0.1
            + min(0.2, gaming_hits * 0.07)
        ),
        ContentType.LIVE.value: _clamp(
            0.03
            + action * 0.3
            + visual * 0.12
            + (1.0 - speech) * 0.05
            + min(0.2, live_hits * 0.07)
            + live_source * 0.5
            + long_stream * 0.08
        ),
    }
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    top_type, top_score = ranked[0]
    second_type, second_score = ranked[1]
    gap = top_score - second_score

    reasons = [
        f"speech coverage {evidence['speech']:.2f}",
        f"audio activity {evidence['active_audio_ratio']:.2f}",
        f"silence ratio {evidence['silence_ratio']:.2f}",
        f"scene changes {evidence['scene_rate']:.1f}/min",
        f"mean motion {evidence['motion_mean']:.2f}",
    ]
    if live_source:
        reasons.append("source hint matches a live-stream platform")
    if duration:
        reasons.append(f"duration {duration:.0f}s")

    if top_score < config.classification_min_confidence:
        reasons.append(
            f"top classification score {top_score:.2f} is below the "
            f"{config.classification_min_confidence:.2f} confidence threshold"
        )
        return MediaClassification(
            content_type=ContentType.UNKNOWN,
            confidence=round(top_score, 4),
            scores={key: round(value, 4) for key, value in scores.items()},
            reasons=reasons,
        )

    top_content = ContentType(top_type)
    second_content = ContentType(second_type)
    if (
        gap < config.classification_margin
        and second_score >= config.classification_min_confidence
        and (
            (top_content in _SPOKEN_TYPES and second_content in _ACTION_TYPES)
            or (top_content in _ACTION_TYPES and second_content in _SPOKEN_TYPES)
        )
    ):
        reasons.append(
            f"spoken and action evidence are close ({top_type} {top_score:.2f}, "
            f"{second_type} {second_score:.2f})"
        )
        confidence = _clamp(0.5 + (config.classification_margin - gap) * 2.0)
        return MediaClassification(
            content_type=ContentType.MIXED,
            confidence=round(confidence, 4),
            scores={key: round(value, 4) for key, value in scores.items()},
            reasons=reasons,
            ambiguous=True,
        )

    reasons.append(f"selected {top_type} over {second_type} by {gap:.2f} confidence margin")
    return MediaClassification(
        content_type=top_content,
        confidence=round(top_score, 4),
        scores={key: round(value, 4) for key, value in scores.items()},
        reasons=reasons,
    )


def infer_content_type(
    transcript: TranscriptResult | None,
    signals: MediaSignals | None,
    requested: ContentType,
    *,
    source_hint: str | None = None,
    config: HighlightSettings | None = None,
) -> ContentType:
    return classify_content(
        transcript,
        signals,
        requested,
        source_hint=source_hint,
        config=config,
    ).content_type
