from __future__ import annotations

import json
import subprocess
from itertools import pairwise
from pathlib import Path

from clipping_ai.clips.captions import (
    CaptionEvent,
    build_ass_file,
    build_caption_events,
    write_ass_file,
)
from clipping_ai.clips.editing import (
    CaptionWord,
    EditPlan,
    Effect,
    build_caption_cues,
    build_edit_plan,
)
from clipping_ai.clips.models import ClipFit, ClipFormat
from clipping_ai.clips.renderer import build_filter_graph, render_clip
from clipping_ai.highlights.models import ContentType, Highlight, HighlightResult
from clipping_ai.schemas import TranscriptResult, TranscriptWord
from tests.test_clips import (
    add_transcript_and_highlight,
    ffprobe_streams,
    make_clip_job,
    make_clip_service,
    make_test_video,
    requires_ffmpeg,
    small_clip_config,
)

SIGNALS = {
    "mean_motion": 0.1,
    "active_audio_ratio": 0.9,
    "mean_energy": 0.7,
    "scene_rate_per_minute": 0.5,
}


def make_words(items: list[tuple[str, float, float]]) -> list[TranscriptWord]:
    return [TranscriptWord(word=word, start=start, end=end) for word, start, end in items]


def make_transcript(
    items: list[tuple[str, float, float]],
    *,
    language: str = "en",
    duration: float | None = None,
) -> TranscriptResult:
    return TranscriptResult(
        text=" ".join(word for word, _, _ in items),
        language=language,
        duration=duration or items[-1][2] + 0.5,
        words=make_words(items),
    )


def make_highlight(
    *,
    start: float = 0.0,
    end: float = 4.0,
    confidence: float = 0.92,
    title: str = "Important moment",
) -> Highlight:
    return Highlight(
        start_time=start,
        end_time=end,
        title=title,
        description="remember this important moment",
        selection_reason="important",
        confidence=confidence,
        category="test",
        signal_score=0.8,
        detection_signals={"semantic_completeness": 0.9},
    )


def render_ass_frame(
    ass_path: Path,
    *,
    width: int,
    height: int,
    source: str = "black",
) -> bytes:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c={source}:s={width}x{height}:d=1:r=1",
        "-vf",
        f"ass={ass_path}:shaping=complex",
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-",
    ]
    completed = subprocess.run(command, check=True, capture_output=True)
    return completed.stdout


def colour_bbox(
    raw: bytes,
    *,
    width: int,
    height: int,
    predicate,
) -> tuple[int, int, int, int] | None:
    min_x, min_y = width, height
    max_x = max_y = -1
    for y in range(height):
        row = y * width * 3
        for x in range(width):
            offset = row + x * 3
            if predicate(raw[offset], raw[offset + 1], raw[offset + 2]):
                min_x = min(min_x, x)
                max_x = max(max_x, x)
                min_y = min(min_y, y)
                max_y = max(max_y, y)
    if max_x < 0:
        return None
    return min_x, min_y, max_x, max_y


def bbox_center_x(bbox: tuple[int, int, int, int]) -> float:
    return (bbox[0] + bbox[2]) / 2.0


def is_accent(r: int, g: int, b: int) -> bool:
    return r > 180 and g > 170 and b < 90


def is_emphasis(r: int, g: int, b: int) -> bool:
    return r > 160 and g < 90 and b < 90


