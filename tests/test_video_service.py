from __future__ import annotations

from io import BytesIO

import pytest

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.video.config import VideoConfig
from clipping_ai.video.models import JobStatus, SourceType
from clipping_ai.video.worker import VideoWorker
from tests.video_helpers import FakeDownloader, make_service, make_test_video, requires_ffmpeg


@requires_ffmpeg
def test_prepare_local_creates_ready_job(tmp_path):
    source = make_test_video(tmp_path / "sample.mp4")
    service = make_service(tmp_path)
    job = service.prepare_local(source)
    assert job.status == JobStatus.READY
    assert job.stage.value == "ready"
    assert job.progress == 1.0
    assert job.metadata is not None
    assert job.input_path is not None and job.input_path.exists()
    assert job.audio_path is not None and job.audio_path.exists()
    assert len(job.audio_chunks) == 2
    assert all(chunk.path.exists() for chunk in job.audio_chunks)
    assert job.input_path.is_relative_to(service.data_dir)
    assert job.audio_path.is_relative_to(service.data_dir)


@requires_ffmpeg
def test_duplicate_upload_returns_existing_job(tmp_path):
    source = make_test_video(tmp_path / "sample.mp4")
    service = make_service(tmp_path)
    first = service.prepare_local(source)
    second = service.prepare_local(source)
    assert first.id == second.id
    assert service.store.count() == 1
    assert len(list(service.jobs_root.iterdir())) == 1


@requires_ffmpeg
def test_upload_validation_and_cleanup(tmp_path):
    service = make_service(tmp_path, config=VideoConfig(max_upload_bytes=10))
    with pytest.raises(AIError):
        service.create_upload("clip.txt", BytesIO(b"data"))
    with pytest.raises(AIError):
        service.create_upload("clip.mp4", BytesIO(b""))
    with pytest.raises(AIError):
        service.create_upload("clip.mp4", BytesIO(b"x" * 20))
    assert list(service.jobs_root.iterdir()) == []
    assert service.store.count() == 0


@requires_ffmpeg
def test_url_job_is_downloaded_and_prepared(tmp_path):
    source = make_test_video(tmp_path / "sample.mp4")
    downloader = FakeDownloader(source)
    service = make_service(tmp_path, downloader=downloader)
    job, duplicate = service.create_url("https://example.com/video")
    assert duplicate is False
    assert job.status == JobStatus.QUEUED
    worker = VideoWorker(service)
    finished = worker.run_once()
    assert finished is not None
    assert finished.id == job.id
    assert finished.status == JobStatus.READY
    assert finished.source_hash is not None
    assert finished.original_filename == "fake title"
    assert finished.input_path is not None and finished.input_path.exists()
    assert downloader.calls[0][0] == "https://example.com/video"


def test_url_validation_and_duplicate_detection(tmp_path):
    service = make_service(tmp_path)
    with pytest.raises(AIError):
        service.create_url("file:///etc/passwd")
    with pytest.raises(AIError):
        service.create_url("not a url")
    first, _ = service.create_url("https://example.com/video")
    second, duplicate = service.create_url("https://example.com/video")
    assert duplicate is True
    assert first.id == second.id
    assert service.store.count() == 1


@requires_ffmpeg
def test_failed_job_can_be_retried(tmp_path):
    source = make_test_video(tmp_path / "video-only.mp4", with_audio=False)
    service = make_service(tmp_path)
    with pytest.raises(AIError):
        service.prepare_local(source)
    failed = service.list(limit=10)[0]
    assert failed.status == JobStatus.FAILED
    retried = service.retry(failed.id)
    assert retried.status == JobStatus.QUEUED
    assert retried.error is None


def test_cancel_queued_job_and_recover_processing(tmp_path):
    service = make_service(tmp_path)
    job, _ = service.create_upload("clip.mp4", BytesIO(b"data"))
    cancelled = service.cancel(job.id)
    assert cancelled.status == JobStatus.CANCELLED
    with pytest.raises(AIError):
        service.cancel(job.id)

    queued = service.store.create(
        job_id="a" * 32,
        source_type=SourceType.URL,
        source_value="https://example.com/other",
    )
    claimed = service.store.claim_next_queued()
    assert claimed is not None and claimed.id == queued.id
    assert service.recover() == 1
    assert service.get(queued.id).status == JobStatus.QUEUED


def test_disabled_and_path_traversal(tmp_path):
    service = make_service(tmp_path, config=VideoConfig(enabled=False))
    with pytest.raises(AIError) as exc_info:
        service.create_upload("clip.mp4", BytesIO(b"data"))
    assert exc_info.value.category == ErrorCategory.INVALID_REQUEST
    with pytest.raises(AIError):
        service.get("../etc/passwd")
    with pytest.raises(AIError):
        service.inspect(tmp_path / "missing.mp4")


def test_worker_start_and_stop(tmp_path):
    service = make_service(tmp_path)
    worker = VideoWorker(service)
    assert worker.run_once() is None
    worker.start()
    assert worker.running is True
    worker.stop()
    assert worker.running is False


@requires_ffmpeg
def test_inspect_returns_metadata_without_creating_job(tmp_path):
    source = make_test_video(tmp_path / "sample.mp4")
    service = make_service(tmp_path)
    metadata = service.inspect(source)
    assert metadata.duration_seconds > 0
    assert service.store.count() == 0
