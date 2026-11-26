from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from clipping_ai.api.app import create_app
from clipping_ai.clips.models import ClipFormat, ClipMetadataStatus, ClipStatus
from clipping_ai.clips.service import ClipService
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.schemas import GeneratedMetadataBatch
from tests.test_metadata_trash import _config, _make_clip, _make_source
from tests.video_helpers import make_service


class FakeBatchManager:
    def __init__(self, responder) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responder = responder

    def analyze(self, task: str, **kwargs):
        self.calls.append({"task": task, **kwargs})
        return self._responder(kwargs)


def _items(clips: list[dict[str, Any]], *, provider: str = "fake") -> dict[str, Any]:
    return {
        "provider": provider,
        "model": "batch-model",
        "data": {
            "items": [
                {
                    "clip_id": clip["clip_id"],
                    "title": f"AI title {clip['clip_id'][-4:]}",
                    "description": f"Description for {clip['clip_id'][-4:]}",
                    "hashtags": ["#focus", "productivity"],
                    "language": "en",
                }
                for clip in clips
            ]
        },
    }


def _build(tmp_path: Path, manager) -> tuple[Any, ClipService]:
    video = make_service(tmp_path)
    clips = ClipService(
        video.settings,
        video_service=video,
        config=_config(metadata_batch_size=6),
        manager=manager,
    )
    return video, clips


def _new_clip(clips: ClipService, source_id: str):
    return _make_clip(clips, source_id, clip_id=uuid4().hex)


def test_batch_metadata_maps_each_clip_and_is_idempotent(tmp_path: Path):
    manager = FakeBatchManager(lambda kwargs: _items(kwargs["payload"]["clips"]))
    video, clips = _build(tmp_path, manager)
    source = _make_source(video)
    created = [_new_clip(clips, source.id) for _ in range(3)]
    clips.store.update(created[0].id, status=ClipStatus.QUEUED)

    results = clips.generate_metadata_batch(source.id)

    assert len(manager.calls) == 1
    assert manager.calls[0]["schema"] is GeneratedMetadataBatch
    assert len(manager.calls[0]["payload"]["clips"]) == 3
    by_id = {clip.id: clip for clip in results}
    for expected in created:
        clip = by_id[expected.id]
        assert clip.metadata_status == ClipMetadataStatus.GENERATED
        assert clip.title == f"AI title {expected.id[-4:]}"
        assert clip.metadata_provider == "fake"
        assert clip.metadata_model == "batch-model"

    assert clips.generate_metadata_batch(source.id) == []
    assert len(manager.calls) == 1


def test_batch_metadata_omitted_item_uses_local_fallback(tmp_path: Path):
    def responder(kwargs):
        source_clips = kwargs["payload"]["clips"]
        omitted = source_clips[-1]["clip_id"]
        payload = _items(source_clips)
        payload["data"]["items"] = [
            item for item in payload["data"]["items"] if item["clip_id"] != omitted
        ]
        return payload

    manager = FakeBatchManager(responder)
    video, clips = _build(tmp_path, manager)
    source = _make_source(video)
    first = _new_clip(clips, source.id)
    second = _new_clip(clips, source.id)

    results = {clip.id: clip for clip in clips.generate_metadata_batch(source.id)}

    by_status = {
        clip_id: clip.metadata_status for clip_id, clip in results.items()
    }
    assert sorted(status.value for status in by_status.values()) == [
        "generated",
        "local_fallback",
    ]
    fallback = next(
        clip
        for clip in results.values()
        if clip.metadata_status == ClipMetadataStatus.LOCAL_FALLBACK
    )
    assert first.id in results
    assert second.id in results
    assert fallback.metadata_status == ClipMetadataStatus.LOCAL_FALLBACK
    assert fallback.title == "Productivity test"
    assert fallback.metadata_provider == "local"
    assert 2 <= len(fallback.hashtags) <= 4
    assert fallback.metadata_error
    assert "omitted" in (fallback.metadata_error or "")


def test_batch_metadata_splits_failures_then_succeeds(tmp_path: Path):
    def responder(kwargs):
        clips = kwargs["payload"]["clips"]
        if len(clips) > 1:
            raise AIError(
                ErrorCategory.RATE_LIMIT_EXCEEDED,
                "temporary rate limit",
                retryable=True,
            )
        return _items(clips)

    manager = FakeBatchManager(responder)
    video, clips = _build(tmp_path, manager)
    source = _make_source(video)
    created = [_new_clip(clips, source.id) for _ in range(3)]

    results = clips.generate_metadata_batch(source.id)

    assert len(manager.calls) > 1
    assert {clip.metadata_status for clip in results} == {ClipMetadataStatus.GENERATED}
    assert {clip.title for clip in results} == {
        f"AI title {clip.id[-4:]}" for clip in created
    }