def test_dynamic_caption_events_sync_to_words_and_accent_current_word(tmp_path):
    items = [
        ("This", 0.2, 0.45),
        ("is", 0.45, 0.6),
        ("a", 0.6, 0.7),
        ("very", 0.7, 0.95),
        ("important", 0.95, 1.45),
        ("moment", 1.45, 1.8),
        ("for", 1.8, 1.95),
        ("the", 1.95, 2.1),
        ("team.", 2.1, 2.4),
    ]
    transcript = make_transcript(items, duration=3.0)
    config = small_clip_config(caption_phrase_max_words=9, caption_phrase_max_chars=100)
    highlight = make_highlight(start=0.2, end=2.4)
    events = build_caption_events(
        transcript,
        start_time=0.2,
        end_time=2.4,
        config=config,
        highlight=highlight,
        signals=SIGNALS,
    )

    assert len(events) == len(items)
    assert [round(event.start, 2) for event in events[:5]] == [0.0, 0.25, 0.4, 0.5, 0.75]
    assert events[0].word_index == 0
    assert events[4].words[4].text == "important"
    assert events[4].words[4].important is True

    output = tmp_path / "captions.ass"
    path, language = build_ass_file(
        transcript,
        start_time=0.2,
        end_time=2.4,
        output_path=output,
        width=180,
        height=320,
        config=config,
        highlight=highlight,
        signals=SIGNALS,
    )
    text = path.read_text(encoding="utf-8")
    assert language == "en"
    assert text.count("Dialogue:") == len(events)
    assert r"\c&H0000E5FF&" in text
    assert r"\b1" in text
    assert r"\c&H0000D7FF&" in text
    assert r"\fscx112\fscy112" in text
    assert "important" in text


def test_dynamic_mixed_arabic_captions_use_arabic_and_latin_fonts(tmp_path):
    items = [
        ("هذا", 0.2, 0.5),
        ("مهم", 0.5, 0.8),
        ("and", 0.8, 1.0),
        ("important", 1.0, 1.4),
        ("لذلك", 1.4, 1.8),
    ]
    transcript = make_transcript(items, language="ar", duration=2.0)
    config = small_clip_config(caption_font_size_ratio=0.06)
    highlight = make_highlight(start=0.2, end=1.8, title="«مهم»")
    path, language = build_ass_file(
        transcript,
        start_time=0.2,
        end_time=1.8,
        output_path=tmp_path / "captions.ass",
        width=180,
        height=320,
        config=config,
        highlight=highlight,
        signals=SIGNALS,
    )
    text = path.read_text(encoding="utf-8")
    assert language == "ar"
    assert r"\fnNoto Sans Arabic" in text
    assert r"\fnNoto Sans" in text
    assert "هذا" in text
    assert "هذا"[::-1] not in text
    assert r"\c&H0000D7FF&" in text


def test_caption_cues_group_by_words_gaps_and_sentences():
    items = [
        ("one", 0.0, 0.4),
        ("two", 0.4, 0.8),
        ("three", 0.8, 1.2),
        ("four.", 1.2, 1.6),
        ("five", 1.6, 2.0),
        ("six", 3.0, 3.4),
    ]
    transcript = make_transcript(items, duration=4.0)
    config = small_clip_config(
        caption_phrase_max_words=4,
        caption_phrase_max_chars=80,
        caption_break_gap_seconds=0.5,
    )
    cues = build_caption_cues(
        transcript,
        start_time=0.0,
        end_time=4.0,
        config=config,
        highlight=make_highlight(),
        signals=SIGNALS,
    )
    assert [len(cue.words) for cue in cues] == [4, 1, 1]
    assert cues[0].text.endswith("four.")
    assert cues[1].text == "five"


def test_dynamic_caption_events_do_not_overlap():
    items = [
        ("One.", 0.0, 0.12),
        ("Two", 0.12, 0.24),
        ("three", 0.24, 0.36),
        ("four.", 0.36, 0.5),
        ("Five", 0.5, 0.65),
        ("six.", 0.65, 0.8),
    ]
    transcript = make_transcript(items, duration=1.0)
    events = build_caption_events(
        transcript,
        start_time=0.0,
        end_time=0.8,
        config=small_clip_config(caption_phrase_max_words=2),
        highlight=make_highlight(start=0.0, end=0.8),
        signals=SIGNALS,
    )
    assert len(events) == len(items)
    assert all(left.end <= right.start + 1e-6 for left, right in pairwise(events))


