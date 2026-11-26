from __future__ import annotations

import json
import stat
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from clipping_ai.cli import build_parser, main
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.settings import Settings
from clipping_ai.youtube import auth
from clipping_ai.youtube.client import UPLOAD_URL, YouTubeClient
from clipping_ai.youtube.config import YouTubeSettings
from clipping_ai.youtube.models import Privacy, UploadRequest, parse_publish_at
from clipping_ai.youtube.service import UploadStore, YouTubeUploadService
from tests.test_clips import make_clip_job

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def yt_settings(**overrides) -> YouTubeSettings:
    values = {
        "enabled": True,
        "client_id": "cid",
        "client_secret": "csecret",
        "_env_file": None,
    }
    values.update(overrides)
    return YouTubeSettings(**values)


def request(**overrides) -> UploadRequest:
    values = {"clip_id": "c1", "title": "Title", "privacy": Privacy.PRIVATE}
    values.update(overrides)
    return UploadRequest(**values)


# --- models -----------------------------------------------------------------


def test_defaults_are_private_and_disabled():
    settings = YouTubeSettings(_env_file=None)
    assert settings.enabled is False
    assert settings.default_privacy == Privacy.PRIVATE
    with pytest.raises(AIError) as exc:
        settings.require_enabled()
    assert exc.value.category == ErrorCategory.POLICY_VIOLATION


def test_api_body_always_sets_privacy_status():
    body = request().api_body()
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["selfDeclaredMadeForKids"] is False
    assert "publishAt" not in body["status"]


def test_schedule_requires_private_and_future():
    future = NOW + timedelta(days=2)
    ok = request(publish_at=future)
    ok.validate_policy(now=NOW)
    assert ok.api_body()["status"]["publishAt"] == "2026-06-03T12:00:00Z"
    assert ok.api_body()["status"]["privacyStatus"] == "private"

    with pytest.raises(AIError):
        request(publish_at=future, privacy=Privacy.UNLISTED).validate_policy(now=NOW)
    with pytest.raises(AIError):
        request(publish_at=NOW - timedelta(hours=1)).validate_policy(now=NOW)
    with pytest.raises(AIError):
        request(publish_at=NOW + timedelta(minutes=5)).validate_policy(now=NOW)
    with pytest.raises(AIError):
        request(publish_at=NOW + timedelta(days=400)).validate_policy(now=NOW)


def test_public_needs_confirmation():
    with pytest.raises(AIError):
        request(privacy=Privacy.PUBLIC).validate_policy(now=NOW)
    request(privacy=Privacy.PUBLIC, confirm_public=True).validate_policy(now=NOW)


def test_publish_at_needs_offset_and_converts_to_utc():
    assert parse_publish_at("2026-12-01T18:00:00+02:00") == datetime(2026, 12, 1, 16, 0, tzinfo=UTC)
    assert parse_publish_at("2026-12-01T16:00:00Z").tzinfo is not None
    with pytest.raises(AIError):
        parse_publish_at("2026-12-01T18:00:00")
    with pytest.raises(AIError):
        parse_publish_at("tomorrow")


def test_text_limits_and_tag_cleaning():
    with pytest.raises(ValueError):
        request(title="x" * 101)
    with pytest.raises(ValueError):
        request(title="bad <title>")
    with pytest.raises(ValueError):
        request(description="é" * 2600)
    assert request(tags=["#a", "a", " b "]).tags == ["a", "b"]


# --- auth -------------------------------------------------------------------


def test_auth_url_uses_upload_scope_and_pkce():
    verifier, challenge = auth.make_pkce()
    assert verifier != challenge
    url = auth.build_auth_url(
        client_id="cid", redirect_uri="http://127.0.0.1:1/", state="s", challenge=challenge
    )
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == ["https://www.googleapis.com/auth/youtube.upload"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"]


@respx.mock
def test_authorize_stores_token_with_private_permissions(tmp_path):
    token_route = respx.post(auth.TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "A", "refresh_token": "R", "expires_in": 3600}
        )
    )
    token_path = tmp_path / "youtube" / "token.json"

    def fake_browser(url: str) -> None:
        query = parse_qs(urlparse(url).query)
        redirect = query["redirect_uri"][0]
        state = query["state"][0]

        def hit() -> None:
            time.sleep(0.2)
            httpx.get(redirect, params={"code": "the-code", "state": state})

        threading.Thread(target=hit, daemon=True).start()

    with httpx.Client() as client:
        # Let loopback traffic through respx.
        respx.route(host="127.0.0.1").pass_through()
        auth.authorize(
            yt_settings(),
            token_path,
            http=client,
            open_url=fake_browser,
            announce=lambda _msg: None,
            timeout=10,
        )
    sent = token_route.calls.last.request.content.decode()
    assert "code=the-code" in sent and "code_verifier=" in sent
    saved = json.loads(token_path.read_text())
    assert saved["refresh_token"] == "R"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_authorize_rejects_wrong_state(tmp_path):
    def fake_browser(url: str) -> None:
        redirect = parse_qs(urlparse(url).query)["redirect_uri"][0]

        def hit() -> None:
            time.sleep(0.2)
            httpx.get(redirect, params={"code": "x", "state": "wrong"})

        threading.Thread(target=hit, daemon=True).start()

    with pytest.raises(AIError) as exc:
        auth.authorize(
            yt_settings(),
            tmp_path / "t.json",
            http=httpx.Client(),
            open_url=fake_browser,
            announce=lambda _msg: None,
            timeout=10,
        )
    assert exc.value.category == ErrorCategory.AUTHENTICATION_FAILED
    assert not (tmp_path / "t.json").exists()


