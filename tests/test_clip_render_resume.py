from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipping_ai.api.app import create_app
from clipping_ai.clips.models import ClipStatus
from clipping_ai.clips.recovery import (
    CLIP_JOB_TYPE,
    STAGE_CAPTIONS,
    STAGE_FINALIZE,
    STAGE_OUTPUT,
    STAGE_PREPARE,
    STAGE_VALIDATE,
    output_staging_path,
)
from clipping_ai.clips.renderer import MediaInfo, RenderResult
from clipping_ai.clips.service import ClipService
from clipping_ai.clips.worker import ClipWorker
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.highlights.models import ContentType, Highlight, HighlightResult
from clipping_ai.recovery.stages import StageStatus, StageStore
from clipping_ai.schemas import TranscriptResult, TranscriptSegment
from clipping_ai.video.models import (
    AudioStreamInfo,
    JobStage,
    JobStatus,
    SourceType,
    VideoMetadata,
    VideoStreamInfo,
)
from tests.test_clips import small_clip_config
from tests.video_helpers import make_service


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_source(tmp_path: Path, video, *, source_hash: str = "source-v1", language: str = "en"):
    job_id = "b" * 32
    source_path = video.settings.data_dir / "videos" / "jobs" / job_id / "input.mp4"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"source-media-v1")
    job = video.store.create(
        job_id=job_id,
        source_type=SourceType.UPLOAD,
        source_value="sample.mp4",
        source_hash=source_hash,
        input_path=source_path,
    )
    text = "Hello world this is a test clip."
    transcript = TranscriptResult(
        text=text,
        language=language,
        duration=3.0,
        segments=[TranscriptSegment(id=0, start=0.1, end=2.9, text=text)],
    )
    highlight = Highlight(
        start_time=0.2,
        end_time=2.8,
        title="Test highlight",
        description="Test description",
        selection_reason="Test reason",
        confidence=0.9,
        category="test",
        transcript_segments=transcript.segments,
    )
    metadata = VideoMetadata(
        duration_seconds=3.0,
        size_bytes=source_path.stat().st_size,
        has_video=True,
        has_audio=True,
        video_streams=[
            VideoStreamInfo(index=0, codec_name="mpeg4", width=180, height=320, duration=3.0)
        ],
        audio_streams=[AudioStreamInfo(index=1, codec_name="aac", duration=3.0)],
    )
    video.store.update(
        job.id,
        status=JobStatus.READY,
        stage=JobStage.READY,
        metadata=metadata,
        transcript=transcript,
        highlight_result=HighlightResult(
            job_id=job.id,
            content_type=ContentType.PODCAST,
            language=language,
            duration=3.0,
            highlights=[highlight],
        ),
    )
    return video.store.require(job.id), source_path


def _build(tmp_path: Path, *, config=None, source_hash: str = "source-v1"):
    video = make_service(tmp_path)
    source, source_path = _make_source(tmp_path, video, source_hash=source_hash)
    stage_store = StageStore(video.settings.jobs_db_path)
    clips = ClipService(
        video.settings,
        video_service=video,
        config=config or small_clip_config(),
        stage_store=stage_store,
    )
    clip = clips.create_for_job(source.id, captions=True)[0]
    return video, clips, clip, source_path, stage_store


