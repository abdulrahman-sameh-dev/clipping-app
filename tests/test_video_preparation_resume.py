from __future__ import annotations

import pytest

import clipping_ai.video.service as video_service
from clipping_ai.audio import hash_file
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.recovery.stages import StageStatus
from clipping_ai.video.config import VideoConfig
from clipping_ai.video.models import JobStatus
from clipping_ai.video.preparation import metadata_checkpoint
from tests.video_helpers import FakeDownloader, make_service, make_test_video, requires_ffmpeg


def _requeue(service, job_id: str) -> None:
    service.store.update(job_id, status=JobStatus.QUEUED)


@requires_ffmpeg
def test_preparation_records_durable_stage_checkpoints(tmp_path):
    source = make_test_video(tmp_path / "sample.mp4")
    service = make_service(tmp_path)
    job = service.prepare_local(source)

    records = {
        record.stage: record
        for record in service.stage_store.list_stages(job_type="video", job_id=job.id)
    }
    assert set(records) == {"source", "probe", "audio", "chunks"}
    assert all(record.status is StageStatus.DONE for record in records.values())
    assert all(record.input_fingerprint for record in records.values())

    audio_record = records["audio"]
    chunks_record = records["chunks"]
    assert records["source"].checkpoint["source_sha256"] == job.source_hash
    assert records["probe"].checkpoint["source_sha256"] == job.source_hash
    assert job.audio_path is not None
    assert audio_record.checkpoint["sha256"] == hash_file(job.audio_path)
    assert audio_record.artifact_path == job.audio_path
    assert chunks_record.checkpoint["audio_sha256"] == audio_record.checkpoint["sha256"]
    assert chunks_record.checkpoint["duration"] == job.metadata.duration_seconds
    assert len(chunks_record.checkpoint["chunks"]) == len(job.audio_chunks)


@requires_ffmpeg
def test_completed_stages_are_reused_without_reprobing_or_reencoding(tmp_path, monkeypatch):
    source = make_test_video(tmp_path / "sample.mp4")
    service = make_service(tmp_path)
    job = service.prepare_local(source)
    audio_path = job.audio_path
    chunk_paths = [chunk.path for chunk in job.audio_chunks]

    def fail(*_args, **_kwargs):
        raise AssertionError("a completed stage was unexpectedly rerun")

    monkeypatch.setattr(video_service, "probe_video", fail)
    monkeypatch.setattr(video_service, "extract_audio", fail)
    monkeypatch.setattr(video_service, "build_chunk_manifest", fail)
    _requeue(service, job.id)

    resumed = service.process(job.id)
    assert resumed.status == JobStatus.READY
    assert resumed.audio_path == audio_path
    assert [chunk.path for chunk in resumed.audio_chunks] == chunk_paths
    assert all(path.is_file() for path in chunk_paths)


@requires_ffmpeg
def test_url_source_is_redownloaded_when_the_local_copy_is_missing(tmp_path):
    source = make_test_video(tmp_path / "sample.mp4")
    downloader = FakeDownloader(source)
    service = make_service(tmp_path, downloader=downloader)
    job, duplicate = service.create_url("https://example.com/video")
    assert duplicate is False

    prepared = service.process(job.id)
    assert prepared.status == JobStatus.READY
    assert prepared.input_path is not None
    prepared.input_path.unlink()
    _requeue(service, job.id)

    resumed = service.process(job.id)
    assert resumed.status == JobStatus.READY
    assert len(downloader.calls) == 2
    assert resumed.input_path is not None and resumed.input_path.is_file()


@requires_ffmpeg
def test_incomplete_probe_checkpoint_is_not_reused(tmp_path):
    source = make_test_video(tmp_path / "sample.mp4")
    service = make_service(tmp_path)
    job = service.prepare_local(source)
    assert job.metadata is not None and job.source_hash is not None

    record = service.stage_store.get(job_type="video", job_id=job.id, stage="probe")
    assert record is not None
    service.stage_store.reset(
        job_type="video",
        job_id=job.id,
        stage="probe",
        input_fingerprint=record.input_fingerprint or "",
    )
    claimed = service.stage_store.claim_next(
        owner="checkpoint-test", job_type="video", job_id=job.id, stage="probe"
    )
    assert claimed is not None
    invalid = metadata_checkpoint(
        job.metadata.model_copy(update={"has_audio": False}),
        source_sha256=job.source_hash,
    )
    service.stage_store.save_checkpoint(
        job_type="video",
        job_id=job.id,
        stage="probe",
        owner="checkpoint-test",
        checkpoint=invalid,
    )
    service.stage_store.complete(
        job_type="video",
        job_id=job.id,
        stage="probe",
        owner="checkpoint-test",
        checkpoint=invalid,
    )
    before = service.stage_store.get(job_type="video", job_id=job.id, stage="probe")
    assert before is not None

    _requeue(service, job.id)
    resumed = service.process(job.id)

    assert resumed.status == JobStatus.READY
    assert resumed.metadata is not None and resumed.metadata.has_audio is True
    after = service.stage_store.get(job_type="video", job_id=job.id, stage="probe")
    assert after is not None and after.status is StageStatus.DONE
    assert after.attempt > before.attempt
    assert after.checkpoint["metadata"]["has_audio"] is True


