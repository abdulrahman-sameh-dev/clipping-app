"""HTTP routes for Facebook Page publishing.

Same safety rules as the rest of the local API: state-changing routes refuse requests that do
not come from a page on this computer. Nothing is published unless the request says
`confirm: true` (the UI asks first and names the Page and the time). A clip that was already
sent is refused, and a Page is stopped before Meta's limit of 30 API Reels per 24 hours.
No route returns a token or the app secret.
"""

from __future__ import annotations

import secrets
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.facebook import client as fb_client
from clipping_ai.facebook.config import (
    DAILY_LIMIT,
    MAX_SCHEDULE_DAYS,
    MIN_SCHEDULE_MINUTES,
    FacebookSettings,
)
from clipping_ai.facebook.store import PostStore, TokenStore
from clipping_ai.pipeline.connect_routes import allowed_hosts, local_guard
from clipping_ai.settings import Settings
from clipping_ai.youtube.models import parse_publish_at

FLOW_TTL_SECONDS = 600
MAX_PENDING_FLOWS = 8
MAX_DESCRIPTION = 2000


class ConnectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    return_to: str | None = Field(default=None, max_length=500)


class PageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_id: str = Field(min_length=1, max_length=40)


class PublishBody(BaseModel):
    """What the UI may send. There is no `force` field and no way to skip `confirm`."""

    model_config = ConfigDict(extra="forbid")

    mode: str = "now"  # "now" or "schedule"
    publish_at: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=4000)
    confirm: bool = False


def _invalid(message: str) -> AIError:
    return AIError(ErrorCategory.INVALID_REQUEST, message, provider="facebook", retryable=False)


class InFlight:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: set[str] = set()

    def claim(self, clip_id: str) -> bool:
        with self._lock:
            if clip_id in self._active:
                return False
            self._active.add(clip_id)
            return True

    def release(self, clip_id: str) -> None:
        with self._lock:
            self._active.discard(clip_id)

    def active(self, clip_id: str) -> bool:
        with self._lock:
            return clip_id in self._active


