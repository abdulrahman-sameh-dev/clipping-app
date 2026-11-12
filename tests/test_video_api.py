from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clipping_ai.api.app import create_app
from clipping_ai.errors import AIError
from clipping_ai.video.config import VideoConfig
from clipping_ai.video.models import JobStatus
from clipping_ai.video.worker import VideoWorker
from tests.video_helpers import FakeDownloader, make_service, make_test_video, requires_ffmpeg


def make_client(service):
    app = create_app(
        SimpleNamespace(settings=service.settings),  # type: ignore[arg-type]
        video_service=service,
        start_worker=False,
    )
    return TestClient(app)


@requires_ffmpeg
def test_upload_status_list_and_details(tmp_path):
    source = make_test_video(tmp_path / "sample.mp4")
    service = make_service(tmp_path)
    client = make_client(service)
    response = client.post(
        "/api/video/upload",
        files={"file": ("sample.mp4", source.read_bytes(), "video/mp4")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["duplicate"] is False
    job_id = body["job"]["id"]
    assert body["job"]["status"] == "queued"

    service.process(job_id)
    detail = client.get(f"/api/video/jobs/{job_id}")
    assert detail.status_code == 200
    assert detail.json()["job"]["status"] == "ready"
    status = client.get(f"/api/video/jobs/{job_id}/status")
    assert status.status_code == 200
    assert status.json()["progress"] == 1.0
    jobs = client.get("/api/video/jobs", params={"status": "ready"})
    assert jobs.status_code == 200
    assert [item["id"] for item in jobs.json()["jobs"]] == [job_id]
    config = client.get("/api/video/config")
    assert config.status_code == 200
    assert config.json()["audio_codec"] == "flac"


@requires_ffmpeg
def test_upload_duplicate_and_validation_errors(tmp_path):
    source = make_test_video(tmp_path / "sample.mp4")
    service = make_service(tmp_path, config=VideoConfig(max_upload_bytes=100_000))
    client = make_client(service)
    payload = source.read_bytes()
    first = client.post(
        "/api/video/upload",
        files={"file": ("sample.mp4", payload, "video/mp4")},
    )
    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    second = client.post(
        "/api/video/upload",
        files={"file": ("sample.mp4", payload, "video/mp4")},
    )
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["job"]["id"] == first.json()["job"]["id"]

    too_large = client.post(
        "/api/video/upload",
        files={"file": ("sample.mp4", b"x" * 100_001, "video/mp4")},
    )
    assert too_large.status_code == 400
    assert too_large.json()["error"]["category"] == "invalid_request"


@requires_ffmpeg
def test_url_endpoint_and_worker(tmp_path):
    source = make_test_video(tmp_path / "sample.mp4")
    downloader = FakeDownloader(source)
    service = make_service(tmp_path, downloader=downloader)
    client = make_client(service)
    response = client.post("/api/video/url", json={"url": "https://example.com/video"})
    assert response.status_code == 200
    job_id = response.json()["job"]["id"]
    worker = VideoWorker(service)
    finished = worker.run_once()
    assert finished is not None and finished.status == JobStatus.READY
    detail = client.get(f"/api/video/jobs/{job_id}")
    assert detail.json()["job"]["source_type"] == "url"


def test_error_responses_and_cancel_retry(tmp_path):
    service = make_service(tmp_path)
    client = make_client(service)
    assert client.get("/api/video/jobs/not-a-job").status_code == 400
    assert client.get(f"/api/video/jobs/{'a' * 32}").status_code == 404
    assert client.get("/api/video/jobs", params={"status": "nope"}).status_code == 400

    upload = client.post(
        "/api/video/upload",
        files={"file": ("clip.mp4", b"data", "video/mp4")},
    )
    job_id = upload.json()["job"]["id"]
    cancelled = client.post(f"/api/video/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["job"]["status"] == "cancelled"
    retried = client.post(f"/api/video/jobs/{job_id}/retry")
    assert retried.status_code == 200
    assert retried.json()["job"]["status"] == "queued"
    assert client.post(f"/api/video/jobs/{job_id}/retry").status_code == 400


@requires_ffmpeg
def test_failed_job_retry_endpoint(tmp_path):
    source = make_test_video(tmp_path / "video-only.mp4", with_audio=False)
    service = make_service(tmp_path)
    client = make_client(service)
    upload = client.post(
        "/api/video/upload",
        files={"file": ("video-only.mp4", source.read_bytes(), "video/mp4")},
    )
    job_id = upload.json()["job"]["id"]
    with pytest.raises(AIError):
        service.process(job_id)
    detail = client.get(f"/api/video/jobs/{job_id}")
    assert detail.json()["job"]["status"] == "failed"
    retried = client.post(f"/api/video/jobs/{job_id}/retry")
    assert retried.status_code == 200
    assert retried.json()["job"]["status"] == "queued"
