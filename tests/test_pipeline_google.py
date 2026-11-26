"""Private sheet, duplicates, clip states, auto-publish plan and analytics. No network."""

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
from clipping_ai.clips.models import ClipStatus
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.pipeline.insights import build_insights
from clipping_ai.pipeline.plan import plan_slots, publish_state
from clipping_ai.pipeline.sheet import canonical_key
from clipping_ai.youtube.config import YouTubeSettings
from tests.helpers import make_manager, make_registry
from tests.test_clips import make_clip_job

SHEET = "https://docs.google.com/spreadsheets/d/abc123/edit#gid=5"
SECRET = "client-secret-value"
ACCESS = "access-token-value"


def test_canonical_key_matches_other_spellings_of_one_video():
    same = [
        "https://youtu.be/dQw4w9WgXcQ?si=abc",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&utm_source=x",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ&t=30",
        "https://youtube.com/shorts/dQw4w9WgXcQ",
    ]
    assert {canonical_key(u) for u in same} == {"youtube:dQw4w9WgXcQ"}
    assert canonical_key("https://vimeo.com/1/?utm_x=1#a") == canonical_key("https://vimeo.com/1")
    assert canonical_key("https://vimeo.com/1") != canonical_key("https://vimeo.com/2")


class FakeClips:
    def __init__(self, tmp: Path) -> None:
        video = tmp / "clip.mp4"
        video.write_bytes(b"fake video")
        self.video = video
        self.clips = {}
        for i in range(7):
            cid = f"{i:032x}"
            self.clips[cid] = make_clip_job(
                cid,
                status=ClipStatus.READY,
                title=f"Clip {i}",
                created_at=f"2026-11-0{i + 1}T10:00:00+00:00",
            )

    def get(self, clip_id: str):
        if clip_id not in self.clips:
            raise AIError(ErrorCategory.NOT_FOUND, "Clip not found.", retryable=False)
        return self.clips[clip_id]

    def list(self, **kwargs):
        return list(self.clips.values())

    def display_title(self, clip) -> str:
        return clip.title or "untitled"

    def output_path(self, clip_id: str) -> Path:
        return self.video


class FakeUploader:
    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.fail_at: int | None = None

    def upload_video(self, path, request):
        if self.fail_at is not None and len(self.requests) == self.fail_at:
            raise AIError(
                ErrorCategory.RATE_LIMIT_EXCEEDED,
                "quota used up",
                provider="youtube",
                retryable=False,
            )
        self.requests.append(request)
        return {"id": f"vid{len(self.requests):08d}"}


class FakeVideo:
    def __init__(self) -> None:
        self.duplicate = False

    def create_url(self, url: str):
        class Job:
            id = "job1"

        return Job(), self.duplicate


@pytest.fixture
def env(tmp_path):
    state: dict[str, Any] = {"hits": [], "analytics": None}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        state["hits"].append((url, request.headers.get("authorization")))
        if "sheets.googleapis.com" in url and "/values/" in url:
            return httpx.Response(
                200,
                json={
                    "values": [
                        ["Name", "Video link"],
                        ["a", "https://youtu.be/AAAAAAAAAAA"],
                        ["b", "https://www.youtube.com/watch?v=AAAAAAAAAAA&utm_source=x"],
                        ["c", "https://youtu.be/BBBBBBBBBBB"],
                    ]
                },
            )
        if "sheets.googleapis.com" in url:
            return httpx.Response(
                200,
                json={
                    "sheets": [
                        {"properties": {"sheetId": 0, "title": "First"}},
                        {"properties": {"sheetId": 5, "title": "O'Brien"}},
                    ]
                },
            )
        if "youtubeanalytics.googleapis.com" in url:
            return httpx.Response(200, json=state["analytics"] or {"columnHeaders": [], "rows": []})
        return httpx.Response(
            200, text="Link\nhttps://youtu.be/CCCCCCCCCCC\n", headers={"content-type": "text/csv"}
        )

    manager = make_manager(make_registry(providers={}, models=[], routing={}), {}, tmp_path)
    uploader = FakeUploader()
    app = create_app(
        manager,
        start_worker=False,
        youtube_settings=YouTubeSettings(
            enabled=True, client_id="cid", client_secret=SECRET, _env_file=None
        ),
        youtube_uploader=uploader,
        sheet_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    clips = FakeClips(tmp_path)
    video = FakeVideo()
    app.state.clips = clips
    app.state.video = video
    data = manager.settings.data_dir

    def sign_in(kind: str) -> None:
        path = data / "google" / f"{kind}_token.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"access_token": ACCESS, "refresh_token": "r", "expires_at": time.time() + 3000}
            )
        )

    def sign_in_youtube() -> None:
        path = data / "youtube" / "token.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"access_token": ACCESS, "refresh_token": "r", "expires_at": time.time() + 3000}
            )
        )

    return TestClient(app, base_url="http://127.0.0.1:8765"), state, clips, uploader, video, sign_in, sign_in_youtube, data