@requires_ffmpeg
def test_changed_url_source_invalidates_downstream_stages(tmp_path):
    first_source = make_test_video(tmp_path / "first.mp4", duration=2.0)
    second_source = make_test_video(tmp_path / "second.mp4", duration=3.0)
    downloader = FakeDownloader(first_source)
    service = make_service(tmp_path, downloader=downloader)
    job, _duplicate = service.create_url("https://example.com/video")
    prepared = service.process(job.id)
    assert prepared.status == JobStatus.READY
    assert prepared.source_hash is not None
    assert prepared.input_path is not None
    assert prepared.metadata is not None
    old_hash = prepared.source_hash
    old_duration = prepared.metadata.duration_seconds

    prepared.input_path.unlink()
    downloader.source = second_source
    _requeue(service, job.id)
    resumed = service.process(job.id)

    assert resumed.status == JobStatus.READY
    assert resumed.source_hash is not None and resumed.source_hash != old_hash
    assert resumed.metadata is not None
    assert resumed.metadata.duration_seconds > old_duration
    assert len(resumed.audio_chunks) == 3
    probe = service.stage_store.get(job_type="video", job_id=job.id, stage="probe")
    chunks = service.stage_store.get(job_type="video", job_id=job.id, stage="chunks")
    assert probe is not None and probe.checkpoint["source_sha256"] == resumed.source_hash
    assert chunks is not None and chunks.checkpoint["duration"] == resumed.metadata.duration_seconds


@requires_ffmpeg
def test_missing_upload_source_fails_without_reconstruction(tmp_path):
    source = make_test_video(tmp_path / "sample.mp4")
    service = make_service(tmp_path)
    job = service.prepare_local(source)
    assert job.input_path is not None
    job.input_path.unlink()
    _requeue(service, job.id)

    with pytest.raises(AIError) as exc_info:
        service.process(job.id)

    assert exc_info.value.category == ErrorCategory.NOT_FOUND
    assert exc_info.value.retryable is False
    assert service.get(job.id).status == JobStatus.FAILED
    record = service.stage_store.get(job_type="video", job_id=job.id, stage="source")
    assert record is not None and record.status is StageStatus.FAILED
    assert record.error is not None
    assert record.error["details"]["reason"] == "unrecoverable_source"


@requires_ffmpeg
def test_corrupt_audio_is_replaced_from_the_valid_source(tmp_path):
    source = make_test_video(tmp_path / "sample.mp4")
    service = make_service(tmp_path)
    job = service.prepare_local(source)
    assert job.audio_path is not None
    job.audio_path.write_bytes(b"corrupt")
    _requeue(service, job.id)

    resumed = service.process(job.id)
    assert resumed.status == JobStatus.READY
    assert resumed.audio_path is not None
    assert resumed.audio_path.stat().st_size > len(b"corrupt")
    assert all(chunk.path.is_file() for chunk in resumed.audio_chunks)


@requires_ffmpeg
def test_corrupt_chunk_is_regenerated_without_rerunning_audio(tmp_path, monkeypatch):
    source = make_test_video(tmp_path / "sample.mp4")
    service = make_service(tmp_path)
    job = service.prepare_local(source)
    corrupt = job.audio_chunks[0].path

    def fail(*_args, **_kwargs):
        raise AssertionError("audio extraction was unexpectedly rerun")

    monkeypatch.setattr(video_service, "extract_audio", fail)
    corrupt.write_bytes(b"corrupt")
    _requeue(service, job.id)

    resumed = service.process(job.id)
    assert resumed.status == JobStatus.READY
    assert corrupt.read_bytes() != b"corrupt"
    assert corrupt.stat().st_size > len(b"corrupt")
    assert [chunk.offset for chunk in resumed.audio_chunks] == [
        chunk.offset for chunk in job.audio_chunks
    ]


@requires_ffmpeg
def test_chunk_config_change_invalidates_chunks_only(tmp_path):
    source = make_test_video(tmp_path / "sample.mp4")
    service = make_service(tmp_path)
    job = service.prepare_local(source)
    original_audio = job.audio_path
    assert original_audio is not None
    before = service.stage_store.get(job_type="video", job_id=job.id, stage="audio")
    assert before is not None

    changed = make_service(
        tmp_path,
        config=VideoConfig(chunk_seconds=0.5, max_upload_bytes=10_000_000),
    )
    _requeue(changed, job.id)
    resumed = changed.process(job.id)

    assert resumed.status == JobStatus.READY
    assert resumed.audio_path == original_audio
    assert len(resumed.audio_chunks) == 4
    audio = changed.stage_store.get(job_type="video", job_id=job.id, stage="audio")
    chunks = changed.stage_store.get(job_type="video", job_id=job.id, stage="chunks")
    assert audio is not None and audio.status is StageStatus.DONE
    assert audio.attempt == before.attempt
    assert chunks is not None and chunks.status is StageStatus.DONE
    assert len(chunks.checkpoint["chunks"]) == 4


@requires_ffmpeg
def test_recover_releases_running_video_stages(tmp_path):
    source = make_test_video(tmp_path / "sample.mp4")
    service = make_service(tmp_path)
    job = service.prepare_local(source)
    record = service.stage_store.get(job_type="video", job_id=job.id, stage="chunks")
    assert record is not None
    service.stage_store.reset(
        job_type="video",
        job_id=job.id,
        stage="chunks",
        input_fingerprint=record.input_fingerprint or "",
    )
    claimed = service.stage_store.claim_next(
        owner="dead-worker", job_type="video", job_id=job.id, stage="chunks"
    )
    assert claimed is not None and claimed.status is StageStatus.RUNNING

    service.recover()

    released = service.stage_store.get(job_type="video", job_id=job.id, stage="chunks")
    assert released is not None and released.status is StageStatus.PENDING
