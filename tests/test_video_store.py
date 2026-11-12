from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.video.models import (
    AudioChunkInfo,
    JobStage,
    JobStatus,
    SourceType,
    VideoMetadata,
)
from clipping_ai.video.store import VideoJobStore


@pytest.fixture
def store(tmp_path):
    return VideoJobStore(tmp_path / "jobs.db")


def make_job(store: VideoJobStore, *, source_hash: str = "abc") -> str:
    job = store.create(
        job_id=uuid4().hex,
        source_type=SourceType.UPLOAD,
        source_value="clip.mp4",
        original_filename="clip.mp4",
        source_hash=source_hash,
        input_path=Path("/tmp/opencode/clip.mp4"),
    )
    return job.id


def test_create_get_and_update(store):
    job_id = make_job(store)
    job = store.require(job_id)
    assert job.status == JobStatus.QUEUED
    assert job.stage == JobStage.QUEUED
    metadata = VideoMetadata(
        duration_seconds=2.0,
        size_bytes=10,
        format_name="mp4",
        has_video=True,
        has_audio=True,
    )
    chunks = [
        AudioChunkInfo(
            index=0,
            path=Path("/tmp/opencode/chunk_0000.flac"),
            offset=0.0,
            duration=1.0,
            size_bytes=5,
        )
    ]
    updated = store.update(
        job_id,
        metadata=metadata,
        audio_path=Path("/tmp/opencode/audio.flac"),
        audio_chunks=chunks,
        status=JobStatus.PROCESSING,
        stage=JobStage.AUDIO,
        progress=0.5,
        error={"category": "x", "message": "y"},
    )
    assert updated.metadata is not None
    assert updated.metadata.duration_seconds == 2.0
    assert updated.audio_chunks[0].path.name == "chunk_0000.flac"
    assert updated.progress == 0.5
    assert updated.error == {"category": "x", "message": "y"}


def test_claim_and_recover(store):
    job_id = make_job(store)
    claimed = store.claim_next_queued()
    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.status == JobStatus.PROCESSING
    assert store.claim_next_queued() is None
    assert store.recover_interrupted() == 1
    recovered = store.require(job_id)
    assert recovered.status == JobStatus.QUEUED
    assert recovered.stage == JobStage.QUEUED
    assert recovered.started_at is None


def test_fail_retry_and_cancel(store):
    failed_id = make_job(store)
    store.fail(failed_id, {"category": "invalid_request", "message": "bad"})
    failed = store.require(failed_id)
    assert failed.status == JobStatus.FAILED
    assert failed.completed_at is not None
    retried = store.retry(failed_id)
    assert retried.status == JobStatus.QUEUED
    assert retried.error is None

    queued_id = make_job(store)
    cancelled = store.cancel(queued_id)
    assert cancelled.status == JobStatus.CANCELLED
    with pytest.raises(AIError):
        store.cancel(queued_id)


def test_retry_rejects_ready_jobs(store):
    job_id = make_job(store)
    store.mark_ready(job_id)
    with pytest.raises(AIError):
        store.retry(job_id)


def test_find_by_hash_and_source(store):
    job_id = make_job(store, source_hash="same")
    by_hash = store.find_by_hash("same")
    assert by_hash is not None
    assert by_hash.id == job_id
    by_source = store.find_by_source(SourceType.UPLOAD, "clip.mp4")
    assert by_source is not None
    assert by_source.id == job_id
    assert store.find_by_hash("missing") is None


def test_list_and_count(store):
    first = make_job(store)
    second = make_job(store)
    store.mark_ready(second)
    jobs = store.list(limit=10)
    assert {job.id for job in jobs} == {first, second}
    ready = store.list(status=JobStatus.READY)
    assert [job.id for job in ready] == [second]
    assert store.count() == 2


def test_invalid_and_unknown_ids(store):
    with pytest.raises(AIError) as exc_info:
        store.get("../etc/passwd")
    assert exc_info.value.category == ErrorCategory.INVALID_REQUEST
    missing = uuid4().hex
    with pytest.raises(AIError) as missing_info:
        store.require(missing)
    assert missing_info.value.category == ErrorCategory.NOT_FOUND
    with pytest.raises(ValueError):
        store.update(make_job(store), unknown=1)