def test_dynamic_captions_use_safe_area_and_wrap_long_cues(tmp_path):
    config = small_clip_config(
        caption_safe_area_ratio=0.1,
        caption_phrase_max_chars=12,
    )
    words = tuple(
        CaptionWord(text=word, start=index * 0.3, end=index * 0.3 + 0.25)
        for index, word in enumerate(["one", "two", "three", "four", "five", "six"])
    )
    event = CaptionEvent(
        start=0.0,
        end=1.8,
        text="one two three four five six",
        language="en",
        words=words,
        word_index=0,
    )
    path = write_ass_file(
        [event],
        output_path=tmp_path / "captions.ass",
        width=180,
        height=320,
        config=config,
    )
    text = path.read_text(encoding="utf-8")
    style_line = next(line for line in text.splitlines() if line.startswith("Style: Default"))
    fields = style_line.split(",")
    assert fields[-2] == "32"
    assert fields[15] == "3"
    assert fields[22] == "-1"
    assert r"\N" in text
    assert "WrapStyle: 0" in text


def test_important_words_are_gated_by_confidence_and_restrained():
    items = [
        ("This", 0.0, 0.3),
        ("is", 0.3, 0.5),
        ("very", 0.5, 0.8),
        ("important", 0.8, 1.3),
        ("for", 1.3, 1.5),
        ("everyone", 1.5, 2.0),
    ]
    transcript = make_transcript(items, duration=2.5)
    config = small_clip_config()
    low = build_caption_cues(
        transcript,
        start_time=0.0,
        end_time=2.0,
        config=config,
        highlight=make_highlight(confidence=0.2),
        signals={},
    )
    assert not any(word.important for cue in low for word in cue.words)

    high = build_caption_cues(
        transcript,
        start_time=0.0,
        end_time=2.0,
        config=config,
        highlight=make_highlight(),
        signals=SIGNALS,
    )
    assert all(sum(1 for word in cue.words if word.important) <= 1 for cue in high)
    assert any(word.important for cue in high for word in cue.words)


def test_effects_activate_from_importance_and_important_words():
    items = [
        ("This", 0.2, 0.6),
        ("is", 0.6, 0.9),
        ("the", 0.9, 1.2),
        ("important", 8.0, 8.5),
        ("part.", 8.5, 9.0),
        ("end.", 19.5, 19.9),
    ]
    transcript = make_transcript(items, duration=20.0)
    config = small_clip_config(
        effects_zoom_min_duration=2.0,
        effects_zoom_duration=4.0,
        effects_max_visual=2,
    )
    plan = build_edit_plan(
        transcript,
        make_highlight(start=0.0, end=20.0),
        start_time=0.0,
        end_time=20.0,
        config=config,
        signals=SIGNALS,
    )
    kinds = {effect.kind for effect in plan.effects}
    assert "zoom" in kinds
    assert "punch" in kinds
    assert "fade_in" in kinds
    assert "fade_out" in kinds
    zoom = next(effect for effect in plan.effects if effect.kind == "zoom")
    punch = next(effect for effect in plan.effects if effect.kind == "punch")
    assert zoom.start == 0.4
    assert 7.6 <= punch.start <= 7.7
    assert punch.end <= 8.6


def test_effects_fall_back_when_evidence_is_insufficient():
    items = [("A", 0.2, 0.5), ("short", 0.5, 0.9), ("clip.", 0.9, 1.2)]
    transcript = make_transcript(items, duration=3.0)
    config = small_clip_config()
    plan = build_edit_plan(
        transcript,
        make_highlight(start=0.0, end=3.0, confidence=0.15),
        start_time=0.0,
        end_time=3.0,
        config=config,
        signals={},
    )
    assert plan.effects == ()
    assert any("No visual effects" in note for note in plan.notes)

    disabled = build_edit_plan(
        transcript,
        make_highlight(start=0.0, end=3.0),
        start_time=0.0,
        end_time=3.0,
        config=small_clip_config(effects_enabled=False),
        signals=SIGNALS,
    )
    assert disabled.effects == ()


