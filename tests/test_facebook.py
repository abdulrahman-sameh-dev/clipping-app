"""Facebook Page publishing. No network: a fake client, plus respx for the real client."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from clipping_ai.api.app import create_app
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.facebook.client import FacebookClient
from clipping_ai.facebook.config import FacebookSettings
from tests.helpers import make_manager, make_registry
from tests.test_clips import make_clip_job

CLIP_ID = "a" * 32
SECRET = "app-secret-value"
USER_TOKEN = "user-token-value"
PAGE_TOKEN = "page-token-value"
PAGES = [
    {"id": "111", "name": "My Store", "access_token": PAGE_TOKEN, "tasks": ["CREATE_CONTENT"]},
    {"id": "222", "name": "Other", "access_token": "tok2", "tasks": ["ANALYZE"]},
]


def fb_settings(**overrides) -> FacebookSettings:
    values = {"app_id": "appid", "app_secret": SECRET, "_env_file": None}
    values.update(overrides)
    return FacebookSettings(**values)


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


class FakeFacebook:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.gate: threading.Event | None = None
        self.started = threading.Event()
        self.pages = PAGES

    def exchange_code(self, code: str, redirect_uri: str) -> str:
        if code == "bad":
            raise AIError(ErrorCategory.AUTHENTICATION_FAILED, "no", retryable=False)
        return USER_TOKEN

    def list_pages(self, user_token: str):
        assert user_token == USER_TOKEN
        return self.pages

    def publish_reel(self, **kwargs) -> dict[str, Any]:
        self.calls.append(kwargs)
        self.started.set()
        if self.gate is not None:
            self.gate.wait(5)
        return {"video_id": "reel1"}


@pytest.fixture
def env(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video")
    manager = make_manager(make_registry(providers={}, models=[], routing={}), {}, tmp_path)
    clips = FakeClips(video)
    fake = FakeFacebook()
    app = create_app(
        manager,
        clip_service=clips,
        start_worker=False,
        facebook_settings=fb_settings(),
        facebook_client=fake,
    )
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    return client, fake, clips, manager.settings.data_dir


def connect(client, pages_choice: str | None = None):
    started = client.post("/api/facebook/connect", json={}).json()
    state = parse_qs(urlparse(started["url"]).query)["state"][0]
    response = client.get(
        "/api/facebook/callback", params={"state": state, "code": "good"}, follow_redirects=False
    )
    if pages_choice:
        client.put("/api/facebook/page", json={"page_id": pages_choice})
    return response


def publish(client, body: dict[str, Any] | None = None, clip_id: str = CLIP_ID):
    payload = {"confirm": True}
    payload.update(body or {})
    return client.post(f"/api/facebook/clips/{clip_id}/publish", json=payload)


def future(minutes: int) -> str:
    when = datetime.now(UTC) + timedelta(minutes=minutes)
    return when.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"


def test_status_before_connect_hides_secrets(env):
    client, *_ = env
    data = client.get("/api/facebook/status").json()
    assert data["client_configured"] is True
    assert data["connected"] is False
    assert SECRET not in json.dumps(data)


def test_not_configured_is_reported(tmp_path):
    manager = make_manager(make_registry(providers={}, models=[], routing={}), {}, tmp_path)
    app = create_app(
        manager,
        start_worker=False,
        facebook_settings=fb_settings(app_id=None, app_secret=None),
    )
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    assert client.get("/api/facebook/status").json()["client_configured"] is False
    assert client.post("/api/facebook/connect", json={}).status_code >= 400


def test_connect_url_asks_for_the_publish_permissions(env):
    client, *_ = env
    out = client.post("/api/facebook/connect", json={}).json()
    query = parse_qs(urlparse(out["url"]).query)
    assert urlparse(out["url"]).netloc == "www.facebook.com"
    assert query["redirect_uri"] == ["http://127.0.0.1:8765/api/facebook/callback"]
    assert query["scope"] == [
        "pages_show_list,pages_manage_posts,pages_read_engagement,publish_video"
    ]
    assert SECRET not in out["url"]


def test_callback_saves_pages_and_never_returns_tokens(env):
    client, _, _, data_dir = env
    response = connect(client)
    assert response.status_code == 303
    assert "fb=ok" in response.headers["location"]
    status = client.get("/api/facebook/status").json()
    assert status["connected"] is True
    assert [p["id"] for p in status["pages"]] == ["111", "222"]
    assert status["pages"][1]["can_post"] is False
    assert status["selected_page"] is None  # two Pages: the user must choose
    text = json.dumps(status)
    assert PAGE_TOKEN not in text and USER_TOKEN not in text
    token_file = data_dir / "facebook" / "token.json"
    assert oct(token_file.stat().st_mode & 0o777) == "0o600"


def test_single_page_is_selected_automatically(env):
    client, fake, *_ = env
    fake.pages = PAGES[:1]
    connect(client)
    assert client.get("/api/facebook/status").json()["selected_page"]["id"] == "111"


def test_callback_rejects_unknown_state_and_reports_denial(env):
    client, *_ = env
    bad = client.get("/api/facebook/callback", params={"state": "nope", "code": "x"})
    assert bad.status_code >= 400
    started = client.post("/api/facebook/connect", json={}).json()
    state = parse_qs(urlparse(started["url"]).query)["state"][0]
    denied = client.get(
        "/api/facebook/callback",
        params={"state": state, "error": "access_denied"},
        follow_redirects=False,
    )
    assert "fb=denied" in denied.headers["location"]
    assert client.get("/api/facebook/status").json()["connected"] is False


def test_failed_code_exchange_saves_nothing(env):
    client, *_ = env
    started = client.post("/api/facebook/connect", json={}).json()
    state = parse_qs(urlparse(started["url"]).query)["state"][0]
    out = client.get(
        "/api/facebook/callback", params={"state": state, "code": "bad"}, follow_redirects=False
    )
    assert "fb=failed" in out.headers["location"]
    assert client.get("/api/facebook/status").json()["connected"] is False


def test_changing_state_refuses_other_websites(env):
    client, *_ = env
    response = client.post(
        "/api/facebook/connect", json={}, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code >= 400


def test_publish_needs_confirm_and_sends_nothing_without_it(env):
    client, fake, *_ = env
    connect(client, "111")
    response = client.post(f"/api/facebook/clips/{CLIP_ID}/publish", json={})
    assert response.status_code >= 400
    assert fake.calls == []


def test_publish_now_sends_to_the_selected_page_and_records_it(env):
    client, fake, *_ = env
    connect(client, "111")
    response = publish(client)
    assert response.status_code == 200
    call = fake.calls[0]
    assert call["page_id"] == "111" and call["page_token"] == PAGE_TOKEN
    assert call["publish_at"] is None
    assert call["description"] == "About\n\n#tag"
    post = response.json()["post"]
    assert post["video_id"] == "reel1" and post["page_name"] == "My Store"
    assert PAGE_TOKEN not in json.dumps(response.json())
    state = client.get(f"/api/facebook/clips/{CLIP_ID}").json()
    assert state["post"]["video_id"] == "reel1"


def test_same_clip_is_not_sent_twice(env):
    client, fake, *_ = env
    connect(client, "111")
    assert publish(client).status_code == 200
    assert publish(client).status_code >= 400
    assert len(fake.calls) == 1


def test_scheduled_publish_passes_the_time(env):
    client, fake, *_ = env
    connect(client, "111")
    response = publish(client, {"mode": "schedule", "publish_at": future(90)})
    assert response.status_code == 200
    assert fake.calls[0]["publish_at"] is not None
    assert response.json()["post"]["mode"] == "schedule"


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "schedule"},
        {"mode": "schedule", "publish_at": "2030-01-01T10:00:00"},  # no offset
        {"mode": "schedule", "publish_at": "soon"},
        {"mode": "schedule", "publish_at": "2020-01-01T10:00:00+00:00"},
        {"mode": "schedule", "publish_at": future(3)},  # under 10 minutes
        {"mode": "schedule", "publish_at": future(60 * 24 * 40)},  # over 29 days
        {"mode": "now", "publish_at": future(60)},
        {"mode": "later"},
    ],
)
def test_bad_schedules_are_rejected_before_any_send(env, body):
    client, fake, *_ = env
    connect(client, "111")
    assert publish(client, body).status_code >= 400
    assert fake.calls == []


def test_no_page_chosen_or_no_permission_blocks_publishing(env):
    client, fake, *_ = env
    connect(client)  # two Pages, none chosen
    assert publish(client).status_code >= 400
    client.put("/api/facebook/page", json={"page_id": "222"})  # no CREATE_CONTENT
    assert publish(client).status_code >= 400
    assert fake.calls == []


def test_unknown_page_cannot_be_selected(env):
    client, *_ = env
    connect(client)
    assert client.put("/api/facebook/page", json={"page_id": "999"}).status_code >= 400


def test_daily_limit_stops_the_31st_reel(env):
    client, fake, _, data_dir = env
    connect(client, "111")
    posts = {
        f"clip{i}": {"page_id": "111", "sent_at": datetime.now(UTC).isoformat()} for i in range(30)
    }
    path = data_dir / "facebook" / "posts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(posts), encoding="utf-8")
    response = publish(client)
    assert response.status_code == 429 or response.status_code >= 400
    assert fake.calls == []
    assert client.get("/api/facebook/status").json()["used_24h"] == 30


def test_old_posts_do_not_count_against_the_limit(env):
    client, fake, _, data_dir = env
    connect(client, "111")
    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    posts = {f"c{i}": {"page_id": "111", "sent_at": old} for i in range(30)}
    path = data_dir / "facebook" / "posts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(posts), encoding="utf-8")
    assert publish(client).status_code == 200


def test_trashed_clip_is_refused(env):
    client, fake, clips, _ = env
    connect(client, "111")
    clips.trashed.add(CLIP_ID)
    assert publish(client).status_code >= 400
    assert fake.calls == []


def test_parallel_publish_of_one_clip_sends_once(env):
    client, fake, *_ = env
    connect(client, "111")
    fake.gate = threading.Event()
    results: list[int] = []
    first = threading.Thread(target=lambda: results.append(publish(client).status_code))
    first.start()
    assert fake.started.wait(5)
    second = publish(client)
    fake.gate.set()
    first.join(5)
    assert second.status_code >= 400
    assert results == [200]
    assert len(fake.calls) == 1


def test_disconnect_deletes_the_saved_sign_in(env):
    client, _, _, data_dir = env
    connect(client, "111")
    out = client.post("/api/facebook/disconnect").json()
    assert out["removed"] is True and out["connected"] is False
    assert not (data_dir / "facebook" / "token.json").exists()


# --- the real client against a mocked Graph API -------------------------------------------


@respx.mock
def test_client_publishes_a_reel_in_three_steps(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"12345")
    start = respx.post("https://graph.facebook.com/v21.0/111/video_reels").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "video_id": "v9",
                    "upload_url": "https://rupload.facebook.com/video-upload/v21.0/v9",
                },
            ),
            httpx.Response(200, json={"success": True}),
        ]
    )
    upload = respx.post("https://rupload.facebook.com/video-upload/v21.0/v9").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    when = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    out = FacebookClient(fb_settings()).publish_reel(
        page_id="111", page_token=PAGE_TOKEN, path=video, description="hi", publish_at=when
    )
    assert out == {"video_id": "v9"}
    first = start.calls[0].request.content.decode()
    finish = start.calls[1].request.content.decode()
    assert "upload_phase=start" in first
    assert "upload_phase=finish" in finish and "video_state=SCHEDULED" in finish
    assert f"scheduled_publish_time={int(when.timestamp())}" in finish
    sent = upload.calls[0].request
    assert sent.headers["authorization"] == f"OAuth {PAGE_TOKEN}"
    assert sent.headers["file_size"] == "5" and sent.headers["offset"] == "0"
    assert sent.content == b"12345"


@respx.mock
def test_client_publish_now_uses_published_state(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"1")
    edge = respx.post("https://graph.facebook.com/v21.0/111/video_reels").mock(
        side_effect=[
            httpx.Response(200, json={"video_id": "v9"}),
            httpx.Response(200, json={"success": True}),
        ]
    )
    respx.post("https://rupload.facebook.com/video-upload/v21.0/v9").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    FacebookClient(fb_settings()).publish_reel(
        page_id="111", page_token=PAGE_TOKEN, path=video, description="", publish_at=None
    )
    finish = edge.calls[1].request.content.decode()
    assert "video_state=PUBLISHED" in finish and "scheduled_publish_time" not in finish


@respx.mock
def test_client_error_shows_metas_message_without_tokens(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"1")
    respx.post("https://graph.facebook.com/v21.0/111/video_reels").mock(
        return_value=httpx.Response(400, json={"error": {"message": "Bad thing", "code": 100}})
    )
    with pytest.raises(AIError) as caught:
        FacebookClient(fb_settings()).publish_reel(
            page_id="111", page_token=PAGE_TOKEN, path=video, description="", publish_at=None
        )
    assert "Bad thing" in str(caught.value)
    assert PAGE_TOKEN not in str(caught.value)


@respx.mock
def test_client_exchanges_code_for_a_long_lived_token():
    route = respx.get("https://graph.facebook.com/v21.0/oauth/access_token").mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "short"}),
            httpx.Response(200, json={"access_token": "long"}),
        ]
    )
    token = FacebookClient(fb_settings()).exchange_code("code1", "http://127.0.0.1:8765/cb")
    assert token == "long"
    assert "grant_type=fb_exchange_token" in str(route.calls[1].request.url)


@respx.mock
def test_client_lists_pages_with_tokens():
    respx.get("https://graph.facebook.com/v21.0/me/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "1", "name": "P", "access_token": "t", "tasks": ["CREATE_CONTENT"]},
                    {"id": "2"},
                ]
            },
        )
    )
    pages = FacebookClient(fb_settings()).list_pages("u")
    assert [p["id"] for p in pages] == ["1"]
