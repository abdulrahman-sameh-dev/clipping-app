"""Web UI routes for connecting Google accounts, managing channels and reading a channel's schedule.

These routes are served by the same local API as everything else. That API has no login, so
the routes that change state (sign in, sign out, channels) refuse requests that do not look like
they come from a page on this computer: the Host must be a loopback name and a browser's
Origin, when it sends one, must be a loopback page too. This blocks other websites from driving
the local API through your browser. It is not a password: any program on this computer, or on a
network you expose the API to, can still call it. See PIPELINE.md.
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.pipeline import google, schedule
from clipping_ai.pipeline.store import CATEGORIES, DEFAULT_CHANNEL, PipelineStore
from clipping_ai.youtube.config import YouTubeSettings

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_SAFE_REASON = re.compile(r"^[a-z_]{1,20}$")


def allowed_hosts() -> set[str]:
    extra = {h.strip().lower() for h in os.environ.get("CLIPPING_AI_ALLOWED_HOSTS", "").split(",")}
    return LOOPBACK | {h for h in extra if h}


def local_guard(request: Request) -> None:
    """Refuse requests that were not made by a page served from this computer."""
    hosts = allowed_hosts()
    host = (request.url.hostname or "").lower()
    if host not in hosts:
        raise AIError(
            ErrorCategory.POLICY_VIOLATION,
            "Open the app at http://127.0.0.1:PORT (this computer) to use this feature. "
            "Other host names are refused. See PIPELINE.md.",
            retryable=False,
        )
    origin = request.headers.get("origin")
    if origin and origin != "null":
        origin_host = (urlparse(origin).hostname or "").lower()
        if origin_host not in hosts:
            raise AIError(
                ErrorCategory.POLICY_VIOLATION,
                "This request came from another website and was refused.",
                retryable=False,
            )
    elif origin == "null" or request.headers.get("sec-fetch-site") == "cross-site":
        raise AIError(
            ErrorCategory.POLICY_VIOLATION,
            "This request came from another website and was refused.",
            retryable=False,
        )


class ChannelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    category: str = "general"
    publish_hour: int = Field(default=18, ge=0, le=23)
    publish_minute: int = Field(default=0, ge=0, le=59)
    per_day: int = Field(default=1, ge=1, le=5)
    strong_ratio: float = Field(default=1.5, ge=1.1, le=10)
    weak_ratio: float = Field(default=0.5, ge=0.05, le=0.9)


class ChannelUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=60)
    category: str | None = None
    publish_hour: int | None = Field(default=None, ge=0, le=23)
    publish_minute: int | None = Field(default=None, ge=0, le=59)
    per_day: int | None = Field(default=None, ge=1, le=5)
    strong_ratio: float | None = Field(default=None, ge=1.1, le=10)
    weak_ratio: float | None = Field(default=None, ge=0.05, le=0.9)


class ConnectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    channel_id: str = DEFAULT_CHANNEL
    return_to: str | None = Field(default=None, max_length=500)


class DisconnectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    channel_id: str = DEFAULT_CHANNEL


class ClipChannelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str | None = None


def _check_category(category: str) -> str:
    if category not in CATEGORIES:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            f"Category must be one of: {', '.join(CATEGORIES)}.",
            retryable=False,
        )
    return category


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20].strip("-")
    return base or "channel"


def register_connection_routes(
    app: FastAPI,
    *,
    store: PipelineStore,
    data_dir: Path,
    yt: YouTubeSettings,
    http_client: httpx.Client | None,
    chan: Callable[[str | None], str],
    refresh_schedule: Callable[..., str | None],
) -> None:
    web = google.WebConnect()

    def channel_view(row: dict[str, Any]) -> dict[str, Any]:
        cid = row["id"]
        return {
            "id": cid,
            "name": row["name"],
            "category": row["category"],
            "is_default": cid == DEFAULT_CHANNEL,
            "publish_hour": row["publish_hour"],
            "publish_minute": row["publish_minute"],
            "per_day": row["per_day"],
            "strong_ratio": row["strong_ratio"],
            "weak_ratio": row["weak_ratio"],
            "sheet_url": row["sheet_url"],
            "youtube_channel_title": row["youtube_channel_title"],
            "upload_authorised": google.is_authorised(data_dir, "upload", cid, yt),
            "analytics_authorised": google.is_authorised(data_dir, "analytics", cid, yt),
            "schedule_ready": google.can_read_schedule(data_dir, cid, yt),
            "counts": store.counts(cid),
        }

    def google_status(channel_id: str, request: Request) -> dict[str, Any]:
        try:
            yt.credentials()
            configured = True
        except AIError:
            configured = False
        return {
            "channel_id": channel_id,
            "client_configured": configured,
            "youtube_enabled": yt.enabled,
            "redirect_uri": f"{request.url.scheme}://{request.url.netloc}/api/google/callback",
            "kinds": {
                "sheets": {"connected": google.is_authorised(data_dir, "sheets", channel_id, yt)},
                "analytics": {
                    "connected": google.is_authorised(data_dir, "analytics", channel_id, yt),
                    "schedule_ready": google.can_read_schedule(data_dir, channel_id, yt),
                },
                "upload": {"connected": google.is_authorised(data_dir, "upload", channel_id, yt)},
            },
        }

    def safe_return_to(request: Request, value: str | None) -> str:
        default = f"{request.url.scheme}://{request.url.netloc}/ui/"
        if not value:
            return default
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or (parsed.hostname or "") not in allowed_hosts():
            return default
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"

    # --- channels -------------------------------------------------------------------------

    @app.get("/api/pipeline/channels")
    def channels_list() -> dict[str, Any]:
        return {
            "channels": [channel_view(r) for r in store.list_channels()],
            "categories": list(CATEGORIES),
        }

    @app.post("/api/pipeline/channels")
    def channels_create(request: Request, body: ChannelBody) -> dict[str, Any]:
        local_guard(request)
        category = _check_category(body.category)
        values = body.model_dump()
        values["category"] = category
        values["name"] = body.name.strip()
        channel_id = f"{_slug(body.name)}-{secrets.token_hex(3)}"
        row = store.create_channel(channel_id, values)
        return channel_view(row)

    @app.put("/api/pipeline/channels/{channel_id}")
    def channels_update(
        request: Request, channel_id: str, body: ChannelUpdateBody
    ) -> dict[str, Any]:
        local_guard(request)
        cid = chan(channel_id)
        values = body.model_dump(exclude_none=True)
        if "category" in values:
            _check_category(values["category"])
        if "name" in values:
            values["name"] = values["name"].strip()
        store.update_channel(cid, values)
        return channel_view(store.get_channel(cid) or {})

    @app.delete("/api/pipeline/channels/{channel_id}")
    def channels_delete(request: Request, channel_id: str) -> dict[str, Any]:
        local_guard(request)
        cid = chan(channel_id)
        if cid == DEFAULT_CHANNEL:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "The first channel cannot be removed.",
                retryable=False,
            )
        if store.channel_item_count(cid):
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "This channel still has links in its queue, so it cannot be removed yet.",
                retryable=False,
            )
        for kind in ("upload", "analytics"):
            google.forget(data_dir, kind, cid, yt)
        store.delete_channel(cid)
        return {"deleted": cid}

    @app.put("/api/pipeline/clips/{clip_id}/channel")
    def clip_channel(request: Request, clip_id: str, body: ClipChannelBody) -> dict[str, Any]:
        local_guard(request)
        request.app.state.clips.get(clip_id)  # 404 for an unknown clip
        cid = chan(body.channel_id) if body.channel_id else None
        store.set_clip_channel(clip_id, cid)
        return {"clip_id": clip_id, "channel_id": cid}

    # --- Google sign-ins from the UI ----------------------------------------------------------

    @app.get("/api/google/status")
    def status(request: Request, channel: str | None = None) -> dict[str, Any]:
        return google_status(chan(channel), request)

    @app.post("/api/google/connect")
    def connect(request: Request, body: ConnectBody) -> dict[str, Any]:
        local_guard(request)
        cid = chan(body.channel_id)
        if body.kind not in google.SCOPES:
            raise AIError(ErrorCategory.INVALID_REQUEST, "Unknown sign-in type.", retryable=False)
        redirect_uri = f"{request.url.scheme}://{request.url.netloc}/api/google/callback"
        url = web.start(
            kind=body.kind,
            channel_id=cid,
            redirect_uri=redirect_uri,
            return_to=safe_return_to(request, body.return_to),
            settings=yt,
        )
        return {"url": url, "redirect_uri": redirect_uri, "kind": body.kind, "channel_id": cid}

    @app.get("/api/google/callback")
    def callback(
        request: Request,
        state: str = "",
        code: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        # Only the Host is checked here: this is a top-level redirect from Google, so the
        # browser sends no useful Origin. The one-time random `state` is the proof that this
        # sign-in was started from this app.
        if (request.url.hostname or "").lower() not in allowed_hosts():
            raise AIError(ErrorCategory.POLICY_VIOLATION, "Unexpected host name.", retryable=False)
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        try:
            info = web.finish(
                state=state,
                code=code,
                error=error,
                settings=yt,
                data_dir=data_dir,
                http=http_client,
            )
            outcome = {"connected": info["kind"], "channel": info["channel_id"]}
            if info["kind"] == "analytics":
                refresh_schedule(info["channel_id"], force=True)
            target = info["return_to"]
        except google.ConnectDenied as denied:
            reason = denied.reason if _SAFE_REASON.match(denied.reason) else "failed"
            outcome = {
                "connect_error": reason,
                "kind": denied.info["kind"],
                "channel": denied.info["channel_id"],
            }
            target = denied.info["return_to"]
        return RedirectResponse(
            f"{target}#/pipeline?{urlencode(outcome)}", status_code=303, headers=headers
        )

    @app.post("/api/google/disconnect")
    def disconnect(request: Request, body: DisconnectBody) -> dict[str, Any]:
        local_guard(request)
        cid = chan(body.channel_id)
        if body.kind not in google.SCOPES:
            raise AIError(ErrorCategory.INVALID_REQUEST, "Unknown sign-in type.", retryable=False)
        removed = google.forget(data_dir, body.kind, cid, yt)
        if body.kind == "analytics":
            store.clear_remote_schedule(cid)
        return {"removed": removed, **google_status(cid, request)}

    # --- the channel's own schedule -----------------------------------------------------------

    @app.get("/api/pipeline/schedule")
    def schedule_get(channel: str | None = None) -> dict[str, Any]:
        cid = chan(channel)
        now = datetime.now().astimezone()
        refresh_schedule(cid)
        return schedule.view(store, data_dir, yt, cid, now)

    @app.post("/api/pipeline/schedule/refresh")
    def schedule_refresh(request: Request, channel: str | None = None) -> dict[str, Any]:
        local_guard(request)
        cid = chan(channel)
        now = datetime.now().astimezone()
        if not google.can_read_schedule(data_dir, cid, yt):
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Connect YouTube read access for this channel first (Connections).",
                provider="youtube",
                retryable=False,
            )
        error = refresh_schedule(cid, force=True)
        if error:
            raise AIError(
                ErrorCategory.PROVIDER_UNAVAILABLE, error, provider="youtube", retryable=True
            )
        return schedule.view(store, data_dir, yt, cid, now)