def test_sheet_is_locked_until_login_or_explicit_public_allow(env):
    c, state, *_ = env
    c.put("/api/pipeline/source", json={"sheet_url": SHEET})
    source = c.get("/api/pipeline/source").json()
    assert source["access"] == "locked" and source["allow_public"] is False
    response = c.post("/api/pipeline/sync")
    assert response.status_code == 400 and "pipeline auth sheets" in response.text
    assert state["hits"] == []  # nothing was fetched
    assert c.get("/api/pipeline/source").json()["last_error"]


def test_private_sheet_login_reads_with_token_only_and_never_public_csv(env):
    c, state, _, _, _, sign_in, *_ = env
    sign_in("sheets")
    c.put("/api/pipeline/source", json={"sheet_url": SHEET})
    assert c.get("/api/pipeline/source").json()["access"] == "login"
    result = c.post("/api/pipeline/sync").json()
    assert result["added"] == 2 and result["duplicates"] == 1  # two spellings of A collapse
    urls = [u for u, _ in state["hits"]]
    assert all("sheets.googleapis.com" in u for u in urls)
    assert all(h == f"Bearer {ACCESS}" for _, h in state["hits"])
    assert "O%27%27Brien" in urls[-1]  # gid 5 picked the right tab, quote escaped
    again = c.post("/api/pipeline/sync").json()
    assert again["added"] == 0 and again["duplicates"] == 3
    assert ACCESS not in c.get("/api/pipeline/source").text


def test_same_video_in_other_spelling_is_one_queue_item_and_reuse_is_reported(env):
    c, _, _, _, video, sign_in, *_ = env
    sign_in("sheets")
    c.put("/api/pipeline/source", json={"sheet_url": SHEET})
    c.post("/api/pipeline/sync")
    item = c.get("/api/pipeline/items").json()["items"][0]
    video.duplicate = True  # the library already has a job for this link
    sent = c.post(f"/api/pipeline/items/{item['id']}/send").json()
    assert sent["duplicate"] is True and sent["item"]["reused_job"] == 1
    assert sent["item"]["status"] == "sent"


def test_old_database_without_new_columns_is_migrated(tmp_path):
    import sqlite3

    from clipping_ai.pipeline.store import PipelineStore

    db = tmp_path / "pipeline.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE pipeline_items (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL "
        "UNIQUE, title TEXT, notes TEXT, status TEXT NOT NULL DEFAULT 'new', job_id TEXT, "
        "first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
        "INSERT INTO pipeline_items (url, first_seen_at, updated_at) VALUES "
        "('https://youtu.be/AAAAAAAAAAA', 'x', 'x');"
    )
    conn.commit()
    conn.close()
    store = PipelineStore(db)
    from clipping_ai.pipeline.sheet import SheetRow

    added, dup = store.add_rows_detailed(
        [SheetRow("https://www.youtube.com/watch?v=AAAAAAAAAAA", None, None)]
    )
    assert (added, dup) == (0, 1)


def test_publish_state_marks_scheduled_clips():
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    future = (now + timedelta(days=2)).isoformat()
    past = (now - timedelta(days=1)).isoformat()
    assert publish_state(None, manual_published=False, now=now) == "not_uploaded"
    assert publish_state(None, manual_published=True, now=now) == "manual"
    assert (
        publish_state({"publish_at": future, "privacy": "private"}, manual_published=False, now=now)
        == "scheduled"
    )
    assert (
        publish_state({"publish_at": past, "privacy": "private"}, manual_published=False, now=now)
        == "went_live"
    )
    assert publish_state({"privacy": "private"}, manual_published=False, now=now) == "private"
    assert publish_state({"privacy": "public"}, manual_published=False, now=now) == "public"


