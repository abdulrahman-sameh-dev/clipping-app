"""HTTP routes for the YouTube upload panel. No network: uploads use a fake uploader."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from clipping_ai.api.app import create_app
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.youtube.config import YouTubeSettings
from tests.helpers import make_manager, make_registry
from tests.test_clips import make_clip_job

CLIP_ID = "a" * 32
SECRET = "client-secret-value"
ACCESS = "access-token-value"
REFRESH = "refresh-token-value"


class FakeClips:
    def __init__(self, video: Path) -> None:
        self.video = video
        self.trashed: set[str] = set()

    def get(self, clip_id: str):
        if clip_id != CLIP_ID:
            raise AIError(ErrorCategory.NOT_FOUND, "Clip not found.", retryable=False)
        deleted = datetime.now(UTC).isoformat() if clip_id in self.trashed else None
        return make_clip_job(clip_id, deleted_at=deleted, hashtags=["tag"], description="About")

    def display_title(self, clip) -> str:
        return "Saved title"

    def output_path(self, clip_id: str) -> Path:
        return self.video


class FakeUploader:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.gate: threading.Event | None = None
        self.started = threading.Event()

    def upload_video(self, path: Path, request) -> dict[str, Any]:
        self.calls.append(request)
        self.started.set()
        if self.gate is not None:
            self.gate.wait(5)
        return {"id": "vid123"}


def yt_settings(**overrides) -> YouTubeSettings:
    values = {
        "enabled": True,
        "client_id": "cid",
        "client_secret": SECRET,
        "_env_file": None,
    }
    values.update(overrides)
    return YouTubeSettings(**values)


@pytest.fixture
def env(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video")
    manager = make_manager(make_registry(providers={}, models=[], routing={}), {}, tmp_path)
    clips = FakeClips(video)
    uploader = FakeUploader()

    def build(*, authorised: bool = True, **settings):
        token = manager.settings.data_dir / "youtube" / "token.json"
        token.unlink(missing_ok=True)
        if authorised:
            token.parent.mkdir(parents=True, exist_ok=True)
            token.write_text(
                json.dumps({"refresh_token": REFRESH, "access_token": ACCESS}), encoding="utf-8"
            )
        app = create_app(
            manager,
            clip_service=clips,
            start_worker=False,
            youtube_settings=yt_settings(**settings),
            youtube_uploader=uploader,
        )
        return TestClient(app)

    return build, uploader, clips, manager.settings.data_dir


def future(days: int = 2, offset: str = "+02:00") -> str:
    when = datetime.now(UTC) + timedelta(days=days)
    return when.strftime("%Y-%m-%dT%H:%M:%S") + offset


def upload(client, body: dict[str, Any] | None = None, clip_id: str = CLIP_ID):
    return client.post(f"/api/youtube/clips/{clip_id}/upload", json=body or {})


def test_status_reports_state_and_hides_secrets(env):
    build, *_ = env
    response = build().get("/api/youtube/status")
    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "client_configured": True,
        "authorised": True,
        "default_privacy": "private",
        "made_for_kids": False,
    }
    for secret in (SECRET, ACCESS, REFRESH, "cid"):
        assert secret not in response.text


def test_status_when_disabled_and_not_authorised(env):
    build, *_ = env
    body = build(authorised=False, enabled=False).get("/api/youtube/status").json()
    assert body["enabled"] is False and body["authorised"] is False


def test_upload_defaults_to_private_and_records_the_upload(env):
    build, uploader, _, data_dir = env
    client = build()
    with respx.mock:  # any real HTTP request would raise
        response = upload(client)
    assert response.status_code == 200, response.text
    record = response.json()["upload"]
    assert record["video_id"] == "vid123" and record["privacy"] == "private"
    assert record["studio_url"] == "https://studio.youtube.com/video/vid123/edit"
    assert uploader.calls[0].privacy.value == "private"
    assert uploader.calls[0].title == "Saved title"
    assert (data_dir / "youtube" / "uploads.json").is_file()
    for secret in (SECRET, ACCESS, REFRESH):
        assert secret not in response.text
    state = client.get(f"/api/youtube/clips/{CLIP_ID}").json()
    assert state["upload"]["video_id"] == "vid123" and state["in_progress"] is False


def test_scheduled_upload_is_private_with_publish_time(env):
    build, uploader, *_ = env
    response = upload(build(), {"publish_at": future()})
    assert response.status_code == 200, response.text
    assert response.json()["upload"]["publish_at"].endswith("Z")
    assert uploader.calls[0].api_body()["status"]["privacyStatus"] == "private"


@pytest.mark.parametrize(
    "body",
    [
        {"privacy": "unlisted", "publish_at": "FUTURE"},
        {"privacy": "public", "publish_at": "FUTURE", "confirm_public": True},
        {"publish_at": "2020-01-01T00:00:00+00:00"},
        {"publish_at": "2099-01-01T00:00:00"},  # no UTC offset
        {"publish_at": "tomorrow"},
    ],
)
def test_bad_schedules_are_rejected_before_any_upload(env, body):
    build, uploader, *_ = env
    body = {k: (future() if v == "FUTURE" else v) for k, v in body.items()}
    response = upload(build(), body)
    assert response.status_code == 400, response.text
    assert uploader.calls == []


def test_public_needs_explicit_confirmation(env):
    build, uploader, *_ = env
    client = build()
    refused = upload(client, {"privacy": "public"})
    assert refused.status_code == 400 and uploader.calls == []
    accepted = upload(client, {"privacy": "public", "confirm_public": True})
    assert accepted.status_code == 200
    assert accepted.json()["upload"]["privacy"] == "public"


def test_uploaded_clip_is_refused_the_second_time(env):
    build, uploader, *_ = env
    client = build()
    assert upload(client).status_code == 200
    again = upload(client)
    assert again.status_code == 400 and "already" in again.json()["error"]["message"]
    assert len(uploader.calls) == 1


def test_force_is_not_accepted_over_http(env):
    build, uploader, *_ = env
    response = upload(build(), {"force": True})
    assert response.status_code == 422 and uploader.calls == []


def test_concurrent_upload_of_same_clip_is_blocked(env):
    build, uploader, *_ = env
    client = build()
    uploader.gate = threading.Event()
    first: dict[str, Any] = {}
    worker = threading.Thread(target=lambda: first.update(r=upload(client)))
    worker.start()
    assert uploader.started.wait(5)
    assert client.get(f"/api/youtube/clips/{CLIP_ID}").json()["in_progress"] is True
    second = upload(client)
    assert second.status_code == 400 and "already being uploaded" in second.text
    uploader.gate.set()
    worker.join(5)
    assert first["r"].status_code == 200
    assert len(uploader.calls) == 1
    assert client.get(f"/api/youtube/clips/{CLIP_ID}").json()["in_progress"] is False


def test_disabled_and_unauthorised_are_refused(env):
    build, uploader, *_ = env
    assert upload(build(enabled=False)).status_code == 403
    unauthorised = upload(build(authorised=False))
    assert unauthorised.status_code == 400 and "youtube auth" in unauthorised.text
    missing = upload(build(client_id=None))
    assert missing.status_code != 200
    assert uploader.calls == []


def test_trashed_and_unknown_clips_are_refused(env):
    build, uploader, clips, _ = env
    client = build()
    assert upload(client, clip_id="f" * 32).status_code == 404
    assert client.get(f"/api/youtube/clips/{'f' * 32}").status_code == 404
    clips.trashed.add(CLIP_ID)
    assert upload(client).status_code == 404
    assert uploader.calls == []


def test_failed_upload_is_not_recorded_and_can_be_retried(env):
    build, uploader, *_ = env
    client = build()
    original = uploader.upload_video

    def boom(path, request):
        raise AIError(ErrorCategory.PROVIDER_UNAVAILABLE, "YouTube is down.", retryable=True)

    uploader.upload_video = boom  # type: ignore[method-assign]
    failed = upload(client)
    assert failed.status_code >= 400 and "YouTube is down." in failed.text
    state = client.get(f"/api/youtube/clips/{CLIP_ID}").json()
    assert state["upload"] is None and state["in_progress"] is False
    uploader.upload_video = original  # type: ignore[method-assign]
    assert upload(client).status_code == 200


def test_frontend_files_for_the_panel_are_served(env):
    build, *_ = env
    client = build()
    assert client.get("/ui/js/youtube.js").status_code == 200
    assert client.get("/ui/youtube.css").status_code == 200
    html = client.get("/ui/").text
    assert "youtube.css" in html
    assert "youtubePanelHtml" in client.get("/ui/js/app.js").text
