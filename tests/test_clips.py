from __future__ import annotations

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clipping_ai.api.app import create_app
from clipping_ai.clips.captions import build_ass_file, build_caption_events
from clipping_ai.clips.config import ClipConfig, ClipFormatProfile, load_clip_config
from clipping_ai.clips.models import (
    ClipFit,
    ClipFormat,
    ClipJob,
    ClipQuality,
    ClipRange,
    ClipStage,
    ClipStatus,
)
from clipping_ai.clips.renderer import probe_media, render_clip
from clipping_ai.clips.service import ClipService
from clipping_ai.clips.worker import ClipWorker
from clipping_ai.errors import AIError
from clipping_ai.highlights.models import ContentType, Highlight, HighlightResult
from clipping_ai.schemas import TranscriptResult, TranscriptSegment
from clipping_ai.settings import Settings
from clipping_ai.video.config import VideoConfig
from clipping_ai.video.service import VideoService
from tests.video_helpers import make_service, make_test_video, requires_ffmpeg


def small_clip_config(**overrides) -> ClipConfig:
    values = {
        "formats": {
            "vertical": ClipFormatProfile(width=180, height=320),
            "square": ClipFormatProfile(width=180, height=180),
            "landscape": ClipFormatProfile(width=320, height=180),
        },
        "min_free_disk_bytes": 0,
        "render_timeout_seconds": 60,
        "caption_font_size_ratio": 0.06,
        "caption_max_chars_per_line": 24,
    }
    values.update(overrides)
    return ClipConfig(**values)


def add_transcript_and_highlight(
    video,
    job,
    *,
    text: str = "Hello world this is a test clip.",
    language: str = "en",
    start: float = 0.2,
    end: float = 2.8,
    highlight_start: float = 0.3,
    highlight_end: float = 2.7,
) -> TranscriptResult:
    transcript = TranscriptResult(
        text=text,
        language=language,
        duration=job.metadata.duration_seconds if job.metadata else end,
        segments=[TranscriptSegment(id=0, start=start, end=end, text=text)],
    )
    highlight = Highlight(
        start_time=highlight_start,
        end_time=highlight_end,
        title="Test highlight",
        description="Test description",
        selection_reason="Test reason",
        confidence=0.9,
        category="test",
        transcript_segments=transcript.segments,
    )
    video.store.update(
        job.id,
        transcript=transcript,
        highlight_result=HighlightResult(
            job_id=job.id,
            content_type=ContentType.PODCAST,
            language=language,
            duration=job.metadata.duration_seconds if job.metadata else end,
            highlights=[highlight],
        ),
    )
    return transcript


def add_highlights(video, job, ranges: list[tuple[float, float]]) -> TranscriptResult:
    duration = job.metadata.duration_seconds if job.metadata else ranges[-1][1]
    text = "Hello world this is a test clip."
    transcript = TranscriptResult(
        text=text,
        language="en",
        duration=duration,
        segments=[TranscriptSegment(id=0, start=0.0, end=duration, text=text)],
    )
    highlights = [
        Highlight(
            start_time=start,
            end_time=end,
            title=f"Test highlight {index}",
            description="Test description",
            selection_reason="Test reason",
            confidence=0.9,
            category="test",
            transcript_segments=transcript.segments,
        )
        for index, (start, end) in enumerate(ranges)
    ]
    video.store.update(
        job.id,
        transcript=transcript,
        highlight_result=HighlightResult(
            job_id=job.id,
            content_type=ContentType.PODCAST,
            language="en",
            duration=duration,
            highlights=highlights,
        ),
    )
    return transcript


def make_clip_service(tmp_path: Path, *, config: ClipConfig | None = None):
    video = make_service(tmp_path)
    service = ClipService(
        video.settings,
        video_service=video,
        config=config or small_clip_config(),
    )
    return video, service