def test_plan_slots_skip_taken_days_and_past_times():
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    taken = {"x": {"publish_at": datetime(2026, 11, 26, 18, 0, tzinfo=UTC).isoformat()}}
    slots = plan_slots(now=now, uploads=taken, count=3, hour=18, minute=0, per_day=1)
    assert [s.date().isoformat() for s in slots] == ["2026-11-27", "2026-11-28", "2026-11-29"]
    late = datetime(2026, 11, 26, 17, 45, tzinfo=UTC)  # 18:00 is under 30 minutes away
    first = plan_slots(now=late, uploads={}, count=1, hour=18, minute=0, per_day=1)[0]
    assert first.date().isoformat() == "2026-11-27"
    two = plan_slots(now=now, uploads={}, count=3, hour=18, minute=0, per_day=2)
    assert [s.isoformat() for s in two][:2] == [
        "2026-11-26T18:00:00+00:00",
        "2026-11-26T18:01:00+00:00",
    ]


def test_clip_states_and_exclude(env):
    c, _, clips, *_ = env
    rows = c.get("/api/pipeline/clips").json()["clips"]
    assert len(rows) == 7 and {r["state"] for r in rows} == {"not_uploaded"}
    cid = rows[0]["clip_id"]
    assert c.put(f"/api/pipeline/clips/{cid}/exclude", json={"excluded": True}).status_code == 200
    assert c.put("/api/pipeline/clips/nope/exclude", json={"excluded": True}).status_code == 404
    plan = c.get("/api/pipeline/autopublish/plan").json()
    assert cid not in [i["clip_id"] for i in plan["items"]]
    assert plan["eligible_total"] == 6 and plan["capped"] is True and len(plan["items"]) == 5


def test_autopublish_needs_confirm_and_uploads_private_scheduled_once(env):
    c, _, clips, uploader, _, _, sign_in_youtube, _ = env
    plan = c.get("/api/pipeline/autopublish/plan").json()
    ids = [i["clip_id"] for i in plan["items"]][:2]
    assert c.post("/api/pipeline/autopublish/run", json={"clip_ids": ids}).status_code == 400
    assert uploader.requests == []
    assert (
        c.post("/api/pipeline/autopublish/run", json={"clip_ids": ids, "confirm": True, "allow_unknown_schedule": True}).status_code
        == 400
    )  # not signed in
    sign_in_youtube()
    result = c.post("/api/pipeline/autopublish/run", json={"clip_ids": ids, "confirm": True, "allow_unknown_schedule": True}).json()
    assert [r["status"] for r in result["results"]] == ["scheduled", "scheduled"]
    assert all(r.privacy.value == "private" and r.publish_at is not None for r in uploader.requests)
    assert uploader.requests[0].publish_at < uploader.requests[1].publish_at
    states = {r["clip_id"]: r["state"] for r in c.get("/api/pipeline/clips").json()["clips"]}
    assert states[ids[0]] == "scheduled" and states[ids[1]] == "scheduled"
    again = c.post("/api/pipeline/autopublish/run", json={"clip_ids": ids, "confirm": True, "allow_unknown_schedule": True}).json()
    assert [r["reason"] for r in again["results"]] == ["not_eligible", "not_eligible"]
    assert len(uploader.requests) == 2
    assert (
        c.post(
            "/api/pipeline/autopublish/run", json={"clip_ids": ["x"] * 6, "confirm": True}
        ).status_code
        == 422
    )


def test_autopublish_stops_after_quota_error(env):
    c, _, _, uploader, _, _, sign_in_youtube, _ = env
    sign_in_youtube()
    ids = [i["clip_id"] for i in c.get("/api/pipeline/autopublish/plan").json()["items"]][:3]
    uploader.fail_at = 1
    result = c.post("/api/pipeline/autopublish/run", json={"clip_ids": ids, "confirm": True, "allow_unknown_schedule": True}).json()
    assert [r["status"] for r in result["results"]] == ["scheduled", "failed", "skipped"]


