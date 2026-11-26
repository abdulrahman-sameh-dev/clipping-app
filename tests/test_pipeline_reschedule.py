"""Day page and analytics-based re-timing of scheduled videos. No network."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from clipping_ai.api.app import create_app
from clipping_ai.pipeline import google, reschedule
from clipping_ai.pipeline.store import PipelineStore
from clipping_ai.youtube.config import YouTubeSettings
from tests.helpers import make_manager, make_registry
from tests.test_pipeline_google import FakeClips, FakeUploader, FakeVideo

BASE = "http://127.0.0.1:8765"
ACCESS = "access-token-value"


def _local(days: int, hour: int, minute: int = 0) -> datetime:
    now = datetime.now().astimezone()
    return (now + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)


@pytest.fixture
def env(tmp_path: Path):
    state: dict[str, Any] = {
        "remote": [],  # videos on the channel: {"id", "title", "publishAt", "privacy"}
        "puts": [],
        "fail_put": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            return httpx.Response(200, json={"access_token": ACCESS, "expires_in": 3600})
        if "googleapis.com/youtube/v3" in url:
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
            if "/videos" in url and request.method == "PUT":
                if state["fail_put"]:
                    return httpx.Response(state["fail_put"], json={"error": {"status": "X"}})
                body = json.loads(request.content)
                state["puts"].append((str(request.url.params.get("part")), body))
                for v in state["remote"]:
                    if v["id"] == body["id"]:
                        v["publishAt"] = body["status"]["publishAt"]
                return httpx.Response(200, json={"id": body["id"]})
            if "/videos" in url:
                wanted = request.url.params.get("id", "").split(",")
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": v["id"],
                                "snippet": {"title": v["title"]},
                                "status": {
                                    "publishAt": v.get("publishAt"),
                                    "privacyStatus": v.get("privacy", "private"),
                                    "embeddable": True,
                                    "license": "youtube",
                                    "publicStatsViewable": True,
                                    "selfDeclaredMadeForKids": False,
                                    "uploadStatus": "processed",
                                },
                            }
                            for v in state["remote"]
                            if v["id"] in wanted
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

    def connect(*kinds: str) -> None:
        paths = {
            "analytics": data / "google" / "analytics_token.json",
            "manage": data / "google" / "manage_token.json",
            "upload": data / "youtube" / "token.json",
        }
        for kind in kinds:
            token(paths[kind], google.SCOPES[kind])

    return TestClient(app, base_url=BASE), state, clips, uploader, data, connect


def _put_remote(state, vid: str, when: datetime, privacy: str = "private") -> None:
    state["remote"].append(
        {"id": vid, "title": f"T {vid}", "publishAt": when.isoformat(), "privacy": privacy}
    )


def _seed_history(data: Path, clips, rows: list[tuple[int, int, int]]) -> None:
    """rows: (days_ago, hour, views). Records go to the upload store, numbers to the database."""
    records: dict[str, Any] = {}
    snaps = []
    ids = list(clips.clips)
    for i, (days_ago, hour, views) in enumerate(rows):
        clip_id = ids[i % len(ids)] if i < len(ids) else f"extra{i:027x}"
        vid = f"hist{i:07d}"
        records[clip_id] = {
            "video_id": vid,
            "privacy": "private",
            "publish_at": _local(-days_ago, hour).isoformat(),
            "uploaded_at": _local(-days_ago - 1, 9).isoformat(),
        }
        snaps.append(
            {
                "video_id": vid,
                "clip_id": clip_id,
                "views": views,
                "minutes_watched": 1.0,
                "avg_view_seconds": 5.0,
                "likes": 0,
                "comments": 0,
                "subscribers_gained": 0,
            }
        )
    path = data / "youtube" / "uploads.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records))
    PipelineStore(data / "pipeline.db").save_analytics(snaps)


# --- pure rules --------------------------------------------------------------------------------


def _points(spec: list[tuple[int, float]]) -> list[dict[str, Any]]:
    return [{"video_id": f"v{i}", "hour": h, "rate": r} for i, (h, r) in enumerate(spec)]


def test_few_videos_means_no_advice():
    ranking = reschedule.rank_hours(_points([(18, 10), (18, 12), (21, 30), (21, 28)]))
    assert ranking["confidence"] == "none" and ranking["reason"] == "not_enough_videos"


def test_one_hour_only_means_no_comparison():
    ranking = reschedule.rank_hours(_points([(18, r) for r in (5, 6, 7, 8, 9, 10)]))
    assert ranking["reason"] == "not_enough_hours"


def test_similar_hours_are_not_called_different():
    spec = [(18, 10), (18, 11), (18, 12), (21, 10), (21, 11), (21, 12)]
    ranking = reschedule.rank_hours(_points(spec))
    assert ranking["confidence"] == "none" and ranking["reason"] == "no_clear_difference"


def test_clear_winner_moves_only_the_time_of_day():
    spec = [(18, 5), (18, 6), (18, 7), (21, 20), (21, 22), (21, 24)]
    ranking = reschedule.rank_hours(_points(spec))
    assert ranking["confidence"] == "ok" and ranking["hours"][0]["hour"] == 21
    now = datetime.now().astimezone()
    old = _local(3, 18)
    keep = _local(4, 21)  # already at the best hour: untouched
    soon = (now + timedelta(minutes=10)).replace(second=0, microsecond=0)
    plan = reschedule.build_plan(
        now=now,
        upcoming=[
            {"video_id": "a" * 11, "title": "A", "publish_at": old.isoformat()},
            {"video_id": "b" * 11, "title": "B", "publish_at": keep.isoformat()},
            {"video_id": "c" * 11, "title": "C", "publish_at": soon.isoformat()},
        ],
        ranking=ranking,
    )
    assert [i["video_id"] for i in plan["items"]] == ["a" * 11]
    new = datetime.fromisoformat(plan["items"][0]["new_publish_at"])
    assert new.date() == old.date() and (new.hour, new.minute) == (21, 0)


def test_two_videos_on_one_day_use_the_two_best_hours():
    spec = [(21, 20), (21, 22), (21, 24), (18, 12), (18, 13), (18, 14), (9, 1), (9, 2)]
    ranking = reschedule.rank_hours(_points(spec))
    now = datetime.now().astimezone()
    day = _local(5, 7)
    plan = reschedule.build_plan(
        now=now,
        upcoming=[
            {"video_id": "a" * 11, "title": "A", "publish_at": day.isoformat()},
            {
                "video_id": "b" * 11,
                "title": "B",
                "publish_at": (day + timedelta(hours=1)).isoformat(),
            },
        ],
        ranking=ranking,
    )
    hours = [datetime.fromisoformat(i["new_publish_at"]).hour for i in plan["items"]]
    assert hours == [21, 18]


# --- day page ----------------------------------------------------------------------------------


def test_day_page_lists_the_days_videos_and_ready_clips(env):
    c, state, clips, _, data, connect = env
    connect("analytics")
    _put_remote(state, "vidAAAAAAAA", _local(3, 18))
    _put_remote(state, "vidBBBBBBBB", _local(4, 18))
    day = _local(3, 18).date().isoformat()
    out = c.get("/api/pipeline/day", params={"date": day}).json()
    assert [v["video_id"] for v in out["videos"]] == ["vidAAAAAAAA"]
    assert out["videos"][0]["source"] == "youtube" and out["manage_connected"] is False
    assert len(out["ready_clips"]) == len(clips.clips)
    empty = c.get("/api/pipeline/day", params={"date": _local(9, 8).date().isoformat()}).json()
    assert empty["videos"] == []
    assert c.get("/api/pipeline/day", params={"date": "nope"}).status_code == 400
    assert c.get("/api/pipeline/day", params={"date": "2020-01-01"}).status_code == 400


def test_moving_a_video_needs_confirm_and_the_extra_sign_in(env):
    c, state, _, _, data, connect = env
    connect("analytics")
    _put_remote(state, "vidAAAAAAAA", _local(3, 18))
    when = _local(3, 20).isoformat()
    path = "/api/pipeline/day/video/vidAAAAAAAA/time"
    assert c.put(path, json={"publish_at": when}).status_code == 400  # no confirm
    denied = c.put(path, json={"publish_at": when, "confirm": True})
    assert denied.status_code == 400 and "Edit scheduled times" in denied.text
    assert state["puts"] == []


def test_move_changes_only_the_time_and_keeps_other_status_fields(env):
    c, state, _, _, data, connect = env
    connect("analytics", "manage")
    _put_remote(state, "vidAAAAAAAA", _local(3, 18))
    target = _local(5, 20)
    r = c.put(
        "/api/pipeline/day/video/vidAAAAAAAA/time",
        json={"publish_at": target.isoformat(), "confirm": True},
    )
    assert r.status_code == 200, r.text
    part, body = state["puts"][0]
    assert part == "status" and body["id"] == "vidAAAAAAAA"
    status = body["status"]
    assert status["privacyStatus"] == "private"
    assert status["publishAt"] == target.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert status["embeddable"] is True and status["selfDeclaredMadeForKids"] is False
    assert "uploadStatus" not in status
    moved = c.get("/api/pipeline/day", params={"date": target.date().isoformat()}).json()
    assert [v["video_id"] for v in moved["videos"]] == ["vidAAAAAAAA"]


def test_move_refuses_other_videos_past_times_and_public_videos(env):
    c, state, _, _, data, connect = env
    connect("analytics", "manage")
    _put_remote(state, "vidAAAAAAAA", _local(3, 18))
    path = "/api/pipeline/day/video/{}/time"
    ok_time = _local(5, 20).isoformat()
    unknown = c.put(path.format("vidZZZZZZZZ"), json={"publish_at": ok_time, "confirm": True})
    assert unknown.status_code == 404
    soon = (datetime.now().astimezone() + timedelta(minutes=5)).isoformat()
    assert (
        c.put(path.format("vidAAAAAAAA"), json={"publish_at": soon, "confirm": True}).status_code
        == 400
    )
    naive = _local(5, 20).replace(tzinfo=None).isoformat()
    assert (
        c.put(path.format("vidAAAAAAAA"), json={"publish_at": naive, "confirm": True}).status_code
        == 400
    )
    state["remote"][0]["privacy"] = "public"  # went public in the meantime
    gone = c.put(path.format("vidAAAAAAAA"), json={"publish_at": ok_time, "confirm": True})
    assert gone.status_code == 400 and state["puts"] == []


def test_state_changing_day_and_plan_routes_refuse_other_sites(env):
    c, *_ = env
    evil = {"Origin": "https://evil.example"}
    body = {"publish_at": _local(5, 20).isoformat(), "confirm": True}
    assert (
        c.put("/api/pipeline/day/video/vidAAAAAAAA/time", json=body, headers=evil).status_code
        == 403
    )
    assert (
        c.post("/api/pipeline/day/add", json={**body, "clip_id": "x"}, headers=evil).status_code
        == 403
    )
    apply_body = {
        "items": [{"video_id": "vidAAAAAAAA", "new_publish_at": body["publish_at"]}],
        "confirm": True,
    }
    assert (
        c.post("/api/pipeline/reschedule/apply", json=apply_body, headers=evil).status_code == 403
    )
    rebinding = TestClient(c.app, base_url="http://evil.example:8765")
    assert rebinding.post("/api/pipeline/reschedule/apply", json=apply_body).status_code == 403


def test_add_a_ready_clip_to_a_day_uploads_private_at_the_chosen_time(env):
    c, state, clips, uploader, data, connect = env
    connect("analytics", "upload")
    clip_id = next(iter(clips.clips))
    when = _local(6, 19, 30)
    body = {"clip_id": clip_id, "publish_at": when.isoformat()}
    assert c.post("/api/pipeline/day/add", json=body).status_code == 400  # no confirm
    assert uploader.requests == []
    out = c.post("/api/pipeline/day/add", json={**body, "confirm": True})
    assert out.status_code == 200, out.text
    sent = uploader.requests[0]
    assert sent.privacy.value == "private" and sent.publish_at == when
    again = c.post("/api/pipeline/day/add", json={**body, "confirm": True})
    assert again.status_code == 400 and len(uploader.requests) == 1
    bad = c.post("/api/pipeline/day/add", json={**body, "clip_id": "nope", "confirm": True})
    assert bad.status_code == 400


# --- reschedule by analytics -------------------------------------------------------------------

GOOD = [(14, 18, 50), (13, 18, 60), (12, 18, 55), (10, 21, 300), (9, 21, 280), (8, 21, 320)]


def test_thin_data_is_reported_and_nothing_can_be_applied(env):
    c, state, clips, _, data, connect = env
    connect("analytics", "manage")
    _seed_history(data, clips, [(10, 18, 40), (9, 21, 90)])
    _put_remote(state, "vidAAAAAAAA", _local(3, 18))
    plan = c.get("/api/pipeline/reschedule/plan").json()
    assert plan["confidence"] == "none" and plan["reason"] == "not_enough_videos"
    assert plan["items"] == [] and plan["samples"] == 2
    forced = c.post(
        "/api/pipeline/reschedule/apply",
        json={
            "items": [{"video_id": "vidAAAAAAAA", "new_publish_at": _local(3, 21).isoformat()}],
            "confirm": True,
        },
    ).json()
    assert forced["results"][0]["reason"] == "plan_changed" and state["puts"] == []


def test_plan_then_confirm_changes_the_times_and_nothing_else(env):
    c, state, clips, _, data, connect = env
    connect("analytics", "manage")
    _seed_history(data, clips, GOOD)
    _put_remote(state, "vidAAAAAAAA", _local(3, 18))
    _put_remote(state, "vidBBBBBBBB", _local(4, 21))
    plan = c.get("/api/pipeline/reschedule/plan").json()
    assert plan["confidence"] == "ok" and plan["hours"][0]["hour"] == 21
    assert [i["video_id"] for i in plan["items"]] == ["vidAAAAAAAA"]
    assert state["puts"] == []  # a plan changes nothing on YouTube
    item = plan["items"][0]
    assert datetime.fromisoformat(item["old_publish_at"]).hour == 18
    assert datetime.fromisoformat(item["new_publish_at"]).hour == 21
    no_confirm = c.post(
        "/api/pipeline/reschedule/apply",
        json={"items": [{"video_id": item["video_id"], "new_publish_at": item["new_publish_at"]}]},
    )
    assert no_confirm.status_code == 400 and state["puts"] == []
    done = c.post(
        "/api/pipeline/reschedule/apply",
        json={
            "items": [{"video_id": item["video_id"], "new_publish_at": item["new_publish_at"]}],
            "confirm": True,
        },
    ).json()
    assert done["results"][0]["status"] == "changed"
    assert len(state["puts"]) == 1
    assert c.get("/api/pipeline/reschedule/plan").json()["items"] == []


def test_apply_ignores_times_that_are_not_in_the_current_plan(env):
    c, state, clips, _, data, connect = env
    connect("analytics", "manage")
    _seed_history(data, clips, GOOD)
    _put_remote(state, "vidAAAAAAAA", _local(3, 18))
    out = c.post(
        "/api/pipeline/reschedule/apply",
        json={
            "items": [{"video_id": "vidAAAAAAAA", "new_publish_at": _local(3, 4).isoformat()}],
            "confirm": True,
        },
    ).json()
    assert out["results"][0]["reason"] == "plan_changed" and state["puts"] == []


def test_apply_stops_after_a_permission_error(env):
    c, state, clips, _, data, connect = env
    connect("analytics", "manage")
    _seed_history(data, clips, GOOD)
    _put_remote(state, "vidAAAAAAAA", _local(3, 18))
    _put_remote(state, "vidBBBBBBBB", _local(4, 18))
    plan = c.get("/api/pipeline/reschedule/plan").json()
    state["fail_put"] = 403
    out = c.post(
        "/api/pipeline/reschedule/apply",
        json={
            "items": [
                {"video_id": i["video_id"], "new_publish_at": i["new_publish_at"]}
                for i in plan["items"]
            ],
            "confirm": True,
        },
    ).json()
    assert [r["status"] for r in out["results"]] == ["failed", "skipped"]


def test_manage_sign_in_asks_for_the_edit_scope_only_when_chosen(env):
    c, *_ = env
    started = c.post("/api/google/connect", json={"kind": "manage"}).json()
    assert google.MANAGE_SCOPE in started["url"].replace("%3A", ":").replace("%2F", "/")
    for kind in ("sheets", "analytics", "upload"):
        url = c.post("/api/google/connect", json={"kind": kind}).json()["url"]
        assert "force-ssl" not in url
    status = c.get("/api/google/status").json()
    assert status["kinds"]["manage"]["connected"] is False