@respx.mock
def test_access_token_refreshes_when_expired_and_keeps_refresh_token(tmp_path):
    path = tmp_path / "token.json"
    auth.save_token(
        path, {"access_token": "old", "refresh_token": "R", "expires_at": time.time() - 10}
    )
    respx.post(auth.TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
    )
    with httpx.Client() as client:
        assert auth.get_access_token(yt_settings(), path, http=client) == "new"
    assert json.loads(path.read_text())["refresh_token"] == "R"


def test_valid_token_is_reused_and_missing_token_errors(tmp_path):
    path = tmp_path / "token.json"
    with pytest.raises(AIError):
        auth.get_access_token(yt_settings(), path)
    auth.save_token(
        path, {"access_token": "tok", "refresh_token": "R", "expires_at": time.time() + 3000}
    )
    assert auth.get_access_token(yt_settings(), path) == "tok"


def test_missing_credentials_error_does_not_leak_secret():
    with pytest.raises(AIError) as exc:
        yt_settings(client_secret=None).credentials()
    assert exc.value.category == ErrorCategory.MISSING_API_KEY


# --- client -----------------------------------------------------------------


@respx.mock
def test_resumable_upload_sends_explicit_privacy_and_chunks(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x" * 600_000)
    session = "https://upload.example/session/1"
    start = respx.post(UPLOAD_URL).mock(
        return_value=httpx.Response(200, headers={"Location": session})
    )
    put = respx.put(session).mock(
        side_effect=[
            httpx.Response(308, headers={"Range": "bytes=0-262143"}),
            httpx.Response(308, headers={"Range": "bytes=0-524287"}),
            httpx.Response(200, json={"id": "vid123"}),
        ]
    )
    client = YouTubeClient(lambda: "tok", chunk_size=262_144)
    result = client.upload_video(video, request(publish_at=NOW + timedelta(days=3)))
    assert result["id"] == "vid123"
    body = json.loads(start.calls.last.request.content)
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["publishAt"].endswith("Z")
    assert start.calls.last.request.headers["Authorization"] == "Bearer tok"
    ranges = [c.request.headers["Content-Range"] for c in put.calls]
    assert ranges == [
        "bytes 0-262143/600000",
        "bytes 262144-524287/600000",
        "bytes 524288-599999/600000",
    ]


@respx.mock
def test_upload_maps_quota_and_auth_errors(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    respx.post(UPLOAD_URL).mock(
        return_value=httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})
    )
    with pytest.raises(AIError) as exc:
        YouTubeClient(lambda: "tok").upload_video(video, request())
    assert exc.value.category == ErrorCategory.RATE_LIMIT_EXCEEDED
    assert "tok" not in str(exc.value.message)

    respx.post(UPLOAD_URL).mock(return_value=httpx.Response(401, json={}))
    with pytest.raises(AIError) as exc:
        YouTubeClient(lambda: "tok").upload_video(video, request())
    assert exc.value.category == ErrorCategory.AUTHENTICATION_FAILED


@respx.mock
def test_chunk_server_error_resumes_from_reported_offset(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"y" * 10)
    session = "https://upload.example/session/2"
    respx.post(UPLOAD_URL).mock(return_value=httpx.Response(200, headers={"Location": session}))
    respx.put(session).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(308),  # status query: nothing received yet
            httpx.Response(200, json={"id": "v2"}),
        ]
    )
    assert YouTubeClient(lambda: "tok").upload_video(video, request())["id"] == "v2"


