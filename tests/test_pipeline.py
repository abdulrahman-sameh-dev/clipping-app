"""Sheet links, queue and daily review. No network: the sheet is served by httpx.MockTransport."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from clipping_ai.api.app import create_app
from clipping_ai.pipeline.review import build_review
from clipping_ai.pipeline.sheet import csv_export_url, parse_rows
from clipping_ai.youtube.config import YouTubeSettings
from tests.helpers import make_manager, make_registry

SHEET = "https://docs.google.com/spreadsheets/d/abc123/edit?usp=sharing#gid=77"
CSV = "Timestamp,Video link,Notes\n1/1,https://youtu.be/aaa,good\n2/1,https://youtu.be/bbb,\n"


def test_export_url_keeps_gid_and_rejects_other_hosts():
    assert csv_export_url(SHEET) == (
        "https://docs.google.com/spreadsheets/d/abc123/export?format=csv&gid=77"
    )
    for bad in (
        "http://docs.google.com/spreadsheets/d/x",
        "https://evil.example/spreadsheets/d/x",
        "https://docs.google.com/document/d/x",
    ):
        with pytest.raises(Exception):  # noqa: B017
            csv_export_url(bad)


def test_parse_rows_with_header_form_sheet_and_duplicates():
    rows = parse_rows(CSV + "3/1,https://youtu.be/aaa,again\n")
    assert [r.url for r in rows] == ["https://youtu.be/aaa", "https://youtu.be/bbb"]
    assert rows[0].notes == "good"
    plain = parse_rows("https://youtu.be/x\nnot a link\n")
    assert [r.url for r in plain] == ["https://youtu.be/x"]


def test_review_flags_empty_days_and_ready_clips():
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    soon = (now + timedelta(days=1, hours=3)).isoformat()
    review = build_review(
        now=now,
        source={"sheet_url": "u", "last_sync_at": now.isoformat(), "last_error": None},
        counts={"new": 2, "sent": 1, "skipped": 0},
        uploads={"c1": {"publish_at": soon}},
        ready_clip_ids=["c1", "c2"],
        failed_clips=1,
    )
    kinds = {t["kind"]: t["count"] for t in review["tips"]}
    assert kinds["new_links"] == 2 and kinds["ready_clips"] == 1 and kinds["failed_clips"] == 1
    assert kinds["empty_days"] == 6
    assert sum(d["count"] for d in review["schedule"]) == 1


class FakeVideo:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def create_url(self, url: str):
        self.calls.append(url)

        class Job:
            id = "job1"

        return Job(), False


@pytest.fixture
def client(tmp_path):
    state = {"body": CSV, "status": 200, "type": "text/csv", "hits": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["hits"].append(str(request.url))
        return httpx.Response(
            state["status"], text=state["body"], headers={"content-type": state["type"]}
        )

    manager = make_manager(make_registry(providers={}, models=[], routing={}), {}, tmp_path)
    app = create_app(
        manager,
        start_worker=False,
        youtube_settings=YouTubeSettings(_env_file=None),
        sheet_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    video = FakeVideo()
    app.state.video = video
    return TestClient(app), state, video


def test_save_sync_and_dedupe(client):
    c, state, _ = client
    assert c.put("/api/pipeline/source", json={"sheet_url": SHEET}).status_code == 200
    first = c.post("/api/pipeline/sync").json()
    assert first["added"] == 2 and first["counts"]["new"] == 2 and first["last_error"] is None
    assert c.post("/api/pipeline/sync").json()["added"] == 0
    assert state["hits"][0].startswith("https://docs.google.com/spreadsheets/d/abc123/export")
    assert len(c.get("/api/pipeline/items").json()["items"]) == 2


def test_rejects_foreign_host_and_missing_sheet(client):
    c, *_ = client
    assert c.post("/api/pipeline/sync").status_code == 400
    assert (
        c.put("/api/pipeline/source", json={"sheet_url": "https://evil.example/x"}).status_code
        == 400
    )


def test_private_sheet_gives_a_clear_error_and_is_remembered(client):
    c, state, _ = client
    c.put("/api/pipeline/source", json={"sheet_url": SHEET})
    state.update(type="text/html", body="<html>sign in</html>")
    response = c.post("/api/pipeline/sync")
    assert response.status_code == 400 and "Anyone with the link" in response.text
    assert c.get("/api/pipeline/source").json()["last_error"]


def test_send_once_skip_and_restore(client):
    c, _, video = client
    c.put("/api/pipeline/source", json={"sheet_url": SHEET})
    c.post("/api/pipeline/sync")
    items = c.get("/api/pipeline/items").json()["items"]
    first, second = items[0]["id"], items[1]["id"]
    ok = c.post(f"/api/pipeline/items/{first}/send")
    assert ok.status_code == 200 and ok.json()["item"]["status"] == "sent"
    assert c.post(f"/api/pipeline/items/{first}/send").status_code == 400
    assert len(video.calls) == 1
    assert c.post(f"/api/pipeline/items/{first}/skip").status_code == 400
    assert c.post(f"/api/pipeline/items/{second}/skip").json()["item"]["status"] == "skipped"
    assert c.post(f"/api/pipeline/items/{second}/restore").json()["item"]["status"] == "new"
    assert c.post("/api/pipeline/items/999/send").status_code == 404


def test_review_endpoint_and_no_secrets(client):
    c, *_ = client
    c.put("/api/pipeline/source", json={"sheet_url": SHEET})
    c.post("/api/pipeline/sync")
    review = c.get("/api/pipeline/review")
    assert review.status_code == 200
    assert any(t["kind"] == "new_links" for t in review.json()["tips"])
    assert len(review.json()["schedule"]) == 7


def test_pipeline_page_assets_are_served(client):
    c, *_ = client
    assert "pipeline.css" in c.get("/ui/").text and 'data-nav="pipeline"' in c.get("/ui/").text
    assert "pipelineView" in c.get("/ui/js/pipeline.js").text
    assert c.get("/ui/pipeline.css").status_code == 200
