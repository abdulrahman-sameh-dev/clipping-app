from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clipping_ai.analysis.config import HighlightSettings
from clipping_ai.analysis.service import AnalysisService
from clipping_ai.api.app import create_app
from clipping_ai.clips.config import ClipConfig, ClipFormatProfile
from clipping_ai.clips.models import (
    ClipFit,
    ClipFormat,
    ClipMetadataStatus,
    ClipQuality,
    ClipStage,
    ClipStatus,
)
from clipping_ai.clips.service import ClipService
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.highlights.models import ContentType, Highlight, HighlightResult
from clipping_ai.highlights.semantic import split_long_highlights
from clipping_ai.schemas import TranscriptResult, TranscriptSegment
from clipping_ai.trash import TrashService, TrashTarget
from clipping_ai.video.models import (
    AudioStreamInfo,
    JobStage,
    JobStatus,
    SourceType,
    VideoMetadata,
    VideoStreamInfo,
)
from clipping_ai.video.service import VideoService
from tests.video_helpers import make_service


def _config(**overrides) -> ClipConfig:
    values = {
        "formats": {
            "vertical": ClipFormatProfile(width=180, height=320),
            "square": ClipFormatProfile(width=180, height=180),
            "landscape": ClipFormatProfile(width=320, height=180),
        },
        "min_free_disk_bytes": 0,
        "metadata_auto_generate": False,
    }
    values.update(overrides)
    return ClipConfig(**values)


def _make_source(video: VideoService, *, job_id: str = "a" * 32):
    video.store.create(
        job_id=job_id,
        source_type=SourceType.UPLOAD,
        source_value="source.mp4",
        original_filename="Source",
    )
    directory = video.job_dir(job_id)
    directory.mkdir(parents=True)
    source_path = directory / "source.mp4"
    source_path.write_bytes(b"fake")
    transcript = TranscriptResult(
        text="This is a test clip about productivity.",
        language="en",
        duration=20.0,
        segments=[
            TranscriptSegment(id=0, start=0.0, end=2.0, text="This is a test clip"),
            TranscriptSegment(id=1, start=2.0, end=4.0, text="about productivity."),
        ],
    )
    highlight = Highlight(
        start_time=0.0,
        end_time=4.0,
        title="Productivity test",
        description="A short productivity test.",
        selection_reason="Test fixture",
        confidence=0.9,
        category="test",
        transcript_segments=transcript.segments,
    )
    return video.store.update(
        job_id,
        input_path=source_path,
        status=JobStatus.READY,
        stage=JobStage.READY,
        metadata=VideoMetadata(
            duration_seconds=20.0,
            size_bytes=4,
            has_video=True,
            has_audio=True,
            video_streams=[VideoStreamInfo(index=0, width=160, height=120)],
            audio_streams=[AudioStreamInfo(index=1)],
        ),
        transcript=transcript,
        highlight_result=HighlightResult(
            job_id=job_id,
            content_type=ContentType.PODCAST,
            language="en",
            duration=20.0,
            highlights=[highlight],
        ),
    )


def _make_clip(clips: ClipService, source_id: str, *, clip_id: str = "b" * 32):
    directory = clips.clip_dir(clip_id)
    directory.mkdir(parents=True)
    output = directory / "clip.mp4"
    output.write_bytes(b"rendered")
    return clips.store.create(
        id=clip_id,
        source_job_id=source_id,
        highlight_index=0,
        start_time=0.0,
        end_time=4.0,
        format=ClipFormat.VERTICAL,
        width=180,
        height=320,
        fps=30.0,
        quality=ClipQuality.DRAFT,
        fit=ClipFit.CROP,
        status=ClipStatus.READY,
        stage=ClipStage.READY,
        progress=1.0,
        output_path=output,
    )


class FakeMetadataManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def analyze(self, task: str, **kwargs):
        self.calls.append((task, kwargs))
        return {
            "provider": "fake",
            "model": "fake-model",
            "data": {
                "title": "A practical productivity tip",
                "description": "A concise description of the productivity tip.",
                "hashtags": ["#Productivity", "focus"],
                "language": "en",
            },
        }