def make_clip_job(clip_id: str = "a" * 32, **overrides) -> ClipJob:
    now = datetime.now(UTC).isoformat()
    values = {
        "id": clip_id,
        "source_job_id": "b" * 32,
        "start_time": 1.0,
        "end_time": 3.0,
        "format": ClipFormat.VERTICAL,
        "width": 1080,
        "height": 1920,
        "fps": 30.0,
        "quality": ClipQuality.STANDARD,
        "fit": ClipFit.CROP,
        "captions": True,
        "caption_language": "ar",
        "status": ClipStatus.QUEUED,
        "stage": ClipStage.QUEUED,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return ClipJob(**values)


def ffprobe_streams(path: Path) -> dict:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def mean_volume(path: Path, *, start: float | None = None, duration: float | None = None) -> float:
    command = ["ffmpeg", "-hide_banner", "-nostdin"]
    if start is not None:
        command.extend(["-ss", f"{start:.3f}"])
    command.extend(["-i", str(path)])
    if duration is not None:
        command.extend(["-t", f"{duration:.3f}"])
    command.extend(["-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"])
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    match = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", completed.stderr)
    assert match is not None
    return float(match.group(1))


def test_clip_config_defaults_and_file(tmp_path):
    config = ClipConfig()
    assert config.format_profile(ClipFormat.VERTICAL).model_dump() == {
        "width": 1080,
        "height": 1920,
    }
    assert config.format_profile(ClipFormat.SQUARE).width == 1080
    assert config.format_profile(ClipFormat.LANDSCAPE).height == 1080
    assert config.quality_profile(ClipQuality.STANDARD).crf == 23
    assert config.default_formats == [ClipFormat.VERTICAL]

    path = tmp_path / "config" / "clips.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "clips": {
                    "default_quality": "high",
                    "formats": {
                        "vertical": {"width": 720, "height": 1280},
                        "square": {"width": 720, "height": 720},
                        "landscape": {"width": 1280, "height": 720},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    loaded = load_clip_config(tmp_path / "config")
    assert loaded.default_quality == ClipQuality.HIGH
    assert loaded.format_profile(ClipFormat.VERTICAL).height == 1280


def test_arabic_captions_preserve_rtl_text_and_use_arabic_font(tmp_path):
    transcript = TranscriptResult(
        text="مرحبا بالعالم، هذا اختبار.",
        language="ar",
        duration=3.0,
        segments=[
            TranscriptSegment(id=0, start=0.0, end=3.0, text="مرحبا بالعالم، هذا اختبار.")
        ],
    )
    config = small_clip_config(caption_max_chars_per_line=80)
    output = tmp_path / "captions.ass"
    path, language = build_ass_file(
        transcript,
        start_time=0.0,
        end_time=3.0,
        output_path=output,
        width=180,
        height=320,
        config=config,
    )
    text = path.read_text(encoding="utf-8")
    assert language == "ar"
    assert "Noto Sans Arabic" in text
    assert "مرحبا بالعالم، هذا اختبار." in text
    assert "مرحبا بالعالم، هذا اختبار."[::-1] not in text
    assert "Dialogue:" in text


def test_caption_events_clip_to_range_and_split_long_text():
    transcript = TranscriptResult(
        text="one two three four five six seven eight nine ten",
        language="en",
        duration=10.0,
        segments=[
            TranscriptSegment(
                id=0,
                start=1.0,
                end=9.0,
                text="one two three four five six seven eight nine ten",
            )
        ],
    )
    config = small_clip_config(caption_max_chars_per_line=10, caption_max_lines=1)
    events = build_caption_events(
        transcript,
        start_time=2.0,
        end_time=6.0,
        config=config,
    )
    assert len(events) > 1
    assert all(0 <= event.start < event.end <= 4.0 for event in events)
    assert all(len(event.text.replace(r"\N", "")) <= 10 for event in events)


@requires_ffmpeg
def test_render_all_formats_preserves_audio_and_writes_captions(tmp_path):
    video, clips = make_clip_service(tmp_path)
    source = make_test_video(tmp_path / "sample.mp4", duration=3.0)
    job = video.prepare_local(source)
    add_transcript_and_highlight(video, job)

    created = clips.create_for_job(
        job.id,
        formats=[ClipFormat.VERTICAL, ClipFormat.SQUARE, ClipFormat.LANDSCAPE],
        captions=True,
    )
    assert len(created) == 3
    assert {clip.format for clip in created} == {
        ClipFormat.VERTICAL,
        ClipFormat.SQUARE,
        ClipFormat.LANDSCAPE,
    }

    source_volume = mean_volume(source, start=0.3, duration=2.4)
    expected_sizes = {
        ClipFormat.VERTICAL: (180, 320),
        ClipFormat.SQUARE: (180, 180),
        ClipFormat.LANDSCAPE: (320, 180),
    }
    for clip in created:
        finished = clips.process(clip.id)
        assert finished.status == ClipStatus.READY, finished.error
        assert finished.output_path is not None and finished.output_path.is_file()
        assert finished.caption_path is not None and finished.caption_path.is_file()
        assert finished.size_bytes and finished.size_bytes > 0
        assert "Hello world" in finished.caption_path.read_text(encoding="utf-8")

        streams = ffprobe_streams(finished.output_path)
        video_stream = next(s for s in streams["streams"] if s["codec_type"] == "video")
        audio_stream = next(s for s in streams["streams"] if s["codec_type"] == "audio")
        assert (video_stream["width"], video_stream["height"]) == expected_sizes[clip.format]
        assert video_stream["codec_name"] == "h264"
        assert audio_stream["codec_name"] == "aac"
        assert audio_stream["channels"] == 1
        assert abs(mean_volume(finished.output_path) - source_volume) < 8.0

        info = probe_media(finished.output_path, clips.config)
        assert info.has_audio
        assert info.duration >= 2.0


@pytest.mark.parametrize(
    ("clip_format", "expected_size"),
    [
        (ClipFormat.VERTICAL, (180, 320)),
        (ClipFormat.SQUARE, (180, 180)),
        (ClipFormat.LANDSCAPE, (320, 180)),
    ],
)
@requires_ffmpeg
def test_captions_disabled_has_no_ass_file(tmp_path, clip_format, expected_size):
    video, clips = make_clip_service(tmp_path)
    source = make_test_video(tmp_path / "sample.mp4", duration=2.0)
    job = video.prepare_local(source)
    add_transcript_and_highlight(video, job, highlight_start=0.2, highlight_end=1.8)
    clip = clips.create_for_job(job.id, formats=[clip_format], captions=False)[0]
    finished = clips.process(clip.id)
    assert finished.status == ClipStatus.READY, finished.error
    assert finished.caption_path is None
    assert not (clips.clip_dir(clip.id) / "captions.ass").exists()
    assert finished.output_path is not None
    streams = ffprobe_streams(finished.output_path)
    video_stream = next(s for s in streams["streams"] if s["codec_type"] == "video")
    assert (video_stream["width"], video_stream["height"]) == expected_size
    assert any(s["codec_type"] == "audio" for s in streams["streams"])


@requires_ffmpeg
def test_arabic_caption_export_renders_real_mp4(tmp_path):
    video, clips = make_clip_service(tmp_path)
    source = make_test_video(tmp_path / "sample.mp4", duration=2.0)
    job = video.prepare_local(source)
    add_transcript_and_highlight(
        video,
        job,
        text="مرحبا بالعالم، هذا اختبار.",
        language="ar",
        start=0.2,
        end=1.8,
        highlight_start=0.2,
        highlight_end=1.8,
    )
    clip = clips.create_for_job(job.id, captions=True, caption_language="ar")[0]
    finished = clips.process(clip.id)
    assert finished.status == ClipStatus.READY, finished.error
    assert finished.caption_path is not None and finished.caption_path.is_file()
    assert finished.output_path is not None and finished.output_path.is_file()
    assert finished.size_bytes and finished.size_bytes > 0
    caption_text = finished.caption_path.read_text(encoding="utf-8")
    assert "مرحبا" in caption_text
    assert "Noto Sans Arabic" in caption_text
    streams = ffprobe_streams(finished.output_path)
    assert any(s["codec_type"] == "video" for s in streams["streams"])
    assert any(s["codec_type"] == "audio" for s in streams["streams"])


@requires_ffmpeg
def test_render_clip_resolves_relative_output_path_before_ffmpeg(tmp_path, monkeypatch):
    source = make_test_video(tmp_path / "sample.mp4", duration=2.0)
    job = make_clip_job(
        clip_id="f" * 32,
        captions=False,
        start_time=0.2,
        end_time=1.8,
        width=180,
        height=320,
    )
    monkeypatch.chdir(tmp_path)
    relative_output = Path("data") / "clips" / "jobs" / job.id / "clip_vertical_standard.mp4"
    result = render_clip(
        job,
        source_path=source,
        output_path=relative_output,
        config=small_clip_config(),
    )
    expected = (tmp_path / relative_output).resolve()
    assert result.path == expected
    assert result.path.is_absolute()
    assert result.path.is_file()
    assert not expected.with_name("clip_vertical_standard.part.mp4").exists()


@requires_ffmpeg
def test_clip_export_survives_working_directory_change(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    settings = Settings(
        config_dir=Path("config"),
        data_dir=Path("data"),
        _env_file=None,
    )
    settings.ensure_dirs()
    assert settings.data_dir == (project / "data").resolve()
    video = VideoService(
        settings,
        config=VideoConfig(chunk_seconds=1, max_upload_bytes=10_000_000),
    )
    clips = ClipService(settings, video_service=video, config=small_clip_config())
    source = make_test_video(tmp_path / "sample.mp4", duration=2.0)
    job = video.prepare_local(source)
    add_transcript_and_highlight(video, job, highlight_start=0.2, highlight_end=1.8)
    clip = clips.create_for_job(job.id, captions=False)[0]
    assert not clips.clip_dir(clip.id).exists()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    finished = clips.process(clip.id)
    assert finished.status == ClipStatus.READY, finished.error
    assert finished.output_path is not None
    assert finished.output_path.is_absolute()
    assert finished.output_path.is_file()
    assert finished.output_path.is_relative_to((project / "data").resolve())


@requires_ffmpeg
def test_concurrent_exports_use_separate_job_directories(tmp_path):
    video, clips = make_clip_service(tmp_path)
    source = make_test_video(tmp_path / "sample.mp4", duration=2.0)
    job = video.prepare_local(source)
    add_transcript_and_highlight(video, job, highlight_start=0.2, highlight_end=1.8)
    created = clips.create_for_job(
        job.id,
        formats=[ClipFormat.VERTICAL, ClipFormat.SQUARE],
        captions=False,
    )
    other = ClipService(video.settings, video_service=video, config=clips.config)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(clips.process, created[0].id),
            pool.submit(other.process, created[1].id),
        ]
        finished = [future.result(timeout=120) for future in futures]
    paths = []
    for result in finished:
        assert result.status == ClipStatus.READY, result.error
        assert result.output_path is not None and result.output_path.is_file()
        paths.append(result.output_path)
    assert len({path.parent for path in paths}) == 2


@requires_ffmpeg
def test_failed_render_cleans_partial_and_retry_succeeds(tmp_path):
    failing_ffmpeg = tmp_path / "failing-ffmpeg"
    failing_ffmpeg.write_text(
        '#!/bin/sh\nfor last in "$@"; do :; done\nprintf "partial" > "$last"\nexit 1\n',
        encoding="utf-8",
    )
    failing_ffmpeg.chmod(0o755)
    video, clips = make_clip_service(
        tmp_path,
        config=small_clip_config(
            ffmpeg_binary=str(failing_ffmpeg),
            keep_failed_artifacts=True,
        ),
    )
    source = make_test_video(tmp_path / "sample.mp4", duration=2.0)
    job = video.prepare_local(source)
    add_transcript_and_highlight(video, job, highlight_start=0.2, highlight_end=1.8)
    clip = clips.create_for_job(job.id, captions=False)[0]
    clip_dir = clips.clip_dir(clip.id)
    clip_dir.mkdir(parents=True, exist_ok=True)
    unrelated = clip_dir / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")
    failed = clips.process(clip.id)
    assert failed.status == ClipStatus.FAILED
    assert unrelated.is_file()
    assert not (clip_dir / "clip_vertical_standard.part.mp4").exists()
    assert not (clip_dir / "clip_vertical_standard.mp4").exists()
    good = ClipService(video.settings, video_service=video, config=small_clip_config())
    assert good.retry(clip.id).status == ClipStatus.QUEUED
    finished = good.process(clip.id)
    assert finished.status == ClipStatus.READY, finished.error
    assert finished.output_path is not None and finished.output_path.is_file()
    assert unrelated.is_file()


@requires_ffmpeg
def test_missing_transcript_and_invalid_timestamps_are_rejected(tmp_path):
    video, clips = make_clip_service(tmp_path)
    source = make_test_video(tmp_path / "sample.mp4", duration=2.0)
    job = video.prepare_local(source)
    add_transcript_and_highlight(video, job, highlight_start=0.2, highlight_end=1.8)
    video.store.update(job.id, transcript=None)
    with pytest.raises(AIError) as excinfo:
        clips.create_for_job(job.id, captions=True)
    assert excinfo.value.category.value == "invalid_request"

    add_transcript_and_highlight(video, job, highlight_start=0.2, highlight_end=5.0)
    with pytest.raises(AIError) as excinfo:
        clips.create_for_job(job.id)
    assert excinfo.value.category.value == "invalid_request"
    assert "exceeds source duration" in excinfo.value.message


@requires_ffmpeg
def test_render_failure_cleans_partial_output_and_allows_retry(tmp_path):
    video, clips = make_clip_service(
        tmp_path,
        config=small_clip_config(ffmpeg_binary="false"),
    )
    source = make_test_video(tmp_path / "sample.mp4", duration=2.0)
    job = video.prepare_local(source)
    add_transcript_and_highlight(video, job, highlight_start=0.2, highlight_end=1.8)
    clip = clips.create_for_job(job.id, captions=False)[0]
    failed = clips.process(clip.id)
    assert failed.status == ClipStatus.FAILED
    assert failed.error is not None
    assert not list(clips.clip_dir(clip.id).glob("*.part.mp4"))
    assert not (clips.clip_dir(clip.id) / "clip_vertical_standard.mp4").exists()
    assert clips.retry(clip.id).status == ClipStatus.QUEUED
    assert clips.cancel(clip.id).status == ClipStatus.CANCELLED


def test_cancel_processing_job_and_cleanup(tmp_path):
    video, clips = make_clip_service(tmp_path)
    clip = clips.store.create(
        id="c" * 32,
        source_job_id="b" * 32,
        start_time=0.0,
        end_time=2.0,
        format=ClipFormat.VERTICAL,
        width=180,
        height=320,
        fps=30.0,
        quality=ClipQuality.STANDARD,
        fit=ClipFit.CROP,
        captions=False,
    )
    clips.store.mark_processing(clip.id)
    clips.store.request_cancel(clip.id)
    cancelled = clips.process(clip.id)
    assert cancelled.status == ClipStatus.CANCELLED
    assert not clips.clip_dir(clip.id).exists()


@requires_ffmpeg
def test_clip_api_create_render_status_download_and_config(tmp_path):
    video, clips = make_clip_service(tmp_path)
    source = make_test_video(tmp_path / "sample.mp4", duration=2.0)
    job = video.prepare_local(source)
    add_transcript_and_highlight(video, job, highlight_start=0.2, highlight_end=1.8)
    app = create_app(
        SimpleNamespace(settings=video.settings),  # type: ignore[arg-type]
        video_service=video,
        clip_service=clips,
        start_worker=False,
    )
    client = TestClient(app)

    config = client.get("/api/clips/config")
    assert config.status_code == 200
    assert config.json()["formats"]["vertical"]["height"] == 320

    response = client.post(
        f"/api/video/jobs/{job.id}/clips",
        json={"formats": ["vertical"], "captions": True, "render": True},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 1
    clip_id = body["jobs"][0]["id"]
    assert body["jobs"][0]["status"] == "ready"

    detail = client.get(f"/api/clip-jobs/{clip_id}")
    assert detail.status_code == 200
    assert detail.json()["job"]["format"] == "vertical"
    status = client.get(f"/api/clip-jobs/{clip_id}/status")
    assert status.status_code == 200
    assert status.json()["progress"] == 1.0
    listed = client.get("/api/clip-jobs", params={"source_job_id": job.id})
    assert [item["id"] for item in listed.json()["jobs"]] == [clip_id]

    download = client.get(f"/api/clip-jobs/{clip_id}/download")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("video/mp4")
    assert len(download.content) > 0


def test_clip_api_creates_custom_highlight_ranges(tmp_path):
    video, clips = make_clip_service(tmp_path)
    source = make_test_video(tmp_path / "sample.mp4", duration=4.0)
    job = video.prepare_local(source)
    add_highlights(video, job, [(0.2, 1.8), (2.0, 3.8)])
    app = create_app(
        SimpleNamespace(settings=video.settings),  # type: ignore[arg-type]
        video_service=video,
        clip_service=clips,
        start_worker=False,
    )
    client = TestClient(app)

    response = client.post(
        f"/api/video/jobs/{job.id}/clips",
        json={
            "formats": ["vertical"],
            "ranges": [
                {"highlight_index": 0, "start_time": 0.4, "end_time": 1.4},
                {"highlight_index": 1, "start_time": 2.2, "end_time": 3.2},
            ],
            "render": False,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 2
    jobs = body["jobs"]
    assert [job["highlight_index"] for job in jobs] == [0, 1]
    assert [job["start_time"] for job in jobs] == pytest.approx([0.4, 2.2])
    assert [job["end_time"] for job in jobs] == pytest.approx([1.4, 3.2])
    assert [job["duration"] for job in jobs] == pytest.approx([1.0, 1.0])
    assert all(job["status"] == "queued" for job in jobs)
    assert all(job["format"] == "vertical" for job in jobs)
    assert clips.store.require(jobs[0]["id"]).highlight_index == 0
    assert clips.store.require(jobs[1]["id"]).highlight_index == 1


@pytest.mark.parametrize(
    ("ranges", "extra", "message"),
    [
        (
            [{"highlight_index": 0, "start_time": 0.4, "end_time": 1.4}],
            {"highlight_indices": [0]},
            "cannot be combined with highlight_indices",
        ),
        (
            [{"highlight_index": 0, "start_time": 0.4, "end_time": 1.4}],
            {"limit": 1},
            "cannot be combined with limit",
        ),
        ([], {}, "At least one clip range is required"),
        (
            [{"highlight_index": 5, "start_time": 0.4, "end_time": 1.4}],
            {},
            "Highlight index out of range",
        ),
        (
            [
                {"highlight_index": 0, "start_time": 0.4, "end_time": 1.4},
                {"highlight_index": 0, "start_time": 0.4, "end_time": 1.4},
            ],
            {},
            "Duplicate highlight index",
        ),
        (
            [{"highlight_index": 0, "start_time": 0.4, "end_time": 0.8}],
            {},
            "shorter than the minimum clip duration",
        ),
        (
            [{"highlight_index": 0, "start_time": 0.4, "end_time": 5.0}],
            {},
            "exceeds source duration",
        ),
    ],
)
def test_clip_api_rejects_invalid_ranges(tmp_path, ranges, extra, message):
    video, clips = make_clip_service(tmp_path)
    source = make_test_video(tmp_path / "sample.mp4", duration=4.0)
    job = video.prepare_local(source)
    add_highlights(video, job, [(0.2, 1.8), (2.0, 3.8)])
    app = create_app(
        SimpleNamespace(settings=video.settings),  # type: ignore[arg-type]
        video_service=video,
        clip_service=clips,
        start_worker=False,
    )
    client = TestClient(app)

    response = client.post(
        f"/api/video/jobs/{job.id}/clips",
        json={"formats": ["vertical"], "ranges": ranges, "render": False, **extra},
    )
    assert response.status_code == 400, response.text
    assert message in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "range_item",
    [
        {"start_time": 0.4, "end_time": 1.4},
        {"highlight_index": 0, "start_time": 0.4},
        {"highlight_index": 0, "start_time": 1.4, "end_time": 0.4},
        {"highlight_index": -1, "start_time": 0.4, "end_time": 1.4},
        {"highlight_index": 0, "start_time": -0.4, "end_time": 1.4},
    ],
)
def test_clip_api_rejects_malformed_ranges(tmp_path, range_item):
    video, clips = make_clip_service(tmp_path)
    source = make_test_video(tmp_path / "sample.mp4", duration=2.0)
    job = video.prepare_local(source)
    add_highlights(video, job, [(0.2, 1.8)])
    app = create_app(
        SimpleNamespace(settings=video.settings),  # type: ignore[arg-type]
        video_service=video,
        clip_service=clips,
        start_worker=False,
    )
    client = TestClient(app)

    response = client.post(
        f"/api/video/jobs/{job.id}/clips",
        json={"formats": ["vertical"], "ranges": [range_item], "render": False},
    )
    assert response.status_code == 422, response.text


def test_clip_service_validates_ranges_directly(tmp_path):
    video, clips = make_clip_service(tmp_path)
    source = make_test_video(tmp_path / "sample.mp4", duration=4.0)
    job = video.prepare_local(source)
    add_highlights(video, job, [(0.2, 1.8), (2.0, 3.8)])

    too_many = [ClipRange(highlight_index=0, start_time=0.4, end_time=1.4)] * 101
    with pytest.raises(AIError) as excinfo:
        clips.create_for_job(job.id, ranges=too_many)
    assert excinfo.value.category.value == "invalid_request"
    assert "At most 100 clip ranges" in excinfo.value.message

    with pytest.raises(AIError) as excinfo:
        clips.create_for_job(job.id, ranges=[{"highlight_index": 0}])
    assert excinfo.value.category.value == "invalid_request"
    assert "Invalid clip range" in excinfo.value.message

    short_max = ClipService(
        video.settings,
        video_service=video,
        config=small_clip_config(max_clip_seconds=1.5, effects_zoom_duration=1.0),
    )
    with pytest.raises(AIError) as excinfo:
        short_max.create_for_job(
            job.id,
            ranges=[ClipRange(highlight_index=0, start_time=0.2, end_time=2.0)],
        )
    assert excinfo.value.category.value == "invalid_request"
    assert "longer than the maximum clip duration" in excinfo.value.message


@requires_ffmpeg
def test_custom_range_render_uses_edited_bounds(tmp_path):
    video, clips = make_clip_service(tmp_path)
    source = make_test_video(tmp_path / "sample.mp4", duration=3.0)
    job = video.prepare_local(source)
    add_transcript_and_highlight(video, job, highlight_start=0.2, highlight_end=2.8)
    clip = clips.create_for_job(
        job.id,
        formats=[ClipFormat.VERTICAL],
        ranges=[ClipRange(highlight_index=0, start_time=0.5, end_time=1.5)],
        captions=True,
    )[0]
    assert clip.start_time == pytest.approx(0.5)
    assert clip.end_time == pytest.approx(1.5)
    assert clip.duration == pytest.approx(1.0)

    finished = clips.process(clip.id)
    assert finished.status == ClipStatus.READY, finished.error
    assert finished.output_path is not None and finished.output_path.is_file()
    assert finished.caption_path is not None and finished.caption_path.is_file()
    info = probe_media(finished.output_path, clips.config)
    assert info.duration == pytest.approx(1.0, abs=0.35)
    caption_text = finished.caption_path.read_text(encoding="utf-8")
    assert "Dialogue: 0,0:00:00.00," in caption_text


def test_clip_api_filters_by_ids(tmp_path):
    video, clips = make_clip_service(tmp_path)
    first = clips.store.create(
        id="1" * 32,
        source_job_id="b" * 32,
        start_time=0.0,
        end_time=2.0,
        format=ClipFormat.VERTICAL,
        width=180,
        height=320,
        fps=30.0,
        quality=ClipQuality.STANDARD,
        fit=ClipFit.CROP,
        captions=False,
    )
    second = clips.store.create(
        id="2" * 32,
        source_job_id="b" * 32,
        start_time=2.0,
        end_time=4.0,
        format=ClipFormat.SQUARE,
        width=180,
        height=180,
        fps=30.0,
        quality=ClipQuality.STANDARD,
        fit=ClipFit.CROP,
        captions=False,
    )
    app = create_app(
        SimpleNamespace(settings=video.settings),  # type: ignore[arg-type]
        video_service=video,
        clip_service=clips,
        start_worker=False,
    )
    client = TestClient(app)

    listed = client.get("/api/clip-jobs", params={"ids": f"{first.id},{second.id}"})
    assert listed.status_code == 200
    assert {item["id"] for item in listed.json()["jobs"]} == {first.id, second.id}

    filtered = client.get("/api/clip-jobs", params={"ids": second.id})
    assert filtered.status_code == 200
    assert [item["id"] for item in filtered.json()["jobs"]] == [second.id]


def test_clip_store_status_transitions_and_recovery(tmp_path):
    settings = Settings(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    settings.ensure_dirs()
    video = make_service(tmp_path)
    clips = ClipService(settings, video_service=video, config=small_clip_config())
    clip = clips.store.create(
        id="d" * 32,
        source_job_id="b" * 32,
        start_time=0.0,
        end_time=2.0,
        format=ClipFormat.SQUARE,
        width=180,
        height=180,
        fps=30.0,
        quality=ClipQuality.DRAFT,
        fit=ClipFit.BLUR,
        captions=False,
    )
    assert clip.status == ClipStatus.QUEUED
    assert clips.store.claim_next_queued().id == clip.id
    assert clips.store.recover_interrupted() == 1
    assert clips.store.require(clip.id).status == ClipStatus.QUEUED
    assert clips.store.request_cancel(clip.id).status == ClipStatus.CANCELLED
    assert clips.store.retry(clip.id).status == ClipStatus.QUEUED

@requires_ffmpeg
def test_pad_and_blur_fits_render(tmp_path):
    video, clips = make_clip_service(tmp_path)
    source = make_test_video(tmp_path / "sample.mp4", duration=2.0)
    job = video.prepare_local(source)
    add_transcript_and_highlight(video, job, highlight_start=0.2, highlight_end=1.8)
    for fit in (ClipFit.PAD, ClipFit.BLUR):
        clip = clips.create_for_job(job.id, fit=fit, captions=False)[0]
        finished = clips.process(clip.id)
        assert finished.status == ClipStatus.READY, finished.error
        streams = ffprobe_streams(finished.output_path)
        video_stream = next(s for s in streams["streams"] if s["codec_type"] == "video")
        assert (video_stream["width"], video_stream["height"]) == (180, 320)


@requires_ffmpeg
def test_clip_worker_processes_queued_job(tmp_path):
    video, clips = make_clip_service(tmp_path)
    source = make_test_video(tmp_path / "sample.mp4", duration=2.0)
    job = video.prepare_local(source)
    add_transcript_and_highlight(video, job, highlight_start=0.2, highlight_end=1.8)
    clip = clips.create_for_job(job.id, captions=False)[0]
    worker = ClipWorker(clips, poll_seconds=0.01)
    finished = worker.run_once()
    assert finished is not None
    assert finished.id == clip.id
    assert finished.status == ClipStatus.READY, finished.error
    assert worker.run_once() is None