def test_effects_do_not_stack_consecutive_punches():
    items = [
        ("This", 0.2, 0.6),
        ("is", 0.6, 0.9),
        ("important", 2.0, 2.4),
        ("and", 2.4, 2.6),
        ("crucial.", 2.6, 3.0),
    ]
    transcript = make_transcript(items, duration=10.0)
    plan = build_edit_plan(
        transcript,
        make_highlight(start=0.0, end=10.0),
        start_time=0.0,
        end_time=10.0,
        config=small_clip_config(effects_max_visual=4),
        signals=SIGNALS,
    )
    punches = [effect for effect in plan.effects if effect.kind == "punch"]
    assert len(punches) == 1


def test_filter_graph_includes_effects_before_captions():
    job = make_clip_job(
        start_time=0.0,
        end_time=20.0,
        caption_path=Path("captions.ass"),
    )
    config = small_clip_config()
    plan = EditPlan(
        effects=(
            Effect(kind="zoom", start=0.4, end=4.4, intensity=0.04),
            Effect(kind="punch", start=7.5, end=8.3, intensity=0.06),
            Effect(kind="emphasis", start=10.0, end=10.8, intensity=0.03),
            Effect(kind="fade_in", start=0.0, end=0.3, intensity=1.0),
            Effect(kind="fade_out", start=19.7, end=20.0, intensity=1.0),
        )
    )
    graph = build_filter_graph(job, config, plan)
    assert "zoompan=" in graph
    assert "eq=brightness=" in graph
    assert "fade=t=in" in graph
    assert "fade=t=out" in graph
    assert "ass=filename=captions.ass" in graph
    assert "[veff]" in graph
    assert graph.index("[veff]") < graph.index("ass=filename")

    plain = build_filter_graph(job, config, EditPlan())
    assert "zoompan=" not in plain
    assert "eq=" not in plain
    assert "fade=" not in plain
    assert "ass=filename=captions.ass" in plain


def test_auto_vertical_fit_for_landscape_source(tmp_path):
    video, clips = make_clip_service(tmp_path)
    source = make_test_video(tmp_path / "sample.mp4", duration=3.0)
    job = video.prepare_local(source)
    add_transcript_and_highlight(video, job)

    automatic = clips.create_for_job(job.id, formats=[ClipFormat.VERTICAL])[0]
    explicit = clips.create_for_job(
        job.id,
        formats=[ClipFormat.VERTICAL],
        fit="crop",
    )[0]
    square = clips.create_for_job(job.id, formats=[ClipFormat.SQUARE])[0]
    assert automatic.fit.value == "blur"
    assert explicit.fit.value == "crop"
    assert square.fit.value == "crop"


def raw_frame_at(path: Path, timestamp: float, *, width: int, height: int) -> bytes:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-ss",
            f"{timestamp:.3f}",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
    )
    return completed.stdout


def white_bbox(raw: bytes, *, width: int, height: int) -> tuple[int, int, int, int] | None:
    return colour_bbox(
        raw,
        width=width,
        height=height,
        predicate=lambda r, g, b: r > 200 and g > 200 and b > 200,
    )


@requires_ffmpeg
def test_real_render_makes_zoom_and_punch_measurable(tmp_path):
    source = tmp_path / "square.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=180x320:d=5:r=30",
            "-vf",
            "drawbox=x=75:y=130:w=30:h=60:color=white:t=fill",
            "-c:v",
            "mpeg4",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    job = make_clip_job(
        start_time=0.0,
        end_time=5.0,
        width=180,
        height=320,
        captions=False,
        caption_path=None,
        fit=ClipFit.CROP,
    )
    config = small_clip_config()
    plan = EditPlan(
        effects=(
            Effect(kind="zoom", start=0.4, end=4.4, intensity=0.10),
            Effect(kind="punch", start=2.0, end=2.9, intensity=0.12),
        )
    )
    output = tmp_path / "effects.mp4"
    render_clip(
        job,
        source_path=source,
        output_path=output,
        config=config,
        edit_plan=plan,
    )
    start = white_bbox(raw_frame_at(output, 0.0, width=180, height=320), width=180, height=320)
    punch = white_bbox(raw_frame_at(output, 2.45, width=180, height=320), width=180, height=320)
    zoom = white_bbox(raw_frame_at(output, 4.4, width=180, height=320), width=180, height=320)
    assert start is not None and punch is not None and zoom is not None
    start_width = start[2] - start[0]
    punch_width = punch[2] - punch[0]
    zoom_width = zoom[2] - zoom[0]
    assert zoom_width >= start_width + 1
    assert punch_width >= start_width + 2