def test_metadata_generation_manual_edit_regeneration_and_publish(tmp_path: Path):
    video = make_service(tmp_path)
    manager = FakeMetadataManager()
    clips = ClipService(
        video.settings,
        video_service=video,
        config=_config(),
        manager=manager,
    )
    source = _make_source(video)
    clip = _make_clip(clips, source.id)

    generated = clips.generate_metadata(clip.id)
    assert generated.metadata_status == ClipMetadataStatus.GENERATED
    assert generated.title == "A practical productivity tip"
    assert generated.description
    assert generated.hashtags == ["#Productivity", "#focus"]
    assert generated.metadata_provider == "fake"
    assert generated.metadata_model == "fake-model"
    assert manager.calls[0][0] == "title_generation"
    assert "productivity" in manager.calls[0][1]["text"].lower()
    assert "caption" not in manager.calls[0][1]["payload"]

    edited = clips.update_metadata(
        clip.id,
        title="My edited title",
        description="My edited description",
        hashtags=["#one"],
        language="en",
    )
    assert edited.metadata_status == ClipMetadataStatus.MANUALLY_EDITED
    assert edited.hashtags == ["#one"]

    with pytest.raises(AIError) as exc:
        clips.generate_metadata(clip.id)
    assert exc.value.category == ErrorCategory.INVALID_REQUEST
    assert "manually edited" in exc.value.message.lower()

    regenerated = clips.generate_metadata(clip.id, force=True)
    assert regenerated.metadata_status == ClipMetadataStatus.GENERATED
    assert regenerated.title == "A practical productivity tip"

    published = clips.set_published(clip.id, True)
    assert published.published is True
    assert published.published_at is not None
    unpublished = clips.set_published(clip.id, False)
    assert unpublished.published is False
    assert unpublished.published_at is None

    clips.soft_delete(clip.id)
    with pytest.raises(AIError):
        clips.generate_metadata(clip.id, force=True)


def test_source_and_clip_soft_delete_restore_and_permanent_delete(tmp_path: Path):
    video = make_service(tmp_path)
    clips = ClipService(video.settings, video_service=video, config=_config())
    source = _make_source(video)
    clip = _make_clip(clips, source.id)
    source_dir = video.job_dir(source.id)
    clip_dir = clips.clip_dir(clip.id)
    assert source_dir.exists()
    assert clip_dir.exists()

    with pytest.raises(AIError):
        video.permanently_delete(source.id)
    with pytest.raises(AIError):
        clips.permanently_delete(clip.id)

    deleted = video.soft_delete(source.id)
    assert deleted.deleted_at is not None
    assert video.store.list() == []
    assert [item.id for item in video.list_deleted()] == [source.id]
    with pytest.raises(AIError) as exc:
        clips.create_for_job(source.id)
    assert "trash" in exc.value.message.lower()
    analysis = AnalysisService(video.settings, store=video.store)
    with pytest.raises(AIError) as exc:
        analysis.get_transcript(source.id)
    assert "trash" in exc.value.message.lower()
    assert clips.store.require(clip.id).deleted_at is None

    restored = video.restore(source.id)
    assert restored.deleted_at is None
    assert [item.id for item in video.store.list()] == [source.id]

    deleted_clip = clips.soft_delete(clip.id)
    assert deleted_clip.deleted_at is not None
    assert clips.store.list() == []
    assert [item.id for item in clips.list_deleted()] == [clip.id]
    with pytest.raises(AIError):
        clips.output_path(clip.id)
    assert clips.restore(clip.id).deleted_at is None

    video.soft_delete(source.id)
    video.permanently_delete(source.id)
    assert not source_dir.exists()
    assert video.store.get(source.id) is None
    assert clips.store.require(clip.id).id == clip.id
    assert (clip_dir / "clip.mp4").read_bytes() == b"rendered"

    clips.soft_delete(clip.id)
    clips.permanently_delete(clip.id)
    assert not clip_dir.exists()
    assert clips.store.get(clip.id) is None


