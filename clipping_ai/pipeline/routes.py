"""HTTP routes for the content pipeline (Sheet links, queue, daily review).

Served by the same local API as the rest of the app. That API has no authentication and
should listen on localhost only. Nothing here uploads or schedules anything: the user sends a
link to clipping with one click, and uploads stay in the YouTube panel on the clip page.
"""

from __future__ import annotations

import threading
import time as _time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field

from clipping_ai.clips.models import ClipStatus
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.pipeline import google
from clipping_ai.pipeline.insights import build_insights
from clipping_ai.pipeline.plan import MAX_BATCH, plan_slots, publish_state
from clipping_ai.pipeline.review import build_review
from clipping_ai.pipeline.sheet import csv_export_url, fetch_rows
from clipping_ai.pipeline.store import PipelineStore
from clipping_ai.settings import Settings
from clipping_ai.youtube import auth
from clipping_ai.youtube.client import YouTubeClient
from clipping_ai.youtube.config import YouTubeSettings
from clipping_ai.youtube.models import Privacy
from clipping_ai.youtube.service import UploadStore, YouTubeUploadService

ANALYTICS_DAYS = 28
ANALYTICS_COOLDOWN_SECONDS = 60
_STOP_CATEGORIES = {ErrorCategory.RATE_LIMIT_EXCEEDED, ErrorCategory.AUTHENTICATION_FAILED}


class SheetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheet_url: str = Field(min_length=1, max_length=2000)
    allow_public: bool | None = None


class PlanSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publish_hour: int = Field(ge=0, le=23)
    publish_minute: int = Field(ge=0, le=59)
    per_day: int = Field(ge=1, le=5)


class AutoPublishBody(BaseModel):
    """The UI sends back the clips it showed in the preview. `confirm` must be true."""

    model_config = ConfigDict(extra="forbid")

    clip_ids: list[str] = Field(min_length=1, max_length=MAX_BATCH)
    confirm: bool = False


class ExcludeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    excluded: bool


def _not_found() -> AIError:
    return AIError(ErrorCategory.NOT_FOUND, "Queue item not found.", retryable=False)


