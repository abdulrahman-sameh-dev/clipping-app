"""HTTP routes for the YouTube module.

These routes are served by the same local API as the rest of the app. That API has no
authentication and is meant to listen on localhost only. Anyone who can reach it can use
these routes, so never expose it to a network you do not trust. See YOUTUBE_SETUP.md.

The routes apply the same safety rules as the CLI: the module is off unless enabled in
.env, privacy defaults to private, scheduling is private-only with an explicit UTC offset,
public uploads need explicit confirmation, and a clip that was already uploaded is refused.
No route returns a token, client id or client secret.
"""

from __future__ import annotations

import threading
from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.settings import Settings
from clipping_ai.youtube import auth
from clipping_ai.youtube.client import YouTubeClient
from clipping_ai.youtube.config import YouTubeSettings
from clipping_ai.youtube.models import Privacy, parse_publish_at
from clipping_ai.youtube.service import UploadStore, YouTubeUploadService

STUDIO_PREFIX = "https://studio.youtube.com/"


class YouTubeUploadBody(BaseModel):
    """What the UI may send. There is deliberately no `force` field."""

    model_config = ConfigDict(extra="forbid")

    privacy: Privacy | None = None
    publish_at: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=10000)
    confirm_public: bool = False


class InFlightUploads:
    """Remembers which clips are uploading right now, so one clip is never sent twice."""

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


def _public_record(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Only fields that are safe to show. The upload record holds no credentials anyway."""
    if not record:
        return None
    studio_url = record.get("studio_url")
    if not isinstance(studio_url, str) or not studio_url.startswith(STUDIO_PREFIX):
        studio_url = None
    return {
        "video_id": record.get("video_id"),
        "privacy": record.get("privacy"),
        "publish_at": record.get("publish_at"),
        "uploaded_at": record.get("uploaded_at"),
        "studio_url": studio_url,
    }


def register_youtube_routes(
    app: FastAPI,
    *,
    settings: Settings,
    youtube_settings: YouTubeSettings | None = None,
    uploader: Any | None = None,
) -> None:
    yt = youtube_settings or YouTubeSettings()
    token_path = yt.resolved_token_path(settings.data_dir)
    store = UploadStore(yt.uploads_path(settings.data_dir))
    inflight = InFlightUploads()
    app.state.youtube_inflight = inflight

    def is_authorised() -> bool:
        token = auth.load_token(token_path)
        return bool(token and token.get("refresh_token"))

    @app.get("/api/youtube/status")
    def youtube_status() -> dict[str, Any]:
        return {
            "enabled": yt.enabled,
            "client_configured": bool(yt.client_id and yt.client_secret),
            "authorised": is_authorised(),
            "default_privacy": yt.default_privacy.value,
            "made_for_kids": yt.made_for_kids,
        }

    @app.get("/api/youtube/clips/{clip_id}")
    def youtube_clip_state(request: Request, clip_id: str) -> dict[str, Any]:
        request.app.state.clips.get(clip_id)  # 404 for an unknown clip
        return {
            "clip_id": clip_id,
            "upload": _public_record(store.get(clip_id)),
            "in_progress": inflight.active(clip_id),
        }

    @app.post("/api/youtube/clips/{clip_id}/upload")
    def youtube_upload(
        request: Request, clip_id: str, body: YouTubeUploadBody
    ) -> dict[str, Any]:
        yt.require_enabled()
        yt.credentials()
        if not is_authorised():
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "YouTube is not authorised yet. Run `uv run clipping-ai youtube auth` "
                "on this computer first.",
                provider="youtube",
                retryable=False,
            )
        publish_at = parse_publish_at(body.publish_at) if body.publish_at else None
        service = YouTubeUploadService(
            yt,
            request.app.state.clips,
            uploader or YouTubeClient(lambda: auth.get_access_token(yt, token_path)),
            store,
        )
        upload_request = service.build_request(
            clip_id,
            privacy=body.privacy,
            publish_at=publish_at,
            title=body.title,
            description=body.description,
            confirm_public=body.confirm_public,
        )
        if not inflight.claim(clip_id):
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "This clip is already being uploaded. Wait for it to finish.",
                provider="youtube",
                retryable=False,
            )
        try:
            record = service.upload(upload_request, force=False)
        finally:
            inflight.release(clip_id)
        return {"clip_id": clip_id, "upload": _public_record(record)}
