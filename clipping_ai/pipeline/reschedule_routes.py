"""Routes for the day page and for re-timing scheduled videos from analytics.

Changing a time on YouTube needs its own sign-in ("manage", youtube.force-ssl). Every change is
shown first and applied only after an explicit confirm. Only videos that are still waiting for a
scheduled release are touched, and only their publish time changes.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.pipeline import google, reschedule, schedule
from clipping_ai.pipeline.connect_routes import local_guard
from clipping_ai.pipeline.store import PipelineStore
from clipping_ai.settings import Settings
from clipping_ai.youtube import auth
from clipping_ai.youtube.config import YouTubeSettings
from clipping_ai.youtube.service import UploadStore

MAX_DAYS_AHEAD = 365
_STOP_CATEGORIES = {ErrorCategory.RATE_LIMIT_EXCEEDED, ErrorCategory.AUTHENTICATION_FAILED}


class MoveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publish_at: str = Field(min_length=10, max_length=40)
    confirm: bool = False
    channel_id: str | None = None


class AddBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clip_id: str = Field(min_length=1, max_length=80)
    publish_at: str = Field(min_length=10, max_length=40)
    confirm: bool = False
    channel_id: str | None = None
    allow_unknown_schedule: bool = False


class PlannedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str = Field(min_length=6, max_length=20)
    new_publish_at: str = Field(min_length=10, max_length=40)


class ApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PlannedItem] = Field(min_length=1, max_length=reschedule.MAX_CHANGES)
    confirm: bool = False
    channel_id: str | None = None


def _bad(message: str) -> AIError:
    return AIError(ErrorCategory.INVALID_REQUEST, message, provider="youtube", retryable=False)


def _aware(value: str, what: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _bad(f"The {what} is not a valid date and time.") from exc
    if parsed.tzinfo is None:
        raise _bad(f"The {what} must include a time zone.")
    return parsed


def register_reschedule_routes(
    app: FastAPI,
    *,
    store: PipelineStore,
    uploads: UploadStore,
    settings: Settings,
    yt: YouTubeSettings,
    http_client: httpx.Client | None,
    chan: Callable[[str | None], str],
    refresh_schedule: Callable[..., str | None],
    channel_records: Callable[[str], dict[str, dict[str, Any]]],
    clip_rows: Callable[..., list[dict[str, Any]]],
    plan_settings: Callable[[str], dict[str, int]],
    batch_lock: threading.Lock,
    upload_clip: Callable[..., dict[str, Any]],
) -> None:
    def manage_ready(cid: str) -> bool:
        return google.is_authorised(settings.data_dir, "manage", cid, yt)

    def need_manage(cid: str) -> None:
        if not manage_ready(cid):
            raise _bad(
                "Changing times on YouTube needs the extra sign-in 'Edit scheduled times' "
                "(Connections on the Pipeline page). Nothing was changed."
            )

    def manage_token(cid: str):
        path = google.token_path(settings.data_dir, "manage", cid, yt)
        return lambda: auth.get_access_token(yt, path, http=http_client)

    def titles_for(request: Request, records: dict[str, dict[str, Any]]) -> dict[str, str]:
        clips = request.app.state.clips
        titles: dict[str, str] = {}
        for clip_id in records:
            try:
                titles[clip_id] = clips.display_title(clips.get(clip_id))
            except (AIError, AttributeError, TypeError):
                continue
        return titles

    def upcoming(request: Request, cid: str, now: datetime) -> list[dict[str, Any]]:
        records = channel_records(cid)
        return reschedule.scheduled_videos(
            records, store.remote_schedule(cid), now=now, titles=titles_for(request, records)
        )

    def remember(cid: str, video_id: str, new_time: str) -> None:
        for clip_id, record in channel_records(cid).items():
            if record.get("video_id") == video_id:
                uploads.put(clip_id, {**record, "publish_at": new_time})

    def build(request: Request, cid: str) -> dict[str, Any]:
        now = datetime.now().astimezone()
        refresh_schedule(cid)
        records = channel_records(cid)
        snapshots = store.analytics()
        mine = {s["video_id"] for s in snapshots} & {
            r.get("video_id") for r in records.values() if r.get("video_id")
        }
        fetched = [s["fetched_at"] for s in snapshots if s["video_id"] in mine]
        points = reschedule.history_points(records, snapshots, now=now)
        plan = reschedule.build_plan(
            now=now,
            upcoming=upcoming(request, cid, now),
            ranking=reschedule.rank_hours(points),
        )
        return {
            **plan,
            "channel_id": cid,
            "manage_connected": manage_ready(cid),
            "analytics_authorised": google.is_authorised(settings.data_dir, "analytics", cid, yt),
            "numbers_read_at": max(fetched) if fetched else None,
            "schedule_known": schedule.is_known(store, cid, now, schedule.KNOWN_FOR),
            "rules": {
                "min_videos": reschedule.MIN_SAMPLES,
                "min_per_hour": reschedule.MIN_PER_HOUR,
                "min_hours": reschedule.MIN_HOURS,
                "min_lift": reschedule.MIN_LIFT,
            },
        }

    @app.get("/api/pipeline/day")
    def day_get(
        request: Request, day_text: str = Query(alias="date"), channel: str | None = None
    ) -> dict[str, Any]:
        cid = chan(channel)
        try:
            day = date.fromisoformat(day_text)
        except ValueError as exc:
            raise _bad("The date must look like 2026-12-07.") from exc
        now = datetime.now().astimezone()
        if not (now.date() - timedelta(days=1) <= day <= now.date() + timedelta(MAX_DAYS_AHEAD)):
            raise _bad("That day is outside the range the schedule shows.")
        refresh_schedule(cid)
        videos = reschedule.videos_on_day(upcoming(request, cid, now), day.isoformat(), now.tzinfo)
        cfg = plan_settings(cid)
        eligible = [
            {"clip_id": r["clip_id"], "title": r["title"], "duration": r["duration"]}
            for r in clip_rows(request, cid)
            if r["state"] == "not_uploaded" and not r["excluded"]
        ]
        return {
            "date": day.isoformat(),
            "channel_id": cid,
            "utc_offset": now.strftime("%z"),
            "is_past": day < now.date(),
            "videos": videos,
            "ready_clips": eligible,
            "manage_connected": manage_ready(cid),
            "can_read": google.can_read_schedule(settings.data_dir, cid, yt),
            "schedule_known": schedule.is_known(store, cid, now, schedule.KNOWN_FOR),
            "upload_authorised": google.is_authorised(settings.data_dir, "upload", cid, yt),
            "youtube_enabled": yt.enabled,
            "default_time": f"{cfg['publish_hour']:02d}:{cfg['publish_minute']:02d}",
        }

    @app.put("/api/pipeline/day/video/{video_id}/time")
    def day_move(request: Request, video_id: str, body: MoveBody) -> dict[str, Any]:
        local_guard(request)
        cid = chan(body.channel_id)
        if not body.confirm:
            raise _bad("Confirm the new time first. Nothing was changed.")
        need_manage(cid)
        when = _aware(body.publish_at, "new time")
        now = datetime.now().astimezone()
        if when < now + reschedule.MIN_LEAD or when > now + timedelta(days=MAX_DAYS_AHEAD):
            raise _bad("Pick a time at least 30 minutes from now and within a year.")
        if not batch_lock.acquire(blocking=False):
            raise _bad("Another change to the schedule is running. Try again in a moment.")
        try:
            refresh_schedule(cid, force=True)
            if video_id not in {v["video_id"] for v in upcoming(request, cid, now)}:
                raise AIError(
                    ErrorCategory.NOT_FOUND,
                    "That video is not a scheduled video of this channel (or its release time "
                    "already passed). Nothing was changed.",
                    provider="youtube",
                    retryable=False,
                )
            new_time = google.set_video_publish_at(
                video_id, when, manage_token(cid), now=now, http=http_client
            )
            remember(cid, video_id, new_time)
            refresh_schedule(cid, force=True)
        finally:
            batch_lock.release()
        return {"video_id": video_id, "publish_at": new_time, "channel_id": cid}

    @app.post("/api/pipeline/day/add")
    def day_add(request: Request, body: AddBody) -> dict[str, Any]:
        local_guard(request)
        cid = chan(body.channel_id)
        if not body.confirm:
            raise _bad("Confirm first. Nothing was uploaded.")
        when = _aware(body.publish_at, "release time")
        now = datetime.now().astimezone()
        if when < now + reschedule.MIN_LEAD or when > now + timedelta(days=MAX_DAYS_AHEAD):
            raise _bad("Pick a time at least 30 minutes from now and within a year.")
        eligible = {
            r["clip_id"]
            for r in clip_rows(request, cid)
            if r["state"] == "not_uploaded" and not r["excluded"]
        }
        if body.clip_id not in eligible:
            raise _bad("That clip is not ready to schedule on this channel.")
        result = upload_clip(
            request,
            cid,
            body.clip_id,
            when,
            allow_unknown_schedule=body.allow_unknown_schedule,
        )
        return {**result, "channel_id": cid}

    @app.get("/api/pipeline/reschedule/plan")
    def reschedule_plan(request: Request, channel: str | None = None) -> dict[str, Any]:
        return build(request, chan(channel))

    @app.post("/api/pipeline/reschedule/apply")
    def reschedule_apply(request: Request, body: ApplyBody) -> dict[str, Any]:
        local_guard(request)
        cid = chan(body.channel_id)
        if not body.confirm:
            raise _bad("Confirm the plan first. Nothing was changed.")
        need_manage(cid)
        if not batch_lock.acquire(blocking=False):
            raise _bad("Another change to the schedule is running. Try again in a moment.")
        try:
            refresh_schedule(cid, force=True)
            plan = build(request, cid)
            planned = {item["video_id"]: item for item in plan["items"]}
            now = datetime.now().astimezone()
            token = manage_token(cid)
            results: list[dict[str, Any]] = []
            stop = False
            for item in body.items:
                target = planned.get(item.video_id)
                asked = _aware(item.new_publish_at, "new time")
                if target is None or datetime.fromisoformat(target["new_publish_at"]) != asked:
                    results.append(
                        {"video_id": item.video_id, "status": "skipped", "reason": "plan_changed"}
                    )
                    continue
                if stop:
                    results.append(
                        {"video_id": item.video_id, "status": "skipped", "reason": "stopped"}
                    )
                    continue
                try:
                    new_time = google.set_video_publish_at(
                        item.video_id, asked, token, now=now, http=http_client
                    )
                    remember(cid, item.video_id, new_time)
                    results.append(
                        {
                            "video_id": item.video_id,
                            "status": "changed",
                            "old_publish_at": target["old_publish_at"],
                            "publish_at": new_time,
                        }
                    )
                except AIError as exc:
                    results.append(
                        {"video_id": item.video_id, "status": "failed", "error": exc.message}
                    )
                    if exc.category in _STOP_CATEGORIES:
                        stop = True
            refresh_schedule(cid, force=True)
        finally:
            batch_lock.release()
        return {"results": results, "channel_id": cid}