def register_pipeline_routes(
    app: FastAPI,
    *,
    settings: Settings,
    uploads_path: Path,
    http_client: httpx.Client | None = None,
    youtube_settings: YouTubeSettings | None = None,
    youtube_uploader: Any | None = None,
) -> None:
    store = PipelineStore(settings.data_dir / "pipeline.db")
    uploads = UploadStore(uploads_path)
    yt = youtube_settings or YouTubeSettings()
    send_lock = threading.Lock()
    batch_lock = threading.Lock()
    refresh_state = {"last": 0.0}

    def token_provider(kind: str):
        path = google.token_path(settings.data_dir, kind)
        return lambda: auth.get_access_token(yt, path, http=http_client)

    def plan_settings() -> dict[str, int]:
        return {
            "publish_hour": int(store.get_setting("publish_hour")),
            "publish_minute": int(store.get_setting("publish_minute")),
            "per_day": int(store.get_setting("per_day")),
        }

    def source_view() -> dict[str, Any]:
        sheets = google.is_authorised(settings.data_dir, "sheets")
        allow = store.get_setting("allow_public_sheet") == "1"
        return {
            **store.source(),
            "counts": store.counts(),
            "access": "login" if sheets else ("public_link" if allow else "locked"),
            "sheets_authorised": sheets,
            "analytics_authorised": google.is_authorised(settings.data_dir, "analytics"),
            "allow_public": allow,
            "plan_settings": plan_settings(),
        }

    def sync(sheet_url: str) -> tuple[int, int]:
        try:
            if google.is_authorised(settings.data_dir, "sheets"):
                rows = google.fetch_private_rows(
                    sheet_url, token_provider("sheets"), http=http_client
                )
            elif store.get_setting("allow_public_sheet") == "1":
                rows = fetch_rows(sheet_url, client=http_client)
            else:
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    "Sheet access is locked. Sign in with Google (read-only) by running "
                    "`uv run clipping-ai pipeline auth sheets` on this computer. See PIPELINE.md.",
                    provider="sheet",
                    retryable=False,
                )
        except AIError as exc:
            store.record_sync(exc.message if hasattr(exc, "message") else str(exc))
            raise
        added, duplicates = store.add_rows_detailed(rows)
        store.record_sync(None)
        return added, duplicates

    def clip_rows(request: Request) -> list[dict[str, Any]]:
        clips = request.app.state.clips
        now = datetime.now().astimezone()
        records = uploads.load()
        excluded = store.excluded_clips()
        rows: list[dict[str, Any]] = []
        for clip in clips.list(status=ClipStatus.READY, limit=500):
            record = records.get(clip.id)
            studio = (record or {}).get("studio_url")
            rows.append(
                {
                    "clip_id": clip.id,
                    "title": clips.display_title(clip),
                    "duration": clip.duration,
                    "created_at": clip.created_at,
                    "state": publish_state(record, manual_published=clip.published, now=now),
                    "publish_at": (record or {}).get("publish_at"),
                    "studio_url": studio
                    if isinstance(studio, str) and studio.startswith("https://studio.youtube.com/")
                    else None,
                    "excluded": clip.id in excluded,
                }
            )
        return rows

    def build_plan(request: Request, only: list[str] | None = None) -> dict[str, Any]:
        now = datetime.now().astimezone()
        cfg = plan_settings()
        rows = [r for r in clip_rows(request) if r["state"] == "not_uploaded" and not r["excluded"]]
        rows.sort(key=lambda r: r["created_at"])
        if only is not None:
            rows = [r for r in rows if r["clip_id"] in only]
        total = len(rows)
        rows = rows[:MAX_BATCH]
        slots = plan_slots(
            now=now,
            uploads=uploads.load(),
            count=len(rows),
            hour=cfg["publish_hour"],
            minute=cfg["publish_minute"],
            per_day=cfg["per_day"],
        )
        items = [
            {"clip_id": r["clip_id"], "title": r["title"], "publish_at": slot.isoformat()}
            for r, slot in zip(rows, slots, strict=False)
        ]
        return {
            "items": items,
            "eligible_total": total,
            "capped": total > MAX_BATCH,
            "max_batch": MAX_BATCH,
            "settings": cfg,
            "youtube_enabled": yt.enabled,
            "youtube_authorised": bool(
                (auth.load_token(yt.resolved_token_path(settings.data_dir)) or {}).get(
                    "refresh_token"
                )
            ),
        }

    def insights_view(request: Request) -> dict[str, Any]:
        clips = request.app.state.clips
        records = uploads.load()
        info: dict[str, dict[str, Any]] = {}
        for clip_id in records:
            try:
                clip = clips.get(clip_id)
                info[clip_id] = {"title": clips.display_title(clip), "duration": clip.duration}
            except (AIError, AttributeError, TypeError):
                continue
        result = build_insights(
            now=datetime.now().astimezone(),
            uploads=records,
            snapshots=store.analytics(),
            clip_info=info,
        )
        return {**result, "authorised": google.is_authorised(settings.data_dir, "analytics")}

    @app.get("/api/pipeline/source")
    def pipeline_source() -> dict[str, Any]:
        return source_view()

    @app.put("/api/pipeline/source")
    def pipeline_set_source(body: SheetBody) -> dict[str, Any]:
        csv_export_url(body.sheet_url)  # validates host and shape
        store.set_sheet_url(body.sheet_url.strip())
        if body.allow_public is not None:
            store.set_setting("allow_public_sheet", "1" if body.allow_public else "0")
        return source_view()

    @app.put("/api/pipeline/settings")
    def pipeline_settings(body: PlanSettingsBody) -> dict[str, Any]:
        store.set_setting("publish_hour", str(body.publish_hour))
        store.set_setting("publish_minute", str(body.publish_minute))
        store.set_setting("per_day", str(body.per_day))
        return source_view()

    @app.post("/api/pipeline/sync")
    def pipeline_sync() -> dict[str, Any]:
        sheet_url = store.source()["sheet_url"]
        if not sheet_url:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Save a Google Sheet link first.",
                retryable=False,
            )
        added, duplicates = sync(sheet_url)
        return {"added": added, "duplicates": duplicates, **source_view()}

    @app.get("/api/pipeline/items")
    def pipeline_items() -> dict[str, Any]:
        return {"items": store.list_items(), "counts": store.counts()}

    @app.post("/api/pipeline/items/{item_id}/send")
    def pipeline_send(request: Request, item_id: int) -> dict[str, Any]:
        with send_lock:
            item = store.get(item_id)
            if item is None:
                raise _not_found()
            if item["status"] == "sent":
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    "This link was already sent to clipping.",
                    retryable=False,
                )
            job, duplicate = request.app.state.video.create_url(item["url"])
            store.set_status(item_id, "sent", job.id, reused=duplicate)
            existing_clips = 0
            if duplicate:
                try:
                    existing_clips = len(request.app.state.clips.list(source_job_id=job.id))
                except (AIError, AttributeError, TypeError):
                    existing_clips = 0
        return {
            "item": store.get(item_id),
            "job_id": job.id,
            "duplicate": duplicate,
            "existing_clips": existing_clips,
        }

    @app.post("/api/pipeline/items/{item_id}/skip")
    def pipeline_skip(item_id: int) -> dict[str, Any]:
        item = store.get(item_id)
        if item is None:
            raise _not_found()
        if item["status"] == "sent":
            raise AIError(
                ErrorCategory.INVALID_REQUEST, "A sent link cannot be skipped.", retryable=False
            )
        store.set_status(item_id, "skipped")
        return {"item": store.get(item_id)}

    @app.post("/api/pipeline/items/{item_id}/restore")
    def pipeline_restore(item_id: int) -> dict[str, Any]:
        item = store.get(item_id)
        if item is None:
            raise _not_found()
        if item["status"] == "skipped":
            store.set_status(item_id, "new")
        return {"item": store.get(item_id)}

    @app.get("/api/pipeline/review")
    def pipeline_review(request: Request) -> dict[str, Any]:
        clips = request.app.state.clips
        ready: list[str] = []
        failed = 0
        try:
            ready = [
                c.id for c in clips.list(status=ClipStatus.READY, limit=500) if not c.published
            ]
            failed = len(clips.list(status=ClipStatus.FAILED, limit=500))
        except (AttributeError, TypeError):
            pass
        return build_review(
            now=datetime.now().astimezone(),
            source=store.source(),
            counts=store.counts(),
            uploads=uploads.load(),
            ready_clip_ids=ready,
            failed_clips=failed,
        )

    @app.get("/api/pipeline/clips")
    def pipeline_clips(request: Request) -> dict[str, Any]:
        return {"clips": clip_rows(request)}

    @app.put("/api/pipeline/clips/{clip_id}/exclude")
    def pipeline_exclude(request: Request, clip_id: str, body: ExcludeBody) -> dict[str, Any]:
        request.app.state.clips.get(clip_id)  # 404 for an unknown clip
        store.set_excluded(clip_id, body.excluded)
        return {"clip_id": clip_id, "excluded": body.excluded}

    @app.get("/api/pipeline/autopublish/plan")
    def pipeline_autopublish_plan(request: Request) -> dict[str, Any]:
        return build_plan(request)

    @app.post("/api/pipeline/autopublish/run")
    def pipeline_autopublish_run(request: Request, body: AutoPublishBody) -> dict[str, Any]:
        if not body.confirm:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Confirm the plan first. Nothing was uploaded.",
                retryable=False,
            )
        yt.require_enabled()
        yt.credentials()
        token = auth.load_token(yt.resolved_token_path(settings.data_dir))
        if not (token and token.get("refresh_token")):
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "YouTube is not authorised yet. Run `uv run clipping-ai youtube auth` first.",
                provider="youtube",
                retryable=False,
            )
        if not batch_lock.acquire(blocking=False):
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "An auto-publish run is already in progress.",
                retryable=False,
            )
        try:
            plan = build_plan(request, only=body.clip_ids)
            planned = {item["clip_id"]: item for item in plan["items"]}
            inflight = request.app.state.youtube_inflight
            token_path = yt.resolved_token_path(settings.data_dir)
            service = YouTubeUploadService(
                yt,
                request.app.state.clips,
                youtube_uploader or YouTubeClient(lambda: auth.get_access_token(yt, token_path)),
                uploads,
            )
            results: list[dict[str, Any]] = []
            stop = False
            for clip_id in body.clip_ids:
                item = planned.get(clip_id)
                if item is None:
                    results.append(
                        {"clip_id": clip_id, "status": "skipped", "reason": "not_eligible"}
                    )
                    continue
                if stop:
                    results.append({"clip_id": clip_id, "status": "skipped", "reason": "stopped"})
                    continue
                try:
                    publish_at = datetime.fromisoformat(item["publish_at"])
                    upload_request = service.build_request(
                        clip_id, privacy=Privacy.PRIVATE, publish_at=publish_at
                    )
                    if not inflight.claim(clip_id):
                        results.append(
                            {"clip_id": clip_id, "status": "skipped", "reason": "in_progress"}
                        )
                        continue
                    try:
                        record = service.upload(upload_request, force=False)
                    finally:
                        inflight.release(clip_id)
                    results.append(
                        {
                            "clip_id": clip_id,
                            "status": "scheduled",
                            "publish_at": record.get("publish_at"),
                            "studio_url": record.get("studio_url"),
                        }
                    )
                except AIError as exc:
                    results.append({"clip_id": clip_id, "status": "failed", "error": exc.message})
                    if exc.category in _STOP_CATEGORIES:
                        stop = True
            return {"results": results}
        finally:
            batch_lock.release()

    @app.get("/api/pipeline/analytics")
    def pipeline_analytics(request: Request) -> dict[str, Any]:
        return insights_view(request)

    @app.post("/api/pipeline/analytics/refresh")
    def pipeline_analytics_refresh(request: Request) -> dict[str, Any]:
        if not google.is_authorised(settings.data_dir, "analytics"):
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Analytics is not connected. Run `uv run clipping-ai pipeline auth analytics` "
                "on this computer (read-only sign-in). See PIPELINE.md.",
                provider="google",
                retryable=False,
            )
        wait = ANALYTICS_COOLDOWN_SECONDS - (_time.monotonic() - refresh_state["last"])
        if wait > 0:
            raise AIError(
                ErrorCategory.RATE_LIMIT_EXCEEDED,
                f"Analytics was just refreshed. Try again in {int(wait) + 1} seconds.",
                provider="google",
                retryable=True,
            )
        records = uploads.load()
        by_video = {r["video_id"]: cid for cid, r in records.items() if r.get("video_id")}
        today = datetime.now().astimezone().date()
        rows = google.fetch_video_analytics(
            list(by_video),
            token_provider("analytics"),
            start=today - timedelta(days=ANALYTICS_DAYS),
            end=today,
            http=http_client,
        )
        refresh_state["last"] = _time.monotonic()
        seen = {row["video_id"] for row in rows}
        for video_id in by_video:  # videos without a row had no views in the window
            if video_id not in seen:
                rows.append(
                    {
                        "video_id": video_id,
                        "views": 0,
                        "minutes_watched": 0.0,
                        "avg_view_seconds": 0.0,
                        "likes": 0,
                        "comments": 0,
                        "subscribers_gained": 0,
                        "window_start": (today - timedelta(days=ANALYTICS_DAYS)).isoformat(),
                        "window_end": today.isoformat(),
                    }
                )
        for row in rows:
            row["clip_id"] = by_video.get(row["video_id"])
        store.save_analytics(rows)
        return insights_view(request)