# --- service ----------------------------------------------------------------


class FakeClips:
    def __init__(self, tmp_path: Path, **overrides) -> None:
        self.clip = make_clip_job(
            title="Great moment",
            description="Body text",
            hashtags=["#fun", "clip"],
            **overrides,
        )
        self.file = tmp_path / "out.mp4"
        self.file.write_bytes(b"video")

    def get(self, clip_id):
        return self.clip

    def display_title(self, clip):
        return clip.display_title()

    def output_path(self, clip_id):
        return self.file


class FakeUploader:
    def __init__(self) -> None:
        self.calls: list[UploadRequest] = []

    def upload_video(self, path, req):
        self.calls.append(req)
        return {"id": "abc"}


def make_service(tmp_path, **clip_overrides):
    uploader = FakeUploader()
    service = YouTubeUploadService(
        yt_settings(),
        FakeClips(tmp_path, **clip_overrides),
        uploader,
        UploadStore(tmp_path / "youtube" / "uploads.json"),
    )
    return service, uploader


def test_service_builds_private_request_from_clip_metadata(tmp_path):
    service, _ = make_service(tmp_path)
    req = service.build_request("c1", now=NOW)
    assert req.privacy == Privacy.PRIVATE
    assert req.title == "Great moment"
    assert req.description.endswith("#fun #clip")
    assert req.tags == ["fun", "clip"]


def test_service_upload_records_and_refuses_repeat(tmp_path):
    service, uploader = make_service(tmp_path)
    req = service.build_request("c1", now=NOW)
    record = service.upload(req)
    assert record["video_id"] == "abc"
    assert record["privacy"] == "private"
    assert "token" not in json.dumps(record)
    with pytest.raises(AIError):
        service.upload(req)
    service.upload(req, force=True)
    assert len(uploader.calls) == 2


def test_service_does_not_touch_published_flag(tmp_path):
    service, _ = make_service(tmp_path)
    service.upload(service.build_request("c1", now=NOW))
    assert service.clips.clip.published is False


def test_service_blocks_trashed_clip_and_disabled_module(tmp_path):
    service, _ = make_service(tmp_path, deleted_at=NOW.isoformat())
    with pytest.raises(AIError):
        service.build_request("c1", now=NOW)
    service, _ = make_service(tmp_path)
    service.settings = yt_settings(enabled=False)
    with pytest.raises(AIError) as exc:
        service.upload(service.build_request("c1", now=NOW))
    assert exc.value.category == ErrorCategory.POLICY_VIOLATION


# --- cli --------------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    settings = Settings(config_dir=tmp_path / "c", data_dir=tmp_path / "d", _env_file=None)
    settings.ensure_dirs()
    clips = FakeClips(tmp_path)
    monkeypatch.setattr("clipping_ai.cli.get_settings", lambda: settings)
    monkeypatch.setattr("clipping_ai.cli._clip_service", lambda: clips)
    monkeypatch.setenv("CLIPPING_AI_YOUTUBE_ENABLED", "true")
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "csecret")
    monkeypatch.chdir(tmp_path)  # no stray .env
    return settings


def test_parser_accepts_youtube_commands():
    args = build_parser().parse_args(["youtube", "upload", "c1", "--dry-run"])
    assert args.command == "youtube" and args.youtube_command == "upload"


def test_cli_status_needs_no_network_and_hides_secrets(cli_env, capsys):
    assert main(["youtube", "status"]) == 0
    out = capsys.readouterr().out
    assert "enabled: True" in out and "authorised: False" in out
    assert "csecret" not in out


def test_cli_dry_run_prints_private_body_without_network(cli_env, capsys):
    with respx.mock:  # any real request would raise
        assert main(["youtube", "upload", "c1", "--dry-run"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["status"]["privacyStatus"] == "private"


def test_cli_rejects_public_without_confirmation_and_past_schedule(cli_env, capsys):
    assert main(["youtube", "upload", "c1", "--privacy", "public", "--dry-run"]) == 1
    past = "2020-01-01T00:00:00Z"
    assert main(["youtube", "upload", "c1", "--publish-at", past, "--dry-run"]) == 1
    assert (
        main(["youtube", "upload", "c1", "--publish-at", "2099-01-01T00:00:00", "--dry-run"]) == 1
    )


def test_cli_disabled_by_default(cli_env, monkeypatch, capsys):
    monkeypatch.setenv("CLIPPING_AI_YOUTUBE_ENABLED", "false")
    assert main(["youtube", "upload", "c1", "--dry-run"]) == 1
    assert "disabled" in capsys.readouterr().err
