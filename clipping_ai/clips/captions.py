"""ASS subtitle generation for clips.

libass performs Arabic shaping and bidirectional layout. Text is never reversed
manually here; doing so would break mixed Arabic/Latin/punctuation runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from clipping_ai.clips.config import ClipConfig
from clipping_ai.clips.editing import CaptionCue, CaptionWord, build_caption_cues, is_arabic_text
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.highlights.models import Highlight
from clipping_ai.schemas import TranscriptResult

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class CaptionEvent:
    start: float
    end: float
    text: str
    language: str | None = None
    words: tuple[CaptionWord, ...] = ()
    word_index: int = 0
    cue_index: int = 0
    important: bool = False


def resolve_caption_language(
    transcript: TranscriptResult,
    requested: str | None,
    *,
    text: str | None = None,
) -> str:
    language = (requested or transcript.language or "").strip().lower()
    if language:
        return language
    sample = text or transcript.text or ""
    return "ar" if is_arabic_text(sample) else "en"


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    total_cs = int(round(seconds * 100))
    cs = total_cs % 100
    total_seconds = total_cs // 100
    s = total_seconds % 60
    total_minutes = total_seconds // 60
    m = total_minutes % 60
    h = total_minutes // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape_ass(text: str) -> str:
    text = text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", r"\N")


def _wrap_lines(text: str, max_chars: int) -> list[str]:
    words = [word for word in _WHITESPACE_RE.split(text.strip()) if word]
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _split_chunks(text: str, max_chars: int, max_lines: int) -> list[str]:
    lines = _wrap_lines(text, max_chars)
    if not lines:
        return []
    chunks: list[str] = []
    for index in range(0, len(lines), max_lines):
        chunks.append(r"\N".join(_escape_ass(line) for line in lines[index : index + max_lines]))
    return chunks


def _has_word_timestamps(transcript: TranscriptResult) -> bool:
    if transcript.words:
        return True
    return any(segment.words for segment in transcript.segments)


def _font_for_language(language: str | None, config: ClipConfig) -> str:
    if language and language.lower().startswith("ar"):
        return config.caption_font_arabic
    return config.caption_font_latin


def _font_for_word(word: CaptionWord, config: ClipConfig) -> str:
    if is_arabic_text(word.text):
        return config.caption_font_arabic
    return config.caption_font_latin


def _word_tags(
    word: CaptionWord,
    *,
    current: bool,
    config: ClipConfig,
) -> str:
    font = _font_for_word(word, config)
    tags = [rf"\fn{font}"]
    if current:
        tags.append(rf"\c{config.caption_accent_colour}&")
        tags.append(r"\b1")
        tags.append(r"\fscx104\fscy104")
    elif word.important:
        tags.append(rf"\c{config.caption_emphasis_colour}&")
        tags.append(
            rf"\fscx{config.caption_emphasis_scale}\fscy{config.caption_emphasis_scale}"
        )
    return "{" + "".join(tags) + "}"


def _wrap_cue_words(words: tuple[CaptionWord, ...], max_chars: int) -> list[list[CaptionWord]]:
    lines: list[list[CaptionWord]] = []
    current: list[CaptionWord] = []
    current_length = 0
    for word in words:
        text = word.text.strip()
        if not text:
            continue
        candidate_length = current_length + (1 if current else 0) + len(text)
        if current and candidate_length > max_chars:
            lines.append(current)
            current = [word]
            current_length = len(text)
        else:
            current.append(word)
            current_length = candidate_length
    if current:
        lines.append(current)
    return lines


def _render_cue_text(
    cue: CaptionCue,
    *,
    current_index: int,
    config: ClipConfig,
    max_chars: int | None = None,
) -> str:
    max_chars = max_chars or config.caption_phrase_max_chars
    lines = _wrap_cue_words(cue.words, max_chars)
    if not lines:
        return _escape_ass(cue.text)
    rendered_lines: list[str] = []
    positions = {id(word): index for index, word in enumerate(cue.words)}
    for line in lines:
        rendered_words: list[str] = []
        for word in line:
            current = positions.get(id(word)) == current_index
            tags = _word_tags(word, current=current, config=config)
            rendered_words.append(f"{tags}{_escape_ass(word.text.strip())}{{\\r}}")
        rendered_lines.append(" ".join(rendered_words))
    return r"\N".join(rendered_lines)


def _events_from_cues(
    cues: list[CaptionCue],
    *,
    start_time: float,
    duration: float,
) -> list[CaptionEvent]:
    events: list[CaptionEvent] = []
    for cue_index, cue in enumerate(cues):
        for word_index, word in enumerate(cue.words):
            event_start = max(0.0, min(duration, word.start - start_time))
            if word_index + 1 < len(cue.words):
                next_start = cue.words[word_index + 1].start - start_time
            else:
                next_start = cue.end - start_time
            event_end = max(0.0, min(duration, next_start))
            if event_end <= event_start:
                event_end = min(duration, max(event_start + 0.05, word.end - start_time))
            if event_end <= event_start:
                continue
            events.append(
                CaptionEvent(
                    start=round(event_start, 3),
                    end=round(event_end, 3),
                    text=cue.text,
                    language=word.language or cue.language,
                    words=cue.words,
                    word_index=word_index,
                    cue_index=cue_index,
                    important=word.important,
                )
            )
    return events


def _legacy_events(
    transcript: TranscriptResult,
    *,
    start_time: float,
    end_time: float,
    config: ClipConfig,
    resolved_language: str,
) -> list[CaptionEvent]:
    duration = end_time - start_time
    source_segments = [
        segment
        for segment in transcript.segments
        if segment.text.strip() and segment.end > start_time and segment.start < end_time
    ]

    raw_events: list[CaptionEvent] = []
    if source_segments:
        for segment in source_segments:
            seg_start = max(segment.start, start_time) - start_time
            seg_end = min(segment.end, end_time) - start_time
            if seg_end <= seg_start:
                continue
            chunks = _split_chunks(
                segment.text,
                config.caption_max_chars_per_line,
                config.caption_max_lines,
            )
            if not chunks:
                continue
            chunk_duration = (seg_end - seg_start) / len(chunks)
            for index, chunk in enumerate(chunks):
                event_start = seg_start + index * chunk_duration
                event_end = seg_end if index == len(chunks) - 1 else event_start + chunk_duration
                raw_events.append(
                    CaptionEvent(
                        start=event_start,
                        end=event_end,
                        text=chunk,
                        language=resolved_language,
                    )
                )
    elif transcript.text.strip():
        chunks = _split_chunks(
            transcript.text,
            config.caption_max_chars_per_line,
            config.caption_max_lines,
        )
        if chunks:
            chunk_duration = duration / len(chunks)
            for index, chunk in enumerate(chunks):
                event_start = index * chunk_duration
                event_end = duration if index == len(chunks) - 1 else event_start + chunk_duration
                raw_events.append(
                    CaptionEvent(
                        start=event_start,
                        end=event_end,
                        text=chunk,
                        language=resolved_language,
                    )
                )

    events: list[CaptionEvent] = []
    for event in raw_events:
        event_start = max(0.0, min(event.start, duration))
        event_end = max(0.0, min(event.end, duration))
        if event_end - event_start < config.caption_min_duration:
            event_end = min(duration, event_start + config.caption_min_duration)
        if event_end - event_start > config.caption_max_duration:
            event_end = event_start + config.caption_max_duration
        if event_end > event_start:
            events.append(
                CaptionEvent(
                    start=event_start,
                    end=event_end,
                    text=event.text,
                    language=event.language,
                )
            )
    return events


def build_caption_events(
    transcript: TranscriptResult,
    *,
    start_time: float,
    end_time: float,
    config: ClipConfig,
    language: str | None = None,
    highlight: Highlight | None = None,
    signals: dict[str, object] | None = None,
) -> list[CaptionEvent]:
    duration = end_time - start_time
    if duration <= 0:
        return []
    resolved_language = resolve_caption_language(transcript, language)
    if config.dynamic_captions and _has_word_timestamps(transcript):
        cues = build_caption_cues(
            transcript,
            start_time=start_time,
            end_time=end_time,
            config=config,
            language=resolved_language,
            highlight=highlight,
            signals=signals,
        )
        dynamic_events = _events_from_cues(cues, start_time=start_time, duration=duration)
        if dynamic_events:
            return dynamic_events
    return _legacy_events(
        transcript,
        start_time=start_time,
        end_time=end_time,
        config=config,
        resolved_language=resolved_language,
    )


def write_ass_file(
    events: list[CaptionEvent],
    *,
    output_path: Path,
    width: int,
    height: int,
    config: ClipConfig,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    font_size = config.caption_font_size or max(16, int(height * config.caption_font_size_ratio))
    margin_v = max(
        int(height * config.caption_margin_v_ratio),
        int(height * config.caption_safe_area_ratio),
    )
    margin_h = max(20, int(width * 0.04))
    font_name = _font_for_language(events[0].language if events else None, config)
    border_style = config.caption_border_style
    outline = (
        max(2.0, height * config.caption_box_padding_ratio)
        if border_style == 3
        else config.caption_outline
    )
    style = (
        f"Style: Default,{font_name},{font_size},"
        f"{config.caption_primary_colour},&H000000FF,"
        f"{config.caption_outline_colour},{config.caption_back_colour},"
        f"0,0,0,0,100,100,0,0,{border_style},{outline:g},{config.caption_shadow},"
        f"2,{margin_h},{margin_h},{margin_v},{config.caption_encoding}"
    )
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding"
        ),
        style,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for event in events:
        if event.words:
            text = _render_cue_text(
                CaptionCue(
                    start=event.start,
                    end=event.end,
                    text=event.text,
                    language=event.language,
                    words=event.words,
                ),
                current_index=event.word_index,
                config=config,
            )
        else:
            text = event.text
        lines.append(
            f"Dialogue: 0,{_ass_time(event.start)},{_ass_time(event.end)},"
            f"Default,,0,0,0,,{text}"
        )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path


def build_ass_file(
    transcript: TranscriptResult,
    *,
    start_time: float,
    end_time: float,
    output_path: Path,
    width: int,
    height: int,
    config: ClipConfig,
    language: str | None = None,
    highlight: Highlight | None = None,
    signals: dict[str, object] | None = None,
) -> tuple[Path, str]:
    events = build_caption_events(
        transcript,
        start_time=start_time,
        end_time=end_time,
        config=config,
        language=language,
        highlight=highlight,
        signals=signals,
    )
    if not events:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "Captions were requested but no transcript text overlaps this clip.",
            retryable=False,
        )
    resolved = resolve_caption_language(transcript, language)
    write_ass_file(events, output_path=output_path, width=width, height=height, config=config)
    return output_path, resolved
