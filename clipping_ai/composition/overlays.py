"""Render lightweight callouts to ASS so libass handles Arabic shaping."""

from __future__ import annotations

import re
from pathlib import Path

from clipping_ai.clips.config import ClipConfig
from clipping_ai.clips.editing import is_arabic_text
from clipping_ai.composition.models import Callout

_WHITESPACE_RE = re.compile(r"\s+")
_OVERLAY_BACK_COLOUR = "&H66000000"
_OVERLAY_PRIMARY_COLOUR = "&H00FFFFFF"


def _ass_time(seconds: float) -> str:
    total_cs = int(round(max(0.0, seconds) * 100))
    cs = total_cs % 100
    total_seconds = total_cs // 100
    s = total_seconds % 60
    total_minutes = total_seconds // 60
    m = total_minutes % 60
    h = total_minutes // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape(text: str) -> str:
    text = text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", r"\N")


def _wrap(text: str, max_chars: int) -> str:
    words = [word for word in _WHITESPACE_RE.split(text.strip()) if word]
    if not words:
        return ""
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
    return r"\N".join(_escape(line) for line in lines[:2])


def write_overlay_ass(
    callouts: tuple[Callout, ...],
    *,
    output_path: Path,
    width: int,
    height: int,
    config: ClipConfig,
) -> Path | None:
    if not callouts:
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    font_size = max(18, int(height * 0.042))
    margin_h = max(20, int(width * 0.06))
    margin_v = max(24, int(height * 0.12))
    outline = max(2.0, height * config.caption_box_padding_ratio)
    style_common = (
        f"{_OVERLAY_PRIMARY_COLOUR},&H000000FF,&H00000000,{_OVERLAY_BACK_COLOUR},"
        f"0,0,0,0,100,100,0,0,3,{outline:g},1,8,{margin_h},{margin_h},{margin_v},"
        f"{config.caption_encoding}"
    )
    styles = [
        f"Style: OverlayLatin,{config.caption_font_latin},{font_size},{style_common}",
        f"Style: OverlayArabic,{config.caption_font_arabic},{font_size},{style_common}",
    ]
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
        *styles,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for callout in callouts:
        style = "OverlayArabic" if is_arabic_text(callout.text) else "OverlayLatin"
        text = _wrap(callout.text, 34)
        if not text:
            continue
        alignment = {"top": 8, "center": 5, "bottom": 2}[callout.position]
        y = {"top": int(height * 0.14), "center": int(height * 0.45), "bottom": int(height * 0.8)}[
            callout.position
        ]
        tags = rf"{{\an{alignment}\pos({width // 2},{y})\fad(150,200)\b1}}"
        lines.append(
            f"Dialogue: 1,{_ass_time(callout.start)},{_ass_time(callout.end)},"
            f"{style},,0,0,0,,{tags}{text}"
        )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path
