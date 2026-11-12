"""Automatic edit planning for captions and restrained visual effects."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from clipping_ai.clips.config import ClipConfig
from clipping_ai.highlights.models import Highlight
from clipping_ai.schemas import TranscriptResult, TranscriptWord

_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_ARABIC_DIACRITICS_RE = re.compile(r"[\u064B-\u0652]")
_PUNCTUATION = ".,!?؟،؛:;…。！？«»\"\'()[]{}"
_SENTENCE_ENDINGS = (".", "!", "?", "؟", "。", "！", "？", "…")
_IMPORTANT_WORDS = {
    "important",
    "crucial",
    "must",
    "never",
    "always",
    "because",
    "reason",
    "secret",
    "best",
    "worst",
    "how",
    "why",
    "wrong",
    "right",
    "truth",
    "remember",
    "avoid",
    "need",
    "key",
    "actually",
    "really",
    "most",
    "least",
    "first",
    "last",
    "example",
    "أهم",
    "مهم",
    "يجب",
    "لا",
    "أبدا",
    "أبداً",
    "السبب",
    "لأن",
    "لذلك",
    "كيف",
    "لماذا",
    "أفضل",
    "أسوأ",
    "خطأ",
    "صحيح",
    "سر",
    "الحقيقة",
    "دائما",
    "دائماً",
    "مستحيل",
    "ضروري",
    "انتبه",
    "احذر",
    "مثلا",
    "مثلاً",
    "على سبيل المثال",
}


@dataclass(frozen=True)
class CaptionWord:
    text: str
    start: float
    end: float
    language: str | None = None
    important: bool = False
    importance: float = 0.0


@dataclass(frozen=True)
class CaptionCue:
    start: float
    end: float
    text: str
    language: str | None
    words: tuple[CaptionWord, ...] = ()


@dataclass(frozen=True)
class Effect:
    kind: str
    start: float
    end: float
    intensity: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class EditPlan:
    caption_cues: tuple[CaptionCue, ...] = ()
    effects: tuple[Effect, ...] = ()
    notes: tuple[str, ...] = ()


def is_arabic_text(text: str) -> bool:
    return _ARABIC_RE.search(text) is not None


def _normalise_word(text: str) -> str:
    cleaned = _ARABIC_DIACRITICS_RE.sub("", text.strip().lower())
    return cleaned.strip(_PUNCTUATION).strip()


def _clip_importance(highlight: Highlight | None, signals: dict[str, object] | None) -> float:
    if highlight is None:
        confidence = 0.5
        signal_score = 0.0
        detection_signals: dict[str, float] = {}
    else:
        confidence = highlight.confidence
        signal_score = highlight.signal_score or 0.0
        detection_signals = highlight.detection_signals
    signals = signals or {}
    active_audio = _number(signals.get("active_audio_ratio"), 0.5)
    mean_energy = _number(signals.get("mean_energy"), 0.5)
    scene_rate = _number(signals.get("scene_rate_per_minute"), 0.0)
    mean_motion = _number(signals.get("mean_motion"), 0.0)
    semantic = detection_signals.get("semantic_completeness", 0.0)
    score = (
        0.55 * confidence
        + 0.15 * signal_score
        + 0.1 * semantic
        + 0.1 * active_audio
        + 0.1 * min(1.0, max(0.0, mean_energy) * 1.4)
        + 0.05 * min(1.0, max(0.0, scene_rate) / 2.0)
        - 0.05 * max(0.0, mean_motion - 0.6)
    )
    return _clamp(score)


def _number(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, value))


def _word_language(text: str, fallback: str | None) -> str | None:
    if is_arabic_text(text):
        return "ar"
    if fallback:
        return fallback
    return "en"


def _word_score(
    word: TranscriptWord,
    *,
    cue_text: str,
    title_tokens: set[str],
    importance: float,
) -> float:
    raw = word.word.strip()
    normalised = _normalise_word(raw)
    score = 0.0
    if normalised in _IMPORTANT_WORDS:
        score += 0.55
    if normalised and normalised in title_tokens:
        score += 0.35
    if any(char.isdigit() for char in raw):
        score += 0.35
    if raw.rstrip().endswith(("?", "!", "؟", "！")):
        score += 0.3
    latin_letters = [char for char in raw if char.isascii() and char.isalpha()]
    if len(latin_letters) >= 3 and raw == raw.upper():
        score += 0.25
    if len(normalised) >= 7:
        score += 0.15
    if importance >= 0.65 and len(normalised) >= 5:
        score += 0.1
    if cue_text and normalised and normalised in _normalise_word(cue_text):
        score += 0.02
    return _clamp(score)


def _title_tokens(highlight: Highlight | None) -> set[str]:
    if highlight is None:
        return set()
    text = " ".join((highlight.title, highlight.description, highlight.selection_reason))
    return {_normalise_word(token) for token in text.split() if _normalise_word(token)}


def _collect_words(
    transcript: TranscriptResult,
    *,
    start_time: float,
    end_time: float,
) -> list[TranscriptWord]:
    words: list[TranscriptWord] = []
    if transcript.words:
        for word in transcript.words:
            if word.end > start_time and word.start < end_time:
                clipped_start = max(word.start, start_time)
                clipped_end = min(word.end, end_time)
                if clipped_end > clipped_start and word.word.strip():
                    words.append(
                        TranscriptWord(
                            word=word.word,
                            start=clipped_start,
                            end=clipped_end,
                            probability=word.probability,
                        )
                    )
    if words:
        return sorted(words, key=lambda item: (item.start, item.end))

    for segment in transcript.segments:
        if not segment.words:
            continue
        if segment.end <= start_time or segment.start >= end_time:
            continue
        for word in segment.words:
            if word.end > start_time and word.start < end_time:
                clipped_start = max(word.start, start_time)
                clipped_end = min(word.end, end_time)
                if clipped_end > clipped_start and word.word.strip():
                    words.append(
                        TranscriptWord(
                            word=word.word,
                            start=clipped_start,
                            end=clipped_end,
                            probability=word.probability,
                        )
                    )
    return sorted(words, key=lambda item: (item.start, item.end))


def _cue_text(words: list[TranscriptWord]) -> str:
    return " ".join(word.word.strip() for word in words).strip()


def _ends_sentence(text: str) -> bool:
    return text.rstrip().endswith(_SENTENCE_ENDINGS)


def _make_cue(
    words: list[TranscriptWord],
    *,
    clip_start: float,
    clip_end: float,
    language: str | None,
    config: ClipConfig,
) -> CaptionCue | None:
    if not words:
        return None
    start = max(clip_start, words[0].start)
    end = min(clip_end, words[-1].end)
    if end <= start:
        return None
    cue_language = "ar" if any(is_arabic_text(word.word) for word in words) else language
    return CaptionCue(
        start=round(start, 3),
        end=round(end, 3),
        text=_cue_text(words),
        language=cue_language,
        words=tuple(
            CaptionWord(
                text=word.word.strip(),
                start=round(word.start, 3),
                end=round(word.end, 3),
                language=_word_language(word.word, cue_language),
            )
            for word in words
        ),
    )


def _group_words(
    words: list[TranscriptWord],
    *,
    clip_start: float,
    clip_end: float,
    language: str | None,
    config: ClipConfig,
) -> list[CaptionCue]:
    cues: list[CaptionCue] = []
    current: list[TranscriptWord] = []
    for word in words:
        if current:
            projected = _cue_text([*current, word])
            gap = word.start - current[-1].end
            too_long = len(projected) > config.caption_phrase_max_chars
            too_many = len(current) >= config.caption_phrase_max_words
            too_slow = word.start - current[0].start > config.caption_phrase_max_seconds
            if gap > config.caption_break_gap_seconds or too_long or too_many or too_slow:
                cue = _make_cue(
                    current,
                    clip_start=clip_start,
                    clip_end=clip_end,
                    language=language,
                    config=config,
                )
                if cue is not None:
                    cues.append(cue)
                current = []
        current.append(word)
        if _ends_sentence(word.word):
            cue = _make_cue(
                current,
                clip_start=clip_start,
                clip_end=clip_end,
                language=language,
                config=config,
            )
            if cue is not None:
                cues.append(cue)
            current = []
    if current:
        cue = _make_cue(
            current,
            clip_start=clip_start,
            clip_end=clip_end,
            language=language,
            config=config,
        )
        if cue is not None:
            cues.append(cue)
    return cues


def _mark_important_words(
    cues: list[CaptionCue],
    *,
    config: ClipConfig,
    importance: float,
    title_tokens: set[str],
) -> list[CaptionCue]:
    marked: list[CaptionCue] = []
    for cue in cues:
        if not cue.words or importance < config.caption_importance_threshold:
            marked.append(cue)
            continue
        scored: list[CaptionWord] = []
        for word in cue.words:
            score = _word_score(
                TranscriptWord(word=word.text, start=word.start, end=word.end),
                cue_text=cue.text,
                title_tokens=title_tokens,
                importance=importance,
            )
            scored.append(
                replace(
                    word,
                    language=_word_language(word.text, cue.language),
                    importance=round(score, 4),
                )
            )
        best = max(scored, key=lambda item: item.importance)
        if best.importance >= config.caption_importance_threshold and (
            len(scored) >= 3 or best.importance >= 0.75
        ):
            scored = [
                replace(word, important=word is best)
                for word in scored
            ]
        marked.append(
            replace(
                cue,
                words=tuple(scored),
            )
        )
    return marked


def build_caption_cues(
    transcript: TranscriptResult,
    *,
    start_time: float,
    end_time: float,
    config: ClipConfig,
    language: str | None = None,
    highlight: Highlight | None = None,
    signals: dict[str, object] | None = None,
) -> list[CaptionCue]:
    if not config.dynamic_captions or end_time <= start_time:
        return []
    words = _collect_words(transcript, start_time=start_time, end_time=end_time)
    if not words:
        return []
    resolved_language = (language or transcript.language or "").strip().lower() or None
    cues = _group_words(
        words,
        clip_start=start_time,
        clip_end=end_time,
        language=resolved_language,
        config=config,
    )
    importance = _clip_importance(highlight, signals)
    return _mark_important_words(
        cues,
        config=config,
        importance=importance,
        title_tokens=_title_tokens(highlight),
    )


def _important_words(cues: list[CaptionCue]) -> list[tuple[CaptionCue, CaptionWord]]:
    candidates: list[tuple[CaptionCue, CaptionWord]] = []
    for cue in cues:
        for word in cue.words:
            if word.important:
                candidates.append((cue, word))
    return sorted(candidates, key=lambda item: item[1].importance, reverse=True)


def _add_effect(
    effects: list[Effect],
    *,
    kind: str,
    start: float,
    end: float,
    intensity: float,
    duration: float,
    min_gap: float,
) -> bool:
    start = max(0.0, start)
    end = min(duration, end)
    if end - start < 0.25:
        return False
    for effect in effects:
        if (
            effect.kind in {"punch", "emphasis"}
            and kind in {"punch", "emphasis"}
            and start < effect.end + min_gap
            and end > effect.start - min_gap
        ):
            return False
    effects.append(
        Effect(
            kind=kind,
            start=round(start, 3),
            end=round(end, 3),
            intensity=round(
                1.0 if kind in {"fade_in", "fade_out"} else _clamp(intensity, 0.0, 0.12),
                4,
            ),
        )
    )
    return True


def _select_effects(
    *,
    cues: list[CaptionCue],
    highlight: Highlight | None,
    signals: dict[str, object] | None,
    start_time: float,
    end_time: float,
    config: ClipConfig,
) -> list[Effect]:
    duration = end_time - start_time
    if not config.effects_enabled or duration <= 0:
        return []
    importance = _clip_importance(highlight, signals)
    if importance < config.effects_min_importance:
        return []
    signals = signals or {}
    mean_motion = _number(signals.get("mean_motion"), 0.0)
    effects: list[Effect] = []
    visual_count = 0

    if duration >= config.effects_zoom_min_duration:
        zoom_duration = min(config.effects_zoom_duration, max(2.0, duration - 1.0))
        intensity = config.effects_zoom_intensity * (0.7 + 0.6 * importance)
        if mean_motion > 0.65:
            intensity *= 0.6
        if _add_effect(
            effects,
            kind="zoom",
            start=0.4,
            end=0.4 + zoom_duration,
            intensity=intensity,
            duration=duration,
            min_gap=config.effects_min_gap_seconds,
        ):
            visual_count += 1

    candidates = _important_words(cues)
    punch_added = False
    for _, word in candidates:
        word_start = word.start - start_time
        if word_start < 0.7 or word_start > duration - 1.0:
            continue
        punch_start = word_start - 0.35
        punch_end = punch_start + config.effects_punch_duration
        intensity = config.effects_punch_intensity * (0.8 + 0.4 * importance)
        if _add_effect(
            effects,
            kind="punch",
            start=punch_start,
            end=punch_end,
            intensity=intensity,
            duration=duration,
            min_gap=config.effects_min_gap_seconds,
        ):
            punch_added = True
            visual_count += 1
            break

    for _, word in candidates:
        if visual_count >= config.effects_max_visual:
            break
        word_start = word.start - start_time
        if word_start < 0.5 or word_start > duration - 0.8:
            continue
        emphasis_start = word_start - 0.25
        emphasis_end = emphasis_start + config.effects_emphasis_duration
        if punch_added:
            punch = next((effect for effect in effects if effect.kind == "punch"), None)
            if punch is not None and emphasis_start < punch.end + config.effects_min_gap_seconds:
                continue
        intensity = config.effects_emphasis_intensity * (0.8 + 0.4 * importance)
        if _add_effect(
            effects,
            kind="emphasis",
            start=emphasis_start,
            end=emphasis_end,
            intensity=intensity,
            duration=duration,
            min_gap=config.effects_min_gap_seconds,
        ):
            visual_count += 1
            break

    if config.effects_transitions and duration >= 3.0 and cues:
        first = cues[0]
        last = cues[-1]
        first_word = first.words[0].text if first.words else first.text
        last_word = last.words[-1].text if last.words else last.text
        if first.start - start_time <= 0.25 and not _starts_continuation(first_word):
            _add_effect(
                effects,
                kind="fade_in",
                start=0.0,
                end=config.effects_transition_duration,
                intensity=1.0,
                duration=duration,
                min_gap=config.effects_min_gap_seconds,
            )
        if last.end - start_time >= duration - 0.35 and _ends_sentence(last_word):
            _add_effect(
                effects,
                kind="fade_out",
                start=duration - config.effects_transition_duration,
                end=duration,
                intensity=1.0,
                duration=duration,
                min_gap=config.effects_min_gap_seconds,
            )

    return sorted(effects, key=lambda item: (item.start, item.kind))


def _starts_continuation(text: str) -> bool:
    normalised = _normalise_word(text)
    return normalised in {"and", "but", "so", "because", "then", "or", "لكن", "ثم", "لأن"}


def build_edit_plan(
    transcript: TranscriptResult | None,
    highlight: Highlight | None,
    *,
    start_time: float,
    end_time: float,
    config: ClipConfig,
    signals: dict[str, object] | None = None,
    source_width: int | None = None,
    source_height: int | None = None,
) -> EditPlan:
    if transcript is None or end_time <= start_time:
        return EditPlan(notes=("No transcript was available; rendered without automatic editing.",))
    cues = build_caption_cues(
        transcript,
        start_time=start_time,
        end_time=end_time,
        config=config,
        highlight=highlight,
        signals=signals,
    )
    effects = _select_effects(
        cues=cues,
        highlight=highlight,
        signals=signals,
        start_time=start_time,
        end_time=end_time,
        config=config,
    )
    notes: list[str] = []
    if cues:
        notes.append(f"Generated {len(cues)} dynamic caption cues.")
    if effects:
        kinds = ", ".join(effect.kind for effect in effects)
        notes.append(f"Automatic effects: {kinds}.")
    elif config.effects_enabled:
        notes.append("No visual effects met the evidence threshold.")
    if source_width and source_height and source_width > source_height:
        notes.append("Landscape source detected for vertical framing.")
    return EditPlan(caption_cues=tuple(cues), effects=tuple(effects), notes=tuple(notes))
