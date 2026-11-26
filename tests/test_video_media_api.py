from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from clipping_ai.api.app import create_app
from clipping_ai.video.models import JobStage, JobStatus, SourceType
from clipping_ai.video.service import VideoService
from tests.video_helpers import make_service

JOB_ID = "a" * 32
SOURCE_BYTES = b"0123456789abcdef"


def _client(service: VideoService) -> TestClient:
    app = create_app(
        SimpleNamespace(settings=service.settings),  # type: ignore[arg-type]
        video_service=service,
        start_worker=False,
    )
    return TestClient(app)


def _ready_job(
    service: VideoService,
    source: Path,
    *,
    job_id: str = JOB_ID,
    status: JobStatus = JobStatus.READY,
    deleted: bool = False,
) -> None:
    service.store.create(
        job_id=job_id,
        source_type=SourceType.UPLOAD,
        source_value=source.name,
        input_path=source,
    )
    updates: dict[str, object] = {
        "status": status,
        "stage": JobStage.READY if status == JobStatus.READY else JobStage.QUEUED,
    }
    if deleted:
        updates["deleted_at"] = "2026-01-01T00:00:00+00:00"
    service.store.update(job_id, **updates)


def _assert_no_paths(response_text: str, service: VideoService, tmp_path: Path) -> None:
    assert str(tmp_path) not in response_text
    assert str(service.data_dir) not in response_text
    assert "outside.mp4" not in response_text


def test_source_media_returns_inline_file_and_supports_ranges(tmp_path):
    service = make_service(tmp_path)
    source = service.data_dir / "source.mp4"
    source.write_bytes(SOURCE_BYTES)
    _ready_job(service, source)
    client = _client(service)

    response = client.get(f"/api/video/jobs/{JOB_ID}/media")
    assert response.status_code == 200
    assert response.content == SOURCE_BYTES
    assert response.headers["content-type"].startswith("video/mp4")
    assert response.headers["content-disposition"].lower().startswith("inline")
    assert response.headers["accept-ranges"] == "bytes"

    ranged = client.get(
        f"/api/video/jobs/{JOB_ID}/media",
        headers={"Range": "bytes=2-5"},
    )
    assert ranged.status_code == 206
    assert ranged.content == SOURCE_BYTES[2:6]
    assert ranged.headers["content-type"].startswith("video/mp4")
    assert ranged.headers["content-range"] == f"bytes 2-5/{len(SOURCE_BYTES)}"
    assert ranged.headers["accept-ranges"] == "bytes"


def test_source_media_unsatisfiable_range_follows_framework(tmp_path):
    service = make_service(tmp_path)
    source = service.data_dir / "source.mp4"
    source.write_bytes(SOURCE_BYTES)
    _ready_job(service, source)
    client = _client(service)

    response = client.get(
        f"/api/video/jobs/{JOB_ID}/media",
        headers={"Range": "bytes=999-1000"},
    )
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(SOURCE_BYTES)}"


def test_source_media_returns_404_for_missing_job(tmp_path):
    service = make_service(tmp_path)
    client = _client(service)

    response = client.get(f"/api/video/jobs/{'b' * 32}/media")
    assert response.status_code == 404
    _assert_no_paths(response.text, service, tmp_path)


def test_source_media_rejects_invalid_job_id(tmp_path):
    service = make_service(tmp_path)
    client = _client(service)

    response = client.get("/api/video/jobs/not-a-job/media")
    assert response.status_code == 400
    _assert_no_paths(response.text, service, tmp_path)


def test_source_media_rejects_deleted_and_unavailable_jobs(tmp_path):
    service = make_service(tmp_path)
    source = service.data_dir / "source.mp4"
    source.write_bytes(SOURCE_BYTES)
    _ready_job(service, source, job_id="b" * 32, deleted=True)
    _ready_job(service, source, job_id="c" * 32, status=JobStatus.QUEUED)
    client = _client(service)

    deleted = client.get(f"/api/video/jobs/{'b' * 32}/media")
    assert deleted.status_code == 400
    assert "Restore" in deleted.json()["error"]["message"]
    _assert_no_paths(deleted.text, service, tmp_path)

    unavailable = client.get(f"/api/video/jobs/{'c' * 32}/media")
    assert unavailable.status_code == 400
    assert "not ready" in unavailable.json()["error"]["message"]
    _assert_no_paths(unavailable.text, service, tmp_path)


def test_source_media_rejects_missing_source_file(tmp_path):
    service = make_service(tmp_path)
    missing = service.data_dir / "missing.mp4"
    _ready_job(service, missing, job_id="d" * 32)
    client = _client(service)

    response = client.get(f"/api/video/jobs/{'d' * 32}/media")
    assert response.status_code == 404
    _assert_no_paths(response.text, service, tmp_path)


def test_source_media_rejects_paths_outside_data_dir(tmp_path):
    service = make_service(tmp_path)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(SOURCE_BYTES)
    _ready_job(service, outside, job_id="e" * 32)
    client = _client(service)

    response = client.get(f"/api/video/jobs/{'e' * 32}/media")
    assert response.status_code == 400
    _assert_no_paths(response.text, service, tmp_path)


def test_source_media_rejects_path_traversal(tmp_path):
    service = make_service(tmp_path)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(SOURCE_BYTES)
    traversal = service.data_dir / "videos" / ".." / ".." / "outside.mp4"
    _ready_job(service, traversal, job_id="f" * 32)
    client = _client(service)

    response = client.get(f"/api/video/jobs/{'f' * 32}/media")
    assert response.status_code == 400
    _assert_no_paths(response.text, service, tmp_path)


def test_source_media_rejects_symlink_escape(tmp_path):
    service = make_service(tmp_path)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(SOURCE_BYTES)
    link = service.data_dir / "linked-source.mp4"
    link.symlink_to(outside)
    _ready_job(service, link, job_id="1" * 32)
    client = _client(service)

    response = client.get(f"/api/video/jobs/{'1' * 32}/media")
    assert response.status_code == 400
    _assert_no_paths(response.text, service, tmp_path)
