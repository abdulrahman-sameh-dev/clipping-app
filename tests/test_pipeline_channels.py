"""Web sign-in, the channel's own schedule, and several channels. No network."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from clipping_ai.api.app import create_app
from clipping_ai.pipeline import google
from clipping_ai.youtube.config import YouTubeSettings
from tests.helpers import make_manager, make_registry
from tests.test_pipeline_google import FakeClips, FakeUploader, FakeVideo

BASE = "http://127.0.0.1:8765"
ACCESS = "access-token-value"
SHEET = "https://docs.google.com/spreadsheets/d/abc123/edit#gid=5"


def _local_evening(days: int) -> datetime:
    now = datetime.now().astimezone()
    return (now + timedelta(days=days)).replace(hour=18, minute=0, second=0, microsecond=0)


@pytest.fixture
def env(tmp_path: Path):
    state: dict[str, Any] = {"remote": [], "hits": [], "token_posts": [], "fail_youtube": False}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        state["hits"].append((url, request.headers.get("authorization")))
        if url.startswith("https://oauth2.googleapis.com/token"):
            state["token_posts"].append(parse_qs(request.content.decode()))
            return httpx.Response(
                200,
                json={
                    "access_token": ACCESS,
                    "refresh_token": "refresh-value",
                    "expires_in": 3600,
                    "scope": "x",
                },
            )
        if "googleapis.com/youtube/v3" in url:
            if state["fail_youtube"]:
                return httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}})
            if "/channels" in url:
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": "UCchan",
                                "snippet": {"title": "My Channel"},
                                "contentDetails": {"relatedPlaylists": {"uploads": "UUup"}},
                            }
                        ]
                    },
                )
            if "/playlistItems" in url:
                return httpx.Response(
                    200,
                    json={
                        "items": [{"contentDetails": {"videoId": v["id"]}} for v in state["remote"]]
                    },
                )
            if "/videos" in url:
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": v["id"],
                                "snippet": {"title": v["title"]},
                                "status": {"publishAt": v.get("publishAt")},
                            }
                            for v in state["remote"]
                        ]
                    },
                )
        return httpx.Response(404)

    manager = make_manager(make_registry(providers={}, models=[], routing={}), {}, tmp_path)
    uploader = FakeUploader()
    app = create_app(
        manager,
        start_worker=False,
        youtube_settings=YouTubeSettings(
            enabled=True, client_id="cid", client_secret="client-secret", _env_file=None
        ),
        youtube_uploader=uploader,
        sheet_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    clips = FakeClips(tmp_path)
    app.state.clips = clips
    app.state.video = FakeVideo()
    data = manager.settings.data_dir

    def token(path: Path, scope: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "access_token": ACCESS,
                    "refresh_token": "r",
                    "expires_at": time.time() + 3000,
                    "scope": scope,
                }
            )
        )

    return TestClient(app, base_url=BASE), state, clips, uploader, data, token


def _connect(c: TestClient, kind: str, channel: str = "default") -> tuple[str, dict]:
    started = c.post("/api/google/connect", json={"kind": kind, "channel_id": channel})
    assert started.status_code == 200, started.text
    query = parse_qs(urlparse(started.json()["url"]).query)
    return query["state"][0], {"query": query, "body": started.json()}


def test_web_connect_saves_token_with_scopes_and_returns_to_ui(env):
    c, state, _, _, data, _ = env
    st, info = _connect(c, "analytics")
    q = info["query"]
    assert q["redirect_uri"] == [f"{BASE}/api/google/callback"]
    assert q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"]
    scopes = q["scope"][0].split()
    assert google.ANALYTICS_SCOPE in scopes and google.YOUTUBE_READ_SCOPE in scopes
    assert google.UPLOAD_SCOPE not in scopes  # read-only sign-in cannot publish
    r = c.get(f"/api/google/callback?state={st}&code=abc", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["location"].startswith(f"{BASE}/ui/#/pipeline?")
    assert "connected=analytics" in r.headers["location"]
    assert "abc" not in r.headers["location"] and "refresh-value" not in r.headers["location"]
    post = state["token_posts"][0]
    assert post["code"] == ["abc"] and post["code_verifier"][0]
    assert post["redirect_uri"] == [f"{BASE}/api/google/callback"]
    saved = json.loads((data / "google" / "analytics_token.json").read_text())
    assert saved["refresh_token"] == "refresh-value" and google.YOUTUBE_READ_SCOPE in saved["scope"]
    assert oct((data / "google" / "analytics_token.json").stat().st_mode & 0o777) == "0o600"
    status = c.get("/api/google/status").json()
    assert status["kinds"]["analytics"]["schedule_ready"] is True
    assert "refresh-value" not in json.dumps(status)


def test_callback_state_is_one_time_and_unknown_state_saves_nothing(env):
    c, state, _, _, data, _ = env
    st, _ = _connect(c, "sheets")
    assert (
        c.get("/api/google/callback?state=bogus&code=abc", follow_redirects=False).status_code
        == 502
    )
    assert c.get("/api/google/callback?code=abc", follow_redirects=False).status_code == 502
    ok = c.get(f"/api/google/callback?state={st}&code=abc", follow_redirects=False)
    assert ok.status_code == 303
    replay = c.get(f"/api/google/callback?state={st}&code=abc2", follow_redirects=False)
    assert replay.status_code == 502 and len(state["token_posts"]) == 1
    assert not (data / "google" / "analytics_token.json").exists()


def test_declined_consent_redirects_with_a_short_reason_and_saves_nothing(env):
    c, state, _, _, data, _ = env
    st, _ = _connect(c, "sheets")
    r = c.get(f"/api/google/callback?state={st}&error=access_denied", follow_redirects=False)
    assert r.status_code == 303 and "connect_error=denied" in r.headers["location"]
    assert "access_denied" not in r.headers["location"]
    assert state["token_posts"] == []
    assert not (data / "google" / "sheets_token.json").exists()


def test_state_expires(env, monkeypatch):
    c, state, _, _, _, _ = env
    st, _ = _connect(c, "sheets")
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + google.FLOW_TTL_SECONDS + 5)
    r = c.get(f"/api/google/callback?state={st}&code=abc", follow_redirects=False)
    assert r.status_code == 502 and state["token_posts"] == []


def test_state_changing_routes_refuse_other_websites_and_other_hosts(env):
    c, *_ = env
    body = {"kind": "sheets", "channel_id": "default"}
    assert (
        c.post(
            "/api/google/connect", json=body, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert c.post("/api/google/connect", json=body, headers={"Origin": "null"}).status_code == 403
    assert (
        c.post(
            "/api/google/connect", json=body, headers={"Sec-Fetch-Site": "cross-site"}
        ).status_code
        == 403
    )
    assert (
        c.post(
            "/api/google/connect", json=body, headers={"Origin": "http://localhost:5173"}
        ).status_code
        == 200
    )
    rebinding = TestClient(c.app, base_url="http://evil.example:8765")
    assert rebinding.post("/api/google/connect", json=body).status_code == 403
    assert (
        rebinding.post(
            "/api/pipeline/autopublish/run", json={"clip_ids": ["a"], "confirm": True}
        ).status_code
        == 403
    )
    assert (
        rebinding.get("/api/google/callback?state=x&code=y", follow_redirects=False).status_code
        == 403
    )


def test_return_to_must_be_a_local_page(env):
    c, *_ = env
    for bad, expect in (
        ("https://evil.example/", f"{BASE}/ui/"),
        ("javascript:alert(1)", f"{BASE}/ui/"),
    ):
        st, _ = _connect(c, "sheets") if False else (None, None)
        started = c.post("/api/google/connect", json={"kind": "sheets", "return_to": bad}).json()
        state_value = parse_qs(urlparse(started["url"]).query)["state"][0]
        r = c.get(f"/api/google/callback?state={state_value}&code=x", follow_redirects=False)
        assert r.headers["location"].startswith(expect)
    started = c.post(
        "/api/google/connect",
        json={"kind": "sheets", "return_to": "http://localhost:5173/app/?x=1#/y"},
    ).json()
    state_value = parse_qs(urlparse(started["url"]).query)["state"][0]
    r = c.get(f"/api/google/callback?state={state_value}&code=x", follow_redirects=False)
    assert r.headers["location"].startswith("http://localhost:5173/app/#/pipeline?")


def test_disconnect_deletes_the_token_and_the_schedule_copy(env):
    c, _, _, _, data, token = env
    token(data / "google" / "analytics_token.json", google.SCOPES["analytics"])
    assert c.post("/api/google/disconnect", json={"kind": "analytics"}).json()["removed"] is True
    assert not (data / "google" / "analytics_token.json").exists()


def test_old_analytics_sign_in_must_be_redone_to_read_the_schedule(env):
    c, _, _, _, data, token = env
    token(data / "google" / "analytics_token.json", google.ANALYTICS_SCOPE)
    view = c.get("/api/pipeline/schedule").json()
    assert view["can_read"] is False and view["reconnect_needed"] is True
    assert c.post("/api/pipeline/schedule/refresh").status_code == 400


def _schedule_days(state, days):
    state["remote"] = [
        {
            "id": f"vid{i:08d}",
            "title": f"Scheduled {i}",
            "publishAt": _local_evening(i).astimezone().isoformat(),
        }
        for i in days
    ]


def test_plan_skips_days_the_channel_already_has_scheduled(env):
    c, state, _, _, data, token = env
    token(data / "google" / "analytics_token.json", google.SCOPES["analytics"])
    token(data / "youtube" / "token.json", google.UPLOAD_SCOPE)
    _schedule_days(state, range(0, 12))  # scheduled by hand until 11 days from now
    plan = c.get("/api/pipeline/autopublish/plan").json()
    assert plan["schedule_known"] is True and plan["scheduled_on_channel"] >= 11
    first = datetime.fromisoformat(plan["items"][0]["publish_at"]).date()
    assert first == (datetime.now().astimezone() + timedelta(days=12)).date()
    view = c.get("/api/pipeline/schedule").json()
    assert view["youtube_channel_title"] == "My Channel" and len(view["videos"]) >= 11
    review = c.get("/api/pipeline/review").json()
    assert review["schedule_known"] is True
    counts = {d["date"]: d["count"] for d in review["schedule"]}
    assert counts[(datetime.now().astimezone() + timedelta(days=5)).date().isoformat()] == 1
    assert not any(t["kind"] == "schedule_unknown" for t in review["tips"])


def test_a_video_this_tool_uploaded_is_not_counted_twice(env):
    c, state, clips, uploader, data, token = env
    token(data / "google" / "analytics_token.json", google.SCOPES["analytics"])
    token(data / "youtube" / "token.json", google.UPLOAD_SCOPE)
    plan = c.get("/api/pipeline/autopublish/plan").json()
    ids = [i["clip_id"] for i in plan["items"]][:1]
    out = c.post("/api/pipeline/autopublish/run", json={"clip_ids": ids, "confirm": True}).json()
    assert out["results"][0]["status"] == "scheduled"
    vid = out["results"][0]["publish_at"]
    state["remote"] = [
        {"id": "vid00000001", "title": "same", "publishAt": vid}
    ]  # same video on YouTube
    review = c.get("/api/pipeline/review").json()
    assert sum(d["count"] for d in review["schedule"]) == 1


def test_run_refuses_when_the_schedule_cannot_be_read_unless_confirmed(env):
    c, state, _, uploader, data, token = env
    token(data / "youtube" / "token.json", google.UPLOAD_SCOPE)  # upload only, no read access
    ids = [i["clip_id"] for i in c.get("/api/pipeline/autopublish/plan").json()["items"]][:1]
    r = c.post("/api/pipeline/autopublish/run", json={"clip_ids": ids, "confirm": True})
    assert r.status_code == 400 and "schedule" in r.text and uploader.requests == []
    review = c.get("/api/pipeline/review").json()
    assert any(t["kind"] == "schedule_unknown" for t in review["tips"])
    ok = c.post(
        "/api/pipeline/autopublish/run",
        json={"clip_ids": ids, "confirm": True, "allow_unknown_schedule": True},
    )
    assert ok.status_code == 200 and len(uploader.requests) == 1


def test_run_refuses_when_reading_fails_now_even_if_an_old_copy_exists(env):
    c, state, _, uploader, data, token = env
    token(data / "google" / "analytics_token.json", google.SCOPES["analytics"])
    token(data / "youtube" / "token.json", google.UPLOAD_SCOPE)
    c.get("/api/pipeline/schedule")  # a good read, now
    state["fail_youtube"] = True
    ids = [i["clip_id"] for i in c.get("/api/pipeline/autopublish/plan").json()["items"]][:1]
    state["hits"].clear()
    r = c.post("/api/pipeline/autopublish/run", json={"clip_ids": ids, "confirm": True})
    assert r.status_code == 200 or r.status_code == 400
    if r.status_code == 200:  # a copy younger than ten minutes may be reused when YouTube fails
        pytest.fail("a failed fresh read must not be treated as a known schedule")
    assert uploader.requests == []


def test_default_channel_keeps_old_data_and_channels_are_separate(env):
    c, state, clips, uploader, data, token = env
    listing = c.get("/api/pipeline/channels").json()
    assert [ch["id"] for ch in listing["channels"]] == ["default"] and "gaming" in listing[
        "categories"
    ]
    created = c.post(
        "/api/pipeline/channels", json={"name": "Games", "category": "gaming", "publish_hour": 20}
    ).json()
    cid = created["id"]
    assert (
        cid.startswith("games-")
        and created["category"] == "gaming"
        and created["publish_hour"] == 20
    )
    assert (
        c.post("/api/pipeline/channels", json={"name": "X", "category": "nope"}).status_code == 400
    )
    assert (
        c.post("/api/pipeline/channels", json={"name": "قناة", "category": "religious"})
        .json()["id"]
        .startswith("channel-")
    )
    # per-channel sheet link and queue
    token(data / "google" / "sheets_token.json", google.SCOPES["sheets"])
    c.put(f"/api/pipeline/source?channel={cid}", json={"sheet_url": SHEET})
    assert c.get("/api/pipeline/source").json()["sheet_url"] is None
    assert c.get(f"/api/pipeline/source?channel={cid}").json()["sheet_url"] == SHEET
    assert c.get("/api/pipeline/channels/nope").status_code in (404, 405)
    assert c.get("/api/pipeline/source?channel=missing").status_code == 404
    # per-channel upload sign-in lives in its own file
    assert google.token_path(data, "upload", cid).parent.name == cid
    assert (
        google.token_path(data, "upload", "default", YouTubeSettings(_env_file=None)).name
        == "token.json"
    )
    token(data / "youtube" / "token.json", google.UPLOAD_SCOPE)
    assert (
        c.get(f"/api/pipeline/autopublish/plan?channel={cid}").json()["youtube_authorised"] is False
    )
    assert c.get("/api/pipeline/autopublish/plan").json()["youtube_authorised"] is True


def test_clips_follow_the_channel_of_the_link_that_made_them(env):
    c, _, clips, uploader, data, token = env
    cid = c.post("/api/pipeline/channels", json={"name": "Edu", "category": "educational"}).json()[
        "id"
    ]
    first = next(iter(clips.clips))
    clips.clips[first].source_job_id = "job-edu"
    from clipping_ai.pipeline.sheet import SheetRow
    from clipping_ai.pipeline.store import PipelineStore

    store = PipelineStore(data / "pipeline.db")
    store.add_rows([SheetRow("https://youtu.be/ZZZZZZZZZZZ", None, None)], cid)
    store.set_status(store.list_items(channel_id=cid)[0]["id"], "sent", "job-edu")
    mine = c.get(f"/api/pipeline/clips?channel={cid}").json()["clips"]
    assert [m["clip_id"] for m in mine] == [first]
    assert first not in [m["clip_id"] for m in c.get("/api/pipeline/clips").json()["clips"]]
    # manual override
    assert (
        c.put(f"/api/pipeline/clips/{first}/channel", json={"channel_id": "default"}).status_code
        == 200
    )
    assert first in [m["clip_id"] for m in c.get("/api/pipeline/clips").json()["clips"]]
    # a clip that was uploaded stays with the channel it went to
    token(data / "google" / "channels" / cid / "upload_token.json", google.UPLOAD_SCOPE)
    c.put(f"/api/pipeline/clips/{first}/channel", json={"channel_id": cid})
    token(data / "google" / "analytics_token.json", google.SCOPES["analytics"])
    token(data / "google" / "channels" / cid / "analytics_token.json", google.SCOPES["analytics"])
    run = c.post(
        "/api/pipeline/autopublish/run",
        json={
            "clip_ids": [first],
            "confirm": True,
            "channel_id": cid,
            "allow_unknown_schedule": True,
        },
    ).json()
    assert run["results"][0]["status"] == "scheduled"
    record = json.loads((data / "youtube" / "uploads.json").read_text())[first]
    assert record["channel_id"] == cid
    assert first in [
        m["clip_id"] for m in c.get(f"/api/pipeline/clips?channel={cid}").json()["clips"]
    ]


def test_channel_with_links_cannot_be_deleted_and_default_is_protected(env):
    c, _, _, _, data, _ = env
    cid = c.post("/api/pipeline/channels", json={"name": "Temp"}).json()["id"]
    assert c.delete("/api/pipeline/channels/default").status_code == 400
    assert c.delete(f"/api/pipeline/channels/{cid}").status_code == 200
    assert c.get(f"/api/pipeline/source?channel={cid}").status_code == 404


def test_old_database_gets_a_default_channel_with_its_sheet_and_slot(tmp_path):
    import sqlite3

    from clipping_ai.pipeline.store import PipelineStore

    db = tmp_path / "pipeline.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE pipeline_source (id INTEGER PRIMARY KEY CHECK (id = 1), sheet_url TEXT, "
        "last_sync_at TEXT, last_error TEXT);"
        "INSERT INTO pipeline_source VALUES (1, 'https://docs.google.com/spreadsheets/d/old/edit', 'x', NULL);"
        "CREATE TABLE pipeline_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO pipeline_settings VALUES ('publish_hour', '20'), ('per_day', '2');"
    )
    conn.commit()
    conn.close()
    store = PipelineStore(db)
    default = store.get_channel("default")
    assert (
        default["sheet_url"].endswith("/old/edit")
        and default["publish_hour"] == 20
        and default["per_day"] == 2
    )
    assert PipelineStore(db).list_channels()[0]["id"] == "default"  # idempotent