def _public_post(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    return {
        "video_id": record.get("video_id"),
        "page_id": record.get("page_id"),
        "page_name": record.get("page_name"),
        "mode": record.get("mode"),
        "publish_at": record.get("publish_at"),
        "sent_at": record.get("sent_at"),
    }


def register_facebook_routes(
    app: FastAPI,
    *,
    settings: Settings,
    facebook_settings: FacebookSettings | None = None,
    facebook_client: Any | None = None,
) -> None:
    fb = facebook_settings or FacebookSettings()
    tokens = TokenStore(fb.token_path(settings.data_dir))
    posts = PostStore(fb.posts_path(settings.data_dir))
    inflight = InFlight()
    pending: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()

    def client() -> Any:
        return facebook_client or fb_client.FacebookClient(fb)

    def page_views(data: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not data:
            return []
        return [
            {"id": p["id"], "name": p["name"], "can_post": _can_post(p)}
            for p in data.get("pages", [])
        ]

    def _can_post(page: dict[str, Any]) -> bool:
        tasks = page.get("tasks") or []
        return not tasks or "CREATE_CONTENT" in tasks

    def status_view(request: Request) -> dict[str, Any]:
        data = tokens.load()
        selected = tokens.selected_page()
        return {
            "client_configured": fb.configured,
            "connected": bool(data),
            "redirect_uri": f"{request.url.scheme}://{request.url.netloc}/api/facebook/callback",
            "pages": page_views(data),
            "selected_page": {"id": selected["id"], "name": selected["name"]} if selected else None,
            "daily_limit": DAILY_LIMIT,
            "used_24h": posts.used_last_24h(selected["id"]) if selected else 0,
            "api_version": fb.api_version,
        }

    def safe_return_to(request: Request, value: str | None) -> str:
        default = f"{request.url.scheme}://{request.url.netloc}/ui/"
        if not value:
            return default
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or (parsed.hostname or "") not in allowed_hosts():
            return default
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"

    @app.get("/api/facebook/status")
    def facebook_status(request: Request) -> dict[str, Any]:
        return status_view(request)

    @app.post("/api/facebook/connect")
    def facebook_connect(request: Request, body: ConnectBody) -> dict[str, Any]:
        local_guard(request)
        fb.credentials()
        redirect_uri = f"{request.url.scheme}://{request.url.netloc}/api/facebook/callback"
        state = secrets.token_urlsafe(32)
        with lock:
            cutoff = time.monotonic() - FLOW_TTL_SECONDS
            for key in [k for k, v in pending.items() if v["at"] <= cutoff]:
                pending.pop(key, None)
            while len(pending) >= MAX_PENDING_FLOWS:
                pending.pop(next(iter(pending)))
            pending[state] = {
                "redirect_uri": redirect_uri,
                "return_to": safe_return_to(request, body.return_to),
                "at": time.monotonic(),
            }
        return {"url": fb_client.auth_url(fb, redirect_uri, state), "redirect_uri": redirect_uri}

    @app.get("/api/facebook/callback")
    def facebook_callback(
        request: Request,
        state: str = "",
        code: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        # Only the Host is checked: this is a top-level redirect from Facebook with no useful
        # Origin. The one-time random `state` proves the sign-in began in this app.
        if (request.url.hostname or "").lower() not in allowed_hosts():
            raise AIError(ErrorCategory.POLICY_VIOLATION, "Unexpected host name.", retryable=False)
        with lock:
            flow = pending.pop(state, None) if state else None
        if flow is None or time.monotonic() - flow["at"] > FLOW_TTL_SECONDS:
            raise AIError(
                ErrorCategory.AUTHENTICATION_FAILED,
                "This sign-in link is not valid or has expired. Start again.",
                provider="facebook",
                retryable=False,
            )
        outcome = {"fb": "ok"}
        if error or not code:
            outcome = {"fb": "denied"}
        else:
            try:
                api = client()
                user_token = api.exchange_code(code, flow["redirect_uri"])
                pages = api.list_pages(user_token)
                previous = tokens.load() or {}
                keep = previous.get("selected_page_id")
                ids = {p["id"] for p in pages}
                selected = keep if keep in ids else (pages[0]["id"] if len(pages) == 1 else None)
                tokens.save(
                    {
                        "user_token": user_token,
                        "pages": pages,
                        "selected_page_id": selected,
                        "connected_at": datetime.now(UTC).isoformat(),
                    }
                )
                if not pages:
                    outcome = {"fb": "nopages"}
            except AIError:
                outcome = {"fb": "failed"}
        return RedirectResponse(
            f"{flow['return_to']}#/pipeline?{urlencode(outcome)}",
            status_code=303,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @app.post("/api/facebook/pages/refresh")
    def facebook_refresh_pages(request: Request) -> dict[str, Any]:
        local_guard(request)
        data = tokens.load()
        if not data:
            raise _invalid("Connect Facebook first.")
        pages = client().list_pages(data["user_token"])
        ids = {p["id"] for p in pages}
        data["pages"] = pages
        if data.get("selected_page_id") not in ids:
            data["selected_page_id"] = pages[0]["id"] if len(pages) == 1 else None
        tokens.save(data)
        return status_view(request)

    @app.put("/api/facebook/page")
    def facebook_select_page(request: Request, body: PageBody) -> dict[str, Any]:
        local_guard(request)
        if tokens.select(body.page_id) is None:
            raise _invalid("That Page is not in your connected list. Refresh the Pages first.")
        return status_view(request)

    @app.post("/api/facebook/disconnect")
    def facebook_disconnect(request: Request) -> dict[str, Any]:
        local_guard(request)
        removed = tokens.delete()
        return {"removed": removed, **status_view(request)}

    @app.get("/api/facebook/clips/{clip_id}")
    def facebook_clip_state(request: Request, clip_id: str) -> dict[str, Any]:
        request.app.state.clips.get(clip_id)  # 404 for an unknown clip
        return {
            "clip_id": clip_id,
            "post": _public_post(posts.get(clip_id)),
            "in_progress": inflight.active(clip_id),
        }

    @app.post("/api/facebook/clips/{clip_id}/publish")
    def facebook_publish(request: Request, clip_id: str, body: PublishBody) -> dict[str, Any]:
        local_guard(request)
        if not body.confirm:
            raise _invalid("Confirm before publishing. Nothing was sent to Facebook.")
        if body.mode not in {"now", "schedule"}:
            raise _invalid("mode must be now or schedule.")
        fb.credentials()
        page = tokens.selected_page()
        if not tokens.load():
            raise _invalid("Facebook is not connected yet. Use Connections on the Pipeline page.")
        if page is None:
            raise _invalid("Choose a Facebook Page first (Connections on the Pipeline page).")
        if not _can_post(page):
            raise _invalid(
                "Your Facebook account cannot create content on this Page. Pick another Page "
                "or ask the Page owner for access."
            )
        clips = request.app.state.clips
        clip = clips.get(clip_id)
        if clip.deleted_at is not None:
            raise AIError(
                ErrorCategory.NOT_FOUND,
                "This clip is in the trash. Restore it before posting.",
                provider="facebook",
                retryable=False,
            )
        publish_at: datetime | None = None
        if body.mode == "schedule":
            if not body.publish_at:
                raise _invalid("Pick a publish time.")
            publish_at = parse_publish_at(body.publish_at)
            now = datetime.now(UTC)
            if publish_at < now + timedelta(minutes=MIN_SCHEDULE_MINUTES):
                raise _invalid(
                    f"The time must be at least {MIN_SCHEDULE_MINUTES} minutes from now."
                )
            if publish_at > now + timedelta(days=MAX_SCHEDULE_DAYS):
                raise _invalid(f"The time must be within {MAX_SCHEDULE_DAYS} days from now.")
        elif body.publish_at:
            raise _invalid("A time is only used when mode is schedule.")
        description = body.description
        if description is None:
            text = clip.description or ""
            tag_line = " ".join(f"#{t.lstrip('#')}" for t in clip.hashtags)
            description = f"{text}\n\n{tag_line}".strip() if tag_line else text
        description = description.strip()
        if len(description) > MAX_DESCRIPTION:
            raise _invalid(f"The description is longer than {MAX_DESCRIPTION} characters.")
        if posts.get(clip_id):
            raise _invalid("This clip was already sent to Facebook. It is not sent twice.")
        used = posts.used_last_24h(page["id"])
        if used >= DAILY_LIMIT:
            raise AIError(
                ErrorCategory.RATE_LIMIT_EXCEEDED,
                f"Facebook allows {DAILY_LIMIT} API Reels per Page per 24 hours and this app "
                f"already sent {used}. Try again later.",
                provider="facebook",
                retryable=False,
            )
        if not inflight.claim(clip_id):
            raise _invalid("This clip is already being sent. Wait for it to finish.")
        try:
            path = clips.output_path(clip_id)
            result = client().publish_reel(
                page_id=page["id"],
                page_token=page["access_token"],
                path=path,
                description=description,
                publish_at=publish_at,
            )
            record = {
                "video_id": result.get("video_id"),
                "page_id": page["id"],
                "page_name": page["name"],
                "mode": body.mode,
                "publish_at": publish_at.isoformat() if publish_at else None,
                "sent_at": datetime.now(UTC).isoformat(),
            }
            posts.put(clip_id, record)
        finally:
            inflight.release(clip_id)
        return {"clip_id": clip_id, "post": _public_post(record)}
