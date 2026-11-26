from __future__ import annotations

import os
import shutil
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipping_ai.api.app import create_app
from clipping_ai.clips.models import (
    ClipFit,
    ClipFormat,
    ClipQuality,
    ClipStage,
    ClipStatus,
)
from clipping_ai.clips.store import ClipJobStore
from clipping_ai.errors import AIError
from clipping_ai.manager import ProviderManager
from clipping_ai.recovery import (
    DataDirectoryLock,
    DataDirectoryLockedError,
    IdempotencyStore,
    ProviderRuntimeStore,
    StageArtifactError,
    StageOwnershipError,
    StageStatus,
    StageStore,
    StartupReconciler,
    migrate_jobs_db,
    schema_version,
)
from clipping_ai.registry import PolicyConfig, ProviderConfig, RouteCandidate
from clipping_ai.settings import Settings
from clipping_ai.throttle import Throttle
from clipping_ai.usage import UsageTracker
from tests.helpers import (
    FakeTextProvider,
    make_manager,
    make_registry,
    quota_error,
    text_model,
)
from tests.video_helpers import make_service


def test_schema_migration_is_versioned_and_idempotent(tmp_path):
    db_path = tmp_path / "jobs.db"
    assert migrate_jobs_db(db_path) == 1
    assert migrate_jobs_db(db_path) == 1
    assert schema_version(db_path) == 1

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {"schema_migrations", "job_stages", "provider_runtime", "idempotency_keys"} <= tables


def test_stage_claim_expiry_heartbeat_and_owner_checks(tmp_path):
    store = StageStore(tmp_path / "jobs.db")
    stage = store.ensure(
        job_type="analysis",
        job_id="a" * 32,
        stage="transcribe",
        input_fingerprint="fingerprint",
        checkpoint={"last_chunk": 2},
    )
    assert stage.status is StageStatus.PENDING
    assert store.ensure(
        job_type="analysis", job_id="a" * 32, stage="transcribe"
    ).attempt == 0

    start = time.time()
    first = store.claim_next(owner="worker-1", lease_seconds=10, now=start)
    assert first is not None
    assert first.attempt == 1
    assert first.lease_owner == "worker-1"
    assert store.claim_next(owner="worker-2", lease_seconds=10, now=start + 5) is None
    assert store.heartbeat(
        job_type="analysis",
        job_id="a" * 32,
        stage="transcribe",
        owner="worker-1",
        lease_seconds=5,
        now=start + 5,
    )

    second = store.claim_next(owner="worker-2", lease_seconds=10, now=start + 11)
    assert second is not None
    assert second.attempt == 2
    assert second.lease_owner == "worker-2"
    assert second.checkpoint == {"last_chunk": 2}

    artifact = tmp_path / "analysis.json"
    artifact.write_text("{}", encoding="utf-8")
    with pytest.raises(StageOwnershipError):
        store.complete(
            job_type="analysis",
            job_id="a" * 32,
            stage="transcribe",
            owner="worker-1",
            artifact_path=artifact,
            require_artifact=True,
        )
    completed = store.complete(
        job_type="analysis",
        job_id="a" * 32,
        stage="transcribe",
        owner="worker-2",
        artifact_path=artifact,
        require_artifact=True,
    )
    assert completed.status is StageStatus.DONE
    assert completed.artifact_sha256 is not None
    assert completed.artifact_size == 2


def test_stage_completion_requires_artifact_and_preserves_checkpoint_on_failure(tmp_path):
    store = StageStore(tmp_path / "jobs.db")
    store.ensure(job_type="video", job_id="b" * 32, stage="audio")
    claimed = store.claim_next(owner="worker", lease_seconds=10)
    assert claimed is not None
    store.save_checkpoint(
        job_type="video",
        job_id="b" * 32,
        stage="audio",
        owner="worker",
        checkpoint={"completed_chunks": [0, 1]},
    )

    with pytest.raises(StageArtifactError):
        store.complete(
            job_type="video",
            job_id="b" * 32,
            stage="audio",
            owner="worker",
            require_artifact=True,
        )
    still_running = store.get(job_type="video", job_id="b" * 32, stage="audio")
    assert still_running is not None
    assert still_running.status is StageStatus.RUNNING
    assert still_running.checkpoint == {"completed_chunks": [0, 1]}

    failed = store.fail(
        job_type="video",
        job_id="b" * 32,
        stage="audio",
        owner="worker",
        error={"category": "network", "message": "temporary failure"},
        retryable=True,
    )
    assert failed.status is StageStatus.PENDING
    assert failed.checkpoint == {"completed_chunks": [0, 1]}
    reclaimed = store.claim_next(owner="worker-2", lease_seconds=10)
    assert reclaimed is not None
    assert reclaimed.attempt == 2
    assert reclaimed.checkpoint == {"completed_chunks": [0, 1]}