def _patch_render(monkeypatch, *, mode: str = "success"):
    calls: list[Path] = []

    def fake_render(job, *, source_path, output_path, config, cancel_check=None, **kwargs):
        calls.append(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = f"video-{len(calls)}".encode()
        output_path.write_bytes(payload)
        if cancel_check is not None and cancel_check():
            raise AIError(ErrorCategory.INVALID_REQUEST, "cancelled", retryable=False)
        if mode == "ffmpeg_failure":
            raise AIError(ErrorCategory.INTERNAL_ERROR, "ffmpeg failed", retryable=True)
        return RenderResult(
            path=output_path,
            size_bytes=len(payload),
            duration=job.duration,
            filter_graph="graph",
            effect_filters=(),
            overlay_path=kwargs.get("overlay_path"),
        )

    monkeypatch.setattr("clipping_ai.clips.recovery.render_clip", fake_render)
    monkeypatch.setattr(
        "clipping_ai.clips.recovery.probe_media",
        lambda path, config: MediaInfo(
            duration=2.6,
            has_video=True,
            has_audio=True,
            video_codec="mpeg4",
            audio_codec="aac",
            width=180,
            height=320,
        ),
    )
    return calls


def _stages(stage_store: StageStore, clip_id: str):
    return {
        record.stage: record
        for record in stage_store.list_stages(job_type=CLIP_JOB_TYPE, job_id=clip_id)
    }


def _reset_processing(clips: ClipService, clip_id: str) -> None:
    clips.store.update(
        clip_id,
        status=ClipStatus.PROCESSING,
        stage="rendering",
        error=None,
        completed_at=None,
    )


def _new_service(tmp_path: Path, video, clips: ClipService) -> ClipService:
    return ClipService(
        video.settings,
        video_service=video,
        config=clips.config,
        stage_store=clips.stage_store,
    )


def test_process_completes_all_durable_stages_and_reuses_valid_artifacts(tmp_path, monkeypatch):
    _, clips, clip, _, stage_store = _build(tmp_path)
    calls = _patch_render(monkeypatch)
    first = clips.process(clip.id)
    assert first.status == ClipStatus.READY
    assert set(_stages(stage_store, clip.id)) == {
        STAGE_PREPARE,
        STAGE_CAPTIONS,
        STAGE_OUTPUT,
        STAGE_VALIDATE,
        STAGE_FINALIZE,
    }
    assert all(
        record.status == StageStatus.DONE
        for record in _stages(stage_store, clip.id).values()
    )

    original = _stages(stage_store, clip.id)
    _reset_processing(clips, clip.id)
    resumed = _new_service(tmp_path, clips.video, clips).process(clip.id)
    assert resumed.status == ClipStatus.READY
    assert len(calls) == 1
    current = _stages(stage_store, clip.id)
    assert all(
        current[stage].attempt == original[stage].attempt
        for stage in (STAGE_PREPARE, STAGE_CAPTIONS, STAGE_OUTPUT, STAGE_VALIDATE)
    )


def test_ffmpeg_failure_preserves_valid_earlier_stages_and_retry_reuses_them(tmp_path, monkeypatch):
    _, clips, clip, _, stage_store = _build(tmp_path)
    _patch_render(monkeypatch, mode="ffmpeg_failure")
    failed = clips.process(clip.id)
    assert failed.status == ClipStatus.FAILED
    stages = _stages(stage_store, clip.id)
    assert stages[STAGE_PREPARE].status == StageStatus.DONE
    assert stages[STAGE_CAPTIONS].status == StageStatus.DONE
    assert stages[STAGE_OUTPUT].status != StageStatus.DONE
    assert (clips.clip_dir(clip.id) / "render.prepare.json").is_file()
    assert (clips.clip_dir(clip.id) / "render.captions.json").is_file()

    prepare_attempt = stages[STAGE_PREPARE].attempt
    captions_attempt = stages[STAGE_CAPTIONS].attempt
    assert clips.retry(clip.id).status == ClipStatus.QUEUED
    success_calls = _patch_render(monkeypatch)
    finished = clips.process(clip.id)
    assert finished.status == ClipStatus.READY
    assert len(success_calls) == 1
    current = _stages(stage_store, clip.id)
    assert current[STAGE_PREPARE].attempt == prepare_attempt
    assert current[STAGE_CAPTIONS].attempt == captions_attempt


def test_crash_after_output_validation_before_publication_publishes_staged_output(
    tmp_path, monkeypatch
):
    _, clips, clip, _, stage_store = _build(tmp_path)
    _patch_render(monkeypatch)
    staging = output_staging_path(clips.clip_dir(clip.id), clip)
    real_replace = os.replace

    def fail_before_publish(src, dst):
        if Path(src) == staging:
            raise OSError("crash before publication")
        return real_replace(src, dst)

    monkeypatch.setattr("clipping_ai.clips.recovery.os.replace", fail_before_publish)
    interrupted = clips.process(clip.id)
    assert interrupted.status == ClipStatus.FAILED
    assert staging.is_file()
    assert not (clips.clip_dir(clip.id) / f"clip_{clip.format.value}_{clip.quality.value}.mp4").is_file()

    monkeypatch.undo()
    success_calls = _patch_render(monkeypatch)
    _reset_processing(clips, clip.id)
    clips.stage_store.release_running(job_type=CLIP_JOB_TYPE, job_id=clip.id)
    resumed = _new_service(tmp_path, clips.video, clips).process(clip.id)
    assert resumed.status == ClipStatus.READY
    assert success_calls == []
    assert resumed.output_path is not None and resumed.output_path.is_file()


def test_crash_after_replace_before_stage_completion_completes_from_final_file(tmp_path, monkeypatch):
    _, clips, clip, _, stage_store = _build(tmp_path)
    _patch_render(monkeypatch)
    staging = output_staging_path(clips.clip_dir(clip.id), clip)
    real_replace = os.replace

    def fail_after_publish(src, dst):
        result = real_replace(src, dst)
        if Path(src) == staging:
            raise OSError("crash after publication")
        return result

    monkeypatch.setattr("clipping_ai.clips.recovery.os.replace", fail_after_publish)
    interrupted = clips.process(clip.id)
    assert interrupted.status == ClipStatus.FAILED
    final = clips.clip_dir(clip.id) / f"clip_{clip.format.value}_{clip.quality.value}.mp4"
    assert final.is_file()
    assert _stages(stage_store, clip.id)[STAGE_OUTPUT].status != StageStatus.DONE

    monkeypatch.undo()
    success_calls = _patch_render(monkeypatch)
    _reset_processing(clips, clip.id)
    clips.stage_store.release_running(job_type=CLIP_JOB_TYPE, job_id=clip.id)
    resumed = _new_service(tmp_path, clips.video, clips).process(clip.id)
    assert resumed.status == ClipStatus.READY
    assert success_calls == []
    assert resumed.output_path == final


def test_crash_after_publication_before_mark_ready_finalizes_without_rendering(tmp_path, monkeypatch):
    _, clips, clip, _, _ = _build(tmp_path)
    calls = _patch_render(monkeypatch)
    real_mark_ready = clips.store.mark_ready

    def fail_mark_ready(*args, **kwargs):
        real_mark_ready(*args, **kwargs)
        raise OSError("crash after database update")

    monkeypatch.setattr(clips.store, "mark_ready", fail_mark_ready)
    interrupted = clips.process(clip.id)
    assert interrupted.status == ClipStatus.FAILED
    assert clips.store.require(clip.id).status == ClipStatus.FAILED

    monkeypatch.undo()
    _reset_processing(clips, clip.id)
    clips.stage_store.release_running(job_type=CLIP_JOB_TYPE, job_id=clip.id)
    success_calls = _patch_render(monkeypatch)
    resumed = _new_service(tmp_path, clips.video, clips).process(clip.id)
    assert resumed.status == ClipStatus.READY
    assert len(success_calls) == 0
    assert len(calls) == 1


def test_corrupt_final_output_resets_output_downstream_and_rerenders(tmp_path, monkeypatch):
    _, clips, clip, _, stage_store = _build(tmp_path)
    calls = _patch_render(monkeypatch)
    finished = clips.process(clip.id)
    assert finished.output_path is not None
    finished.output_path.write_bytes(b"corrupted")

    _reset_processing(clips, clip.id)
    resumed = clips.process(clip.id)
    assert resumed.status == ClipStatus.READY
    assert len(calls) == 2
    assert resumed.output_path is not None
    assert resumed.output_path.read_bytes() == b"video-2"
    assert all(
        record.status == StageStatus.DONE
        for record in _stages(stage_store, clip.id).values()
    )


def test_changed_source_media_invalidates_preparation_and_output(tmp_path, monkeypatch):
    _, clips, clip, source_path, stage_store = _build(tmp_path)
    calls = _patch_render(monkeypatch)
    clips.process(clip.id)
    original_prepare = _stages(stage_store, clip.id)[STAGE_PREPARE]
    source_path.write_bytes(b"source-media-v2")

    _reset_processing(clips, clip.id)
    resumed = clips.process(clip.id)
    assert resumed.status == ClipStatus.READY
    assert len(calls) == 2
    current_prepare = _stages(stage_store, clip.id)[STAGE_PREPARE]
    assert current_prepare.input_fingerprint != original_prepare.input_fingerprint
    assert current_prepare.attempt > original_prepare.attempt


def test_changed_caption_language_invalidates_captions_and_downstream_only(tmp_path, monkeypatch):
    _, clips, clip, _, stage_store = _build(tmp_path)
    calls = _patch_render(monkeypatch)
    caption_calls: list[object] = []
    from clipping_ai.clips import recovery as clip_recovery

    original_build = clip_recovery.build_ass_file

    def tracked_build(*args, **kwargs):
        caption_calls.append(kwargs.get("language"))
        return original_build(*args, **kwargs)

    monkeypatch.setattr(clip_recovery, "build_ass_file", tracked_build)
    clips.store.update(clip.id, caption_language="ar")

    first = clips.process(clip.id)
    assert first.status == ClipStatus.READY
    stages = _stages(stage_store, clip.id)
    prepare_fingerprint = stages[STAGE_PREPARE].input_fingerprint
    prepare_attempt = stages[STAGE_PREPARE].attempt
    captions_attempt = stages[STAGE_CAPTIONS].attempt

    clips.store.update(clip.id, caption_language="en")
    _reset_processing(clips, clip.id)
    resumed = clips.process(clip.id)
    assert resumed.status == ClipStatus.READY
    assert len(calls) == 2
    current = _stages(stage_store, clip.id)
    assert current[STAGE_PREPARE].input_fingerprint == prepare_fingerprint
    assert current[STAGE_PREPARE].attempt == prepare_attempt
    assert current[STAGE_CAPTIONS].attempt > captions_attempt
    assert caption_calls == ["ar", "en"]


def test_active_stage_lease_rejects_concurrent_render(tmp_path, monkeypatch):
    _, clips, clip, _, stage_store = _build(tmp_path)
    calls = _patch_render(monkeypatch)
    finished = clips.process(clip.id)
    assert finished.status == ClipStatus.READY

    output = _stages(stage_store, clip.id)[STAGE_OUTPUT]
    stage_store.reset(
        job_type=CLIP_JOB_TYPE,
        job_id=clip.id,
        stage=STAGE_OUTPUT,
        input_fingerprint=str(output.input_fingerprint),
    )
    stage_store.claim_next(
        job_type=CLIP_JOB_TYPE,
        job_id=clip.id,
        stage=STAGE_OUTPUT,
        owner="other-worker",
        lease_seconds=120,
    )
    _reset_processing(clips, clip.id)
    with pytest.raises(AIError) as exc:
        _new_service(tmp_path, clips.video, clips).process(clip.id)
    assert exc.value.details.get("reason") == "lease_held"
    assert len(calls) == 1


def test_lease_loss_during_ffmpeg_prevents_publication(tmp_path, monkeypatch):
    _, clips, clip, _, stage_store = _build(tmp_path)
    calls: list[Path] = []
    lost = False

    def fake_render(job, *, output_path, cancel_check=None, **kwargs):
        calls.append(output_path)
        with sqlite3.connect(stage_store.db_path) as conn:
            conn.execute(
                "UPDATE job_stages SET lease_expires_at = 0 WHERE job_type = ? AND job_id = ? AND stage = ?",
                (CLIP_JOB_TYPE, clip.id, STAGE_OUTPUT),
            )
        if cancel_check is not None and cancel_check():
            nonlocal lost
            lost = True
            raise AIError(
                ErrorCategory.INTERNAL_ERROR,
                "lease lost",
                retryable=True,
                details={"reason": "lease_lost"},
            )
        return RenderResult(output_path, len(b"video"), job.duration)

    monkeypatch.setattr("clipping_ai.clips.recovery.render_clip", fake_render)
    monkeypatch.setattr(
        "clipping_ai.clips.recovery.probe_media",
        lambda path, config: MediaInfo(2.6, True, True, "mpeg4", "aac", 180, 320),
    )
    failed = clips.process(clip.id)
    assert failed.status == ClipStatus.FAILED
    assert lost
    assert not (clips.clip_dir(clip.id) / f"clip_{clip.format.value}_{clip.quality.value}.mp4").exists()


def test_cancellation_during_render_marks_cancelled_without_ready_output(tmp_path, monkeypatch):
    _, clips, clip, _, _ = _build(tmp_path)
    calls: list[Path] = []

    def fake_render(job, *, output_path, cancel_check=None, **kwargs):
        calls.append(output_path)
        clips.cancel(clip.id)
        if cancel_check is not None and cancel_check():
            raise AIError(ErrorCategory.INVALID_REQUEST, "cancelled", retryable=False)
        return RenderResult(output_path, len(b"video"), job.duration)

    monkeypatch.setattr("clipping_ai.clips.recovery.render_clip", fake_render)
    result = clips.process(clip.id)
    assert result.status == ClipStatus.CANCELLED
    assert not (clips.clip_dir(clip.id) / f"clip_{clip.format.value}_{clip.quality.value}.mp4").exists()


@pytest.mark.parametrize("keep_failed_artifacts", [False, True])
def test_failure_cleanup_preserves_valid_stage_artifacts(
    tmp_path, monkeypatch, keep_failed_artifacts
):
    config = small_clip_config(keep_failed_artifacts=keep_failed_artifacts)
    _, clips, clip, _, stage_store = _build(tmp_path, config=config)
    _patch_render(monkeypatch, mode="ffmpeg_failure")
    failed = clips.process(clip.id)
    assert failed.status == ClipStatus.FAILED
    directory = clips.clip_dir(clip.id)
    assert (directory / "render.prepare.json").is_file()
    assert (directory / "render.captions.json").is_file()
    assert not list(directory.glob("*.part.mp4"))
    assert all(
        record.status == StageStatus.DONE
        for stage, record in _stages(stage_store, clip.id).items()
        if stage in {STAGE_PREPARE, STAGE_CAPTIONS}
    )


def test_app_shares_stage_store_with_clip_service(tmp_path):
    video = make_service(tmp_path)
    app = create_app(
        video_service=video,
        start_worker=False,
        start_clip_worker=False,
    )
    assert app.state.clips.stage_store is app.state.stage_store


def test_clip_worker_uses_durable_service_and_ready_clip_download(tmp_path, monkeypatch):
    _, clips, clip, _, stage_store = _build(tmp_path)
    _patch_render(monkeypatch)
    worker = ClipWorker(clips)
    claimed = worker.run_once()
    assert claimed is not None and claimed.status == ClipStatus.READY
    assert all(
        record.status == StageStatus.DONE
        for record in _stages(stage_store, clip.id).values()
    )

    app = create_app(
        video_service=clips.video,
        clip_service=clips,
        start_worker=False,
        start_clip_worker=False,
    )
    with TestClient(app) as client:
        response = client.get(f"/api/clip-jobs/{clip.id}/download")
    assert response.status_code == 200
    assert response.content == b"video-1"


def test_cli_commands_operate_on_durable_clip_jobs(tmp_path, monkeypatch):
    from clipping_ai import cli

    _, clips, clip, _, _ = _build(tmp_path)
    _patch_render(monkeypatch)
    monkeypatch.setattr(cli, "_clip_service", lambda: clips)
    assert cli.main(["render-clip", clip.id, "--json"]) == 0
    assert clips.store.require(clip.id).status == ClipStatus.READY
    assert cli.main(["clip-job", clip.id, "--json"]) == 0
    assert cli.main(["retry-clip", clip.id, "--json"]) != 0


def test_heartbeat_keeps_lease_alive_and_expired_lease_is_reclaimable(tmp_path):
    stage_store = StageStore(tmp_path / "jobs.db")
    fingerprint = "fingerprint"
    stage_store.ensure(
        job_type=CLIP_JOB_TYPE,
        job_id="c" * 32,
        stage=STAGE_OUTPUT,
        input_fingerprint=fingerprint,
    )
    owner_a = "clip-a"
    owner_b = "clip-b"
    claimed = stage_store.claim_next(
        job_type=CLIP_JOB_TYPE,
        job_id="c" * 32,
        stage=STAGE_OUTPUT,
        owner=owner_a,
        lease_seconds=1,
        now=time.time(),
    )
    assert claimed is not None
    assert stage_store.heartbeat(
        job_type=CLIP_JOB_TYPE,
        job_id="c" * 32,
        stage=STAGE_OUTPUT,
        owner=owner_a,
        lease_seconds=30,
    )
    second = stage_store.claim_next(
        job_type=CLIP_JOB_TYPE,
        job_id="c" * 32,
        stage=STAGE_OUTPUT,
        owner=owner_b,
        lease_seconds=30,
    )
    assert second is None
    assert stage_store.expire_leases(now=time.time() + 60) == 1
    reclaimed = stage_store.claim_next(
        job_type=CLIP_JOB_TYPE,
        job_id="c" * 32,
        stage=STAGE_OUTPUT,
        owner=owner_b,
        lease_seconds=30,
    )
    assert reclaimed is not None
    assert reclaimed.lease_owner == owner_b