def test_caption_style_uses_large_font_background_and_whole_text_bidi(tmp_path):
    config = small_clip_config()
    event = CaptionEvent(start=0.0, end=1.0, text="Readable caption", language="en")
    path = write_ass_file(
        [event],
        output_path=tmp_path / "captions.ass",
        width=1080,
        height=1920,
        config=config,
    )
    text = path.read_text(encoding="utf-8")
    style_line = next(line for line in text.splitlines() if line.startswith("Style: Default"))
    fields = style_line.split(",")
    expected_font = max(16, int(1920 * config.caption_font_size_ratio))
    assert int(fields[2]) == expected_font
    assert expected_font > int(1920 * 0.045)
    assert fields[6] == "&H50000000"
    assert fields[15] == "3"
    assert float(fields[16]) > 10
    assert fields[22] == "-1"
    assert "WrapStyle: 0" in text


@requires_ffmpeg
def test_rendered_arabic_caption_orders_first_word_on_the_right(tmp_path):
    items = [
        ("فلولا", 0.0, 0.5),
        ("أنه", 0.5, 0.9),
        ("كان", 0.9, 1.2),
        ("من", 1.2, 1.5),
        ("المسبحين", 1.5, 2.0),
    ]
    transcript = make_transcript(items, language="ar", duration=2.0)
    config = small_clip_config(
        caption_font_size_ratio=0.08,
        caption_emphasis_colour="&H000000FF",
        caption_phrase_max_words=5,
        caption_phrase_max_chars=100,
    )
    highlight = make_highlight(start=0.0, end=2.0, confidence=0.95, title="المسبحين")
    path, _ = build_ass_file(
        transcript,
        start_time=0.0,
        end_time=2.0,
        output_path=tmp_path / "captions.ass",
        width=540,
        height=960,
        config=config,
        highlight=highlight,
        signals=SIGNALS,
    )
    raw = render_ass_frame(path, width=540, height=960)
    accent = colour_bbox(raw, width=540, height=960, predicate=is_accent)
    emphasis = colour_bbox(raw, width=540, height=960, predicate=is_emphasis)
    assert accent is not None
    assert emphasis is not None
    assert bbox_center_x(accent) > bbox_center_x(emphasis) + 20
    assert accent[2] > 540 * 0.55
    assert emphasis[0] < 540 * 0.45


@requires_ffmpeg
def test_rendered_mixed_caption_preserves_rtl_word_order(tmp_path):
    items = [("هذا", 0.0, 0.5), ("important", 0.5, 1.0), ("جدا.", 1.0, 1.5)]
    transcript = make_transcript(items, language="ar", duration=1.5)
    config = small_clip_config(
        caption_font_size_ratio=0.08,
        caption_emphasis_colour="&H000000FF",
        caption_phrase_max_words=5,
        caption_phrase_max_chars=100,
    )
    highlight = make_highlight(start=0.0, end=1.5, confidence=0.95, title="important")
    path, _ = build_ass_file(
        transcript,
        start_time=0.0,
        end_time=1.5,
        output_path=tmp_path / "captions.ass",
        width=540,
        height=960,
        config=config,
        highlight=highlight,
        signals=SIGNALS,
    )
    raw = render_ass_frame(path, width=540, height=960)
    accent = colour_bbox(raw, width=540, height=960, predicate=is_accent)
    emphasis = colour_bbox(raw, width=540, height=960, predicate=is_emphasis)
    assert accent is not None
    assert emphasis is not None
    assert bbox_center_x(accent) > bbox_center_x(emphasis) + 20