def test_expired_lease_cannot_be_renewed_or_completed(tmp_path):
    store = StageStore(tmp_path / "jobs.db")
    store.ensure(job_type="video", job_id="e" * 32, stage="audio")
    start = time.time()
    claimed = store.claim_next(owner="worker", lease_seconds=1, now=start)
    assert claimed is not None
    assert not store.heartbeat(
        job_type="video",
        job_id="e" * 32,
        stage="audio",
        owner="worker",
        now=start + 2,
    )
    with pytest.raises(StageOwnershipError):
        store.fail(
            job_type="video",
            job_id="e" * 32,
            stage="audio",
            owner="worker",
            error={"category": "network"},
            retryable=True,
            now=start + 2,
        )


def test_startup_reconciliation_removes_only_stale_temp_files(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    store = StageStore(tmp_path / "jobs.db")
    store.ensure(job_type="video", job_id="c" * 32, stage="audio")
    store.claim_next(owner="worker", lease_seconds=1, now=time.time() - 100)

    final = data_dir / "videos" / "jobs" / ("c" * 32) / "input.mp4"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"final")
    store.ensure(
        job_type="video",
        job_id="c" * 32,
        stage="finalize",
        status=StageStatus.DONE,
        artifact_path=final,
    )
    with store._session() as conn:
        conn.execute(
            """
            UPDATE job_stages SET artifact_sha256 = ?, completed_at = ?
            WHERE job_type = 'video' AND job_id = ? AND stage = 'finalize'
            """,
            ("bad-hash", datetime.now(UTC).isoformat(), "c" * 32),
        )

    fresh_temp = data_dir / "videos" / "tmp" / "fresh.part"
    stale_temp = data_dir / "videos" / "tmp" / "stale.part"
    fresh_temp.parent.mkdir(parents=True)
    fresh_temp.write_bytes(b"fresh")
    stale_temp.write_bytes(b"stale")
    old = time.time() - (48 * 60 * 60)
    os.utime(stale_temp, (old, old))

    report = StartupReconciler(data_dir, store).run()
    assert report.expired_leases == 1
    assert report.invalid_artifacts == ("video:" + "c" * 32 + ":finalize",)
    assert stale_temp in {Path(item) for item in report.removed_temp_files}
    assert not stale_temp.exists()
    assert fresh_temp.exists()
    assert final.exists()