def test_batch_quota_failure_uses_local_metadata_without_raising(tmp_path: Path):
    def responder(_kwargs):
        raise AIError(
            ErrorCategory.FREE_QUOTA_EXHAUSTED,
            "generate content requests per day limit exceeded",
            retryable=False,
        )

    manager = FakeBatchManager(responder)
    video, clips = _build(tmp_path, manager)
    source = _make_source(video)
    created = [_new_clip(clips, source.id) for _ in range(2)]

    results = clips.generate_metadata_batch(source.id)

    assert len(results) == 2
    for clip in results:
        assert clip.metadata_status == ClipMetadataStatus.LOCAL_FALLBACK
        assert clip.title
        assert clip.description
        assert 2 <= len(clip.hashtags) <= 4
        assert "quota" in (clip.metadata_error or "").lower()

    assert clips.generate_metadata_batch(source.id) == []
    for clip in created:
        current = clips.store.require(clip.id)
        assert current.metadata_status == ClipMetadataStatus.LOCAL_FALLBACK


def test_batch_metadata_preserves_manual_edits_unless_forced(tmp_path: Path):
    manager = FakeBatchManager(lambda kwargs: _items(kwargs["payload"]["clips"]))
    video, clips = _build(tmp_path, manager)
    source = _make_source(video)
    clip = _new_clip(clips, source.id)
    clips.generate_metadata_batch(source.id)
    clips.update_metadata(
        clip.id,
        title="Manual title",
        description="Manual description",
        hashtags=["#manual"],
        language="en",
    )

    assert clips.generate_metadata_batch(source.id) == []
    manual = clips.store.require(clip.id)
    assert manual.metadata_status == ClipMetadataStatus.MANUALLY_EDITED
    assert manual.title == "Manual title"

    forced = clips.generate_metadata_batch(source.id, force=True)
    assert forced[0].metadata_status == ClipMetadataStatus.GENERATED
    assert forced[0].title.startswith("AI title")


def test_batch_without_transcript_marks_failed(tmp_path: Path):
    manager = FakeBatchManager(lambda kwargs: _items(kwargs["payload"]["clips"]))
    video, clips = _build(tmp_path, manager)
    source = _make_source(video)
    clip = _new_clip(clips, source.id)
    clips.store.update(
        clip.id,
        highlight_index=None,
        start_time=100.0,
        end_time=104.0,
    )

    results = clips.generate_metadata_batch(source.id)

    assert results[0].metadata_status == ClipMetadataStatus.FAILED
    assert manager.calls == []


def test_source_publishing_completion_lifecycle_and_counts(tmp_path: Path):
    video, clips = _build(tmp_path, None)
    source = _make_source(video)
    first = _new_clip(clips, source.id)
    second = _new_clip(clips, source.id)
    clips.set_published(first.id, True)

    app = create_app(
        video_service=video,
        clip_service=clips,
        start_worker=False,
    )
    with TestClient(app) as client:
        response = client.post(
            f"/api/video/jobs/{source.id}/publish",
            json={"published": True},
        )
        assert response.status_code == 200
        payload = response.json()["job"]
        assert payload["all_clips_published"] is True
        assert payload["all_clips_published_at"]
        assert payload["clip_counts"] == {"total": 2, "published": 1}

        fetched = client.get(f"/api/video/jobs/{source.id}").json()["job"]
        assert fetched["all_clips_published"] is True
        assert fetched["clip_counts"] == {"total": 2, "published": 1}

        clips.set_published(second.id, True)
        listed = client.get("/api/video/jobs").json()["jobs"]
        source_payload = next(job for job in listed if job["id"] == source.id)
        assert source_payload["clip_counts"] == {"total": 2, "published": 2}

        cleared = client.post(
            f"/api/video/jobs/{source.id}/publish",
            json={"published": False},
        ).json()["job"]
        assert cleared["all_clips_published"] is False
        assert cleared["all_clips_published_at"] is None

        client.post(
            f"/api/video/jobs/{source.id}/publish",
            json={"published": True},
        )
        clips.create_for_job(source.id, formats=[ClipFormat.VERTICAL], limit=1)
        reset = client.get(f"/api/video/jobs/{source.id}").json()["job"]
        assert reset["all_clips_published"] is False
        assert reset["all_clips_published_at"] is None

        client.post(
            f"/api/video/jobs/{source.id}/publish",
            json={"published": True},
        )
        deleted = client.delete(f"/api/video/jobs/{source.id}").json()["job"]
        assert deleted["all_clips_published"] is True
        restored = client.post(f"/api/video/jobs/{source.id}/restore").json()["job"]
        assert restored["all_clips_published"] is True