def test_plan_settings_are_validated(env):
    c, *_ = env
    ok = c.put(
        "/api/pipeline/settings", json={"publish_hour": 20, "publish_minute": 30, "per_day": 2}
    )
    assert ok.status_code == 200 and ok.json()["plan_settings"]["publish_hour"] == 20
    assert (
        c.put(
            "/api/pipeline/settings", json={"publish_hour": 25, "publish_minute": 0, "per_day": 1}
        ).status_code
        == 422
    )
    slots = c.get("/api/pipeline/autopublish/plan").json()["items"]
    assert slots[0]["publish_at"][11:16] == "20:30"


def test_insights_rules():
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    uploads, snaps, info = {}, [], {}
    for i, views in enumerate([1000, 100, 120, 20]):
        cid = f"c{i}"
        uploads[cid] = {
            "video_id": f"vid{i:08d}",
            "privacy": "private",
            "publish_at": (now - timedelta(days=5)).isoformat(),
        }
        snaps.append(
            {
                "video_id": f"vid{i:08d}",
                "views": views,
                "likes": 1,
                "comments": 0,
                "avg_view_seconds": 5.0,
                "fetched_at": "t",
            }
        )
        info[cid] = {"title": f"T{i}", "duration": 30.0}
    uploads["new"] = {
        "video_id": "vidnew0000",
        "privacy": "private",
        "publish_at": (now - timedelta(hours=5)).isoformat(),
    }
    uploads["sched"] = {
        "video_id": "vidsched00",
        "privacy": "private",
        "publish_at": (now + timedelta(days=1)).isoformat(),
    }
    out = build_insights(now=now, uploads=uploads, snapshots=snaps, clip_info=info)
    verdicts = {v["clip_id"]: v["verdict"] for v in out["videos"]}
    assert verdicts["c0"] == "strong" and verdicts["c3"] == "weak" and verdicts["c1"] == "ok"
    assert verdicts["new"] == "too_new" and verdicts["sched"] == "not_live"
    kinds = {d["kind"] for d in out["decisions"]}
    assert {"make_more", "change_content", "improve_hook"} <= kinds
    few = build_insights(
        now=now, uploads={"c0": uploads["c0"]}, snapshots=snaps[:1], clip_info=info
    )
    assert few["decisions"][0]["kind"] == "need_more_data"


def test_analytics_refresh_needs_login_reads_numbers_and_has_cooldown(env):
    c, state, clips, _, _, sign_in, sign_in_youtube, data = env
    denied = c.post("/api/pipeline/analytics/refresh")
    assert denied.status_code == 400 and "pipeline auth analytics" in denied.text
    sign_in("analytics")
    cid = next(iter(clips.clips))
    uploads = data / "youtube" / "uploads.json"
    uploads.parent.mkdir(parents=True, exist_ok=True)
    uploads.write_text(
        json.dumps(
            {
                cid: {
                    "video_id": "vidAAAAAAAA",
                    "privacy": "private",
                    "publish_at": (datetime.now(UTC) - timedelta(days=4)).isoformat(),
                    "studio_url": "https://studio.youtube.com/video/vidAAAAAAAA/edit",
                }
            }
        )
    )
    state["analytics"] = {
        "columnHeaders": [
            {"name": "video"},
            {"name": "views"},
            {"name": "averageViewDuration"},
            {"name": "likes"},
        ],
        "rows": [["vidAAAAAAAA", 321, 12.5, 7]],
    }
    result = c.post("/api/pipeline/analytics/refresh")
    assert result.status_code == 200, result.text
    video = result.json()["videos"][0]
    assert video["views"] == 321 and video["likes"] == 7 and video["clip_id"] == cid
    url, header = state["hits"][-1]
    assert "channel%3D%3DMINE" in url and "video%3D%3DvidAAAAAAAA" in url
    assert header == f"Bearer {ACCESS}"
    assert c.post("/api/pipeline/analytics/refresh").status_code >= 400  # cooldown
    assert c.get("/api/pipeline/analytics").json()["videos"][0]["views"] == 321