def test_startup_reconciliation_reports_missing_artifacts(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    store = StageStore(tmp_path / "jobs.db")
    artifact = data_dir / "output.txt"
    artifact.write_text("valid", encoding="utf-8")
    store.ensure(
        job_type="clip",
        job_id="d" * 32,
        stage="render",
        status=StageStatus.DONE,
        artifact_path=artifact,
    )
    with store._session() as conn:
        conn.execute(
            """
            UPDATE job_stages SET completed_at = ?
            WHERE job_type = 'clip' AND job_id = ? AND stage = 'render'
            """,
            (datetime.now(UTC).isoformat(), "d" * 32),
        )
    artifact.unlink()

    report = StartupReconciler(data_dir, store).run(now=time.time())
    assert report.missing_artifacts == ("clip:" + "d" * 32 + ":render",)
    assert not report.orphaned_temp_files


def test_data_directory_lock_rejects_concurrent_owner(tmp_path):
    first = DataDirectoryLock(tmp_path / "data")
    second = DataDirectoryLock(tmp_path / "data")
    first.acquire()
    try:
        with pytest.raises(DataDirectoryLockedError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_provider_runtime_state_survives_manager_restart(tmp_path):
    provider = FakeTextProvider("p1", [quota_error("p1", "free-1")])
    registry = make_registry(
        providers={
            "p1": ProviderConfig(
                type="litellm", enabled=True, api_key_env=None, max_retries=0
            )
        },
        models=[text_model("free-1", provider="p1")],
        routing={"content_analysis": [RouteCandidate(provider="p1", model="free-1")]},
        policy=PolicyConfig(schema_validation_retries=0),
    )
    settings = Settings(
        config_dir=tmp_path / "config", data_dir=tmp_path / "data", _env_file=None
    )
    settings.ensure_dirs()
    manager = ProviderManager(
        settings=settings,
        registry=registry,
        usage=UsageTracker(settings.usage_db_path),
        providers={"p1": provider},
        throttle=Throttle(),
        sleep_fn=lambda _: None,
    )
    with pytest.raises(AIError):
        manager.analyze("content_analysis", text="hello")

    runtime = ProviderRuntimeStore(settings.jobs_db_path)
    assert runtime.cooldown_remaining("p1") > 0
    restarted = ProviderManager(
        settings=settings,
        registry=registry,
        usage=UsageTracker(settings.usage_db_path),
        providers={"p1": provider},
        throttle=Throttle(),
        sleep_fn=lambda _: None,
    )
    assert restarted._provider_cooldown("p1") > 0
    assert restarted.list_providers()[0]["cooldown_seconds"] > 0


def test_url_idempotency_does_not_persist_the_raw_url(tmp_path):
    service = make_service(tmp_path)
    url = "https://example.com/watch?v=private-token"
    first, duplicate = service.create_url(url)
    assert duplicate is False
    second, duplicate = service.create_url(url)
    assert duplicate is True
    assert second.id == first.id

    with sqlite3.connect(service.settings.jobs_db_path) as conn:
        keys = [row[0] for row in conn.execute("SELECT key FROM idempotency_keys")]
    assert keys
    assert all("example.com" not in key and "private-token" not in key for key in keys)


def test_video_upload_idempotency_returns_same_job_across_concurrent_requests(tmp_path):
    service = make_service(tmp_path)
    payload = b"same video bytes"

    def upload():
        return service.create_upload("clip.mp4", BytesIO(payload))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: upload(), range(2)))

    ids = {job.id for job, _duplicate in results}
    assert len(ids) == 1
    assert sorted(duplicate for _job, duplicate in results) == [False, True]
    assert service.store.count() == 1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_clip_creation_idempotency_returns_existing_clips(tmp_path):
    from tests.test_clips import add_transcript_and_highlight, make_clip_service
    from tests.video_helpers import make_test_video

    source = make_test_video(tmp_path / "source.mp4", duration=3.0)
    video, clips = make_clip_service(tmp_path)
    job = video.prepare_local(source)
    add_transcript_and_highlight(video, job)

    first = clips.create_for_job(job.id, formats=[ClipFormat.VERTICAL])
    second = clips.create_for_job(job.id, formats=[ClipFormat.VERTICAL])
    assert [clip.id for clip in first] == [clip.id for clip in second]
    assert clips.store.count() == 1


def test_clip_store_idempotency_does_not_duplicate_rows(tmp_path):
    store = ClipJobStore(tmp_path / "jobs.db")
    now = datetime.now(UTC).isoformat()
    fields = {
        "id": "e" * 32,
        "source_job_id": "f" * 32,
        "highlight_index": 0,
        "start_time": 1.0,
        "end_time": 3.0,
        "duration": 2.0,
        "format": ClipFormat.VERTICAL,
        "width": 1080,
        "height": 1920,
        "fps": 30.0,
        "quality": ClipQuality.STANDARD,
        "fit": ClipFit.CROP,
        "captions": False,
        "status": ClipStatus.QUEUED,
        "stage": ClipStage.QUEUED,
        "created_at": now,
        "updated_at": now,
    }
    first, duplicate = store.create_idempotent(idempotency_key="same", **fields)
    assert duplicate is False
    existing, duplicate = store.create_idempotent(
        idempotency_key="same", **{**fields, "id": "a" * 32}
    )
    assert duplicate is True
    assert existing.id == first.id
    assert store.count() == 1


def test_app_lifespan_runs_reconciliation_before_serving(tmp_path):
    registry = make_registry(
        providers={},
        models=[],
        routing={},
        policy=PolicyConfig(schema_validation_retries=0),
    )
    manager = make_manager(registry, {}, tmp_path)
    stale = manager.settings.data_dir / "videos" / "tmp" / "stale.part"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale")
    old = time.time() - (48 * 60 * 60)
    os.utime(stale, (old, old))

    app = create_app(manager, start_worker=False)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert app.state.data_lock.held is True
    assert app.state.data_lock.held is False
    assert stale.exists() is False
    assert app.state.reconciliation is not None
    assert str(stale) in app.state.reconciliation.removed_temp_files


def test_idempotency_store_is_atomic(tmp_path):
    store = IdempotencyStore(tmp_path / "jobs.db")
    record, created = store.reserve(
        scope="video.create", key="key", job_type="video", job_id="a" * 32
    )
    assert created and record is None
    existing, created = store.reserve(
        scope="video.create", key="key", job_type="video", job_id="b" * 32
    )
    assert not created
    assert existing is not None and existing.job_id == "a" * 32