@requires_ffmpeg
def test_rendered_caption_has_readable_background_box(tmp_path):
    config = small_clip_config(caption_font_size_ratio=0.1)
    event = CaptionEvent(start=0.0, end=1.0, text="Readable", language="en")
    path = write_ass_file(
        [event],
        output_path=tmp_path / "captions.ass",
        width=540,
        height=960,
        config=config,
    )
    raw = render_ass_frame(path, width=540, height=960, source="white")
    box = colour_bbox(
        raw,
        width=540,
        height=960,
        predicate=lambda r, g, b: r < 140 and g < 140 and b < 140,
    )
    assert box is not None
    assert box[2] - box[0] > 120
    assert box[3] - box[1] > 40


@requires_ffmpeg
def test_real_render_with_dynamic_captions_and_effects(tmp_path):
    config = small_clip_config(
        effects_zoom_min_duration=1.5,
        effects_zoom_duration=1.0,
    )
    video, clips = make_clip_service(tmp_path, config=config)
    source = make_test_video(tmp_path / "sample.mp4", duration=3.0)
    job = video.prepare_local(source)
    items = [
        ("This", 0.2, 0.5),
        ("is", 0.5, 0.7),
        ("very", 0.7, 1.0),
        ("important.", 1.0, 1.5),
        ("Remember", 1.6, 2.0),
        ("this.", 2.0, 2.5),
    ]
    transcript = make_transcript(items, duration=3.0)
    highlight = make_highlight(start=0.3, end=2.7)
    video.store.update(
        job.id,
        transcript=transcript,
        highlight_result=HighlightResult(
            job_id=job.id,
            content_type=ContentType.PODCAST,
            language="en",
            duration=3.0,
            highlights=[highlight],
            signals=SIGNALS,
        ),
    )
    clip = clips.create_for_job(job.id, formats=[ClipFormat.VERTICAL], captions=True)[0]
    finished = clips.process(clip.id)
    assert finished.status.value == "ready", finished.error
    assert finished.caption_path is not None and finished.caption_path.is_file()
    assert finished.output_path is not None and finished.output_path.is_file()
    caption_text = finished.caption_path.read_text(encoding="utf-8")
    assert r"\c&H0000E5FF&" in caption_text
    assert r"\c&H0000D7FF&" in caption_text
    diagnostics_path = finished.output_path.parent / "render_diagnostics.json"
    assert diagnostics_path.is_file()
    diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
    assert diagnostics["effects"]
    assert "zoompan=" in diagnostics["filter_graph"]
    assert diagnostics["caption_style"]["encoding"] == -1
    assert diagnostics["caption_style"]["border_style"] == 3
    streams = ffprobe_streams(finished.output_path)
    video_stream = next(s for s in streams["streams"] if s["codec_type"] == "video")
    assert (video_stream["width"], video_stream["height"]) == (180, 320)
    assert any(s["codec_type"] == "audio" for s in streams["streams"])


@requires_ffmpeg
def test_real_render_with_dynamic_arabic_captions(tmp_path):
    video, clips = make_clip_service(tmp_path)
    source = make_test_video(tmp_path / "sample.mp4", duration=3.0)
    job = video.prepare_local(source)
    items = [
        ("هذا", 0.2, 0.6),
        ("اختبار", 0.6, 1.1),
        ("مهم", 1.1, 1.6),
        ("جدا.", 1.6, 2.2),
    ]
    transcript = make_transcript(items, language="ar", duration=3.0)
    highlight = make_highlight(start=0.3, end=2.4, title="اختبار مهم")
    video.store.update(
        job.id,
        transcript=transcript,
        highlight_result=HighlightResult(
            job_id=job.id,
            content_type=ContentType.PODCAST,
            language="ar",
            duration=3.0,
            highlights=[highlight],
            signals=SIGNALS,
        ),
    )
    clip = clips.create_for_job(
        job.id,
        formats=[ClipFormat.VERTICAL],
        captions=True,
        caption_language="ar",
    )[0]
    finished = clips.process(clip.id)
    assert finished.status.value == "ready", finished.error
    assert finished.caption_path is not None
    caption_text = finished.caption_path.read_text(encoding="utf-8")
    assert "هذا" in caption_text
    assert r"\fnNoto Sans Arabic" in caption_text
    assert finished.output_path is not None and finished.output_path.is_file()