def test_trash_service_lists_restores_and_empties(tmp_path: Path):
    video = make_service(tmp_path)
    clips = ClipService(video.settings, video_service=video, config=_config())
    source = _make_source(video)
    clip = _make_clip(clips, source.id)
    trash = TrashService(video, clips)

    video.soft_delete(source.id)
    clips.soft_delete(clip.id)
    items = trash.list()
    assert {item["kind"] for item in items} == {"source", "clip"}
    assert {item["id"] for item in items} == {source.id, clip.id}

    restored = trash.restore([TrashTarget("source", source.id)])
    assert restored["count"] == 1
    assert restored["errors"] == []
    assert video.store.require(source.id).deleted_at is None

    errors = trash.restore([TrashTarget("unknown", clip.id)])
    assert errors["count"] == 0
    assert errors["errors"][0]["kind"] == "unknown"

    emptied = trash.empty()
    assert emptied["count"] == 1
    assert emptied["errors"] == []
    assert trash.list() == []
    assert clips.store.get(clip.id) is None


def test_clip_metadata_trash_and_delete_api(tmp_path: Path):
    video = make_service(tmp_path)
    clips = ClipService(video.settings, video_service=video, config=_config())
    source = _make_source(video)
    clip = _make_clip(clips, source.id)
    app = create_app(
        SimpleNamespace(settings=video.settings),  # type: ignore[arg-type]
        video_service=video,
        clip_service=clips,
        start_worker=False,
    )
    client = TestClient(app)

    response = client.patch(
        f"/api/clip-jobs/{clip.id}/metadata",
        json={
            "title": "Edited via API",
            "description": "Description",
            "hashtags": ["#api"],
            "language": "en",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()["job"]
    assert body["title"] == "Edited via API"
    assert body["metadata_status"] == "manually_edited"
    assert "output_path" not in body
    assert "caption_path" not in body

    response = client.post(
        f"/api/clip-jobs/{clip.id}/publish",
        json={"published": True},
    )
    assert response.status_code == 200
    assert response.json()["job"]["published"] is True

    response = client.delete(f"/api/clip-jobs/{clip.id}")
    assert response.status_code == 200
    assert response.json()["job"]["deleted_at"] is not None

    response = client.get("/api/trash")
    assert response.status_code == 200
    assert any(item["kind"] == "clip" for item in response.json()["items"])

    response = client.post(
        "/api/trash/restore",
        json={"items": [{"kind": "clip", "id": clip.id}]},
    )
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert clips.store.require(clip.id).deleted_at is None

    response = client.delete(f"/api/clip-jobs/{clip.id}")
    assert response.status_code == 200
    response = client.delete("/api/trash")
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert clips.store.get(clip.id) is None


def test_split_long_highlights_uses_transcript_boundaries_and_keeps_coverage():
    transcript = TranscriptResult(
        text="one two three four five six seven eight",
        language="en",
        duration=90.0,
        segments=[
            TranscriptSegment(id=0, start=0.0, end=20.0, text="one two"),
            TranscriptSegment(id=1, start=20.0, end=40.0, text="three four"),
            TranscriptSegment(id=2, start=40.0, end=60.0, text="five six"),
            TranscriptSegment(id=3, start=60.0, end=80.0, text="seven eight"),
        ],
    )
    highlight = Highlight(
        start_time=0.0,
        end_time=80.0,
        title="Long moment",
        description="A long coherent moment.",
        selection_reason="Test",
        confidence=0.9,
        category="test",
        transcript_segments=transcript.segments,
    )
    settings = HighlightSettings(
        target_clip_seconds_min=30,
        target_clip_seconds_max=45,
        target_duration_tolerance_seconds=15,
    )
    parts = split_long_highlights([highlight], transcript, settings)
    assert len(parts) == 2
    assert parts[0].start_time == 0.0
    assert parts[-1].end_time == 80.0
    assert all(part.end_time - part.start_time <= 60 for part in parts)
    assert all(part.detection_signals["duration_split"] == 1.0 for part in parts)
    assert parts[1].title.endswith("part 2")
    assert parts[0].transcript_segments + parts[1].transcript_segments == transcript.segments

    unchanged = split_long_highlights(
        [highlight.model_copy(update={"end_time": 40.0})],
        transcript,
        settings,
    )
    assert len(unchanged) == 1
