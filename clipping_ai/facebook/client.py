"""Minimal Facebook Graph API client: sign-in, list Pages, publish a Reel from a local file.

Reel publishing follows Meta's three steps for the video_reels edge: start, upload the bytes
to the returned upload URL, finish. Meta can change this; errors show Meta's own short
message so a change is visible instead of hidden.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.facebook.config import SCOPES, FacebookSettings

DIALOG_URL = "https://www.facebook.com/{version}/dialog/oauth"
UPLOAD_HOST = "rupload.facebook.com"


def _meta_error(response: httpx.Response, what: str) -> AIError:
    message = ""
    code = ""
    try:
        err = response.json().get("error", {})
        message = str(err.get("message") or "")[:200]
        code = str(err.get("code") or "")
    except (ValueError, AttributeError):
        pass
    status = response.status_code
    if status in (401, 403) or code in {"190", "10", "200", "102"}:
        category = ErrorCategory.AUTHENTICATION_FAILED
        text = (
            f"Facebook refused the {what} request. Connect Facebook again in Connections, and "
            f"check you are an admin of the Page and of the Meta app. Facebook said: "
            f"{message or status}"
        )
    elif status == 429 or code in {"4", "17", "32", "613"}:
        category = ErrorCategory.RATE_LIMIT_EXCEEDED
        text = f"Facebook is rate limiting the {what} requests. Try again later."
    elif 400 <= status < 500:
        category = ErrorCategory.INVALID_REQUEST
        text = f"Facebook rejected the {what} request: {message or status}"
    else:
        category = ErrorCategory.PROVIDER_UNAVAILABLE
        text = f"Facebook answered the {what} request with status {status}."
    return AIError(
        category,
        text,
        provider="facebook",
        retryable=status >= 500,
        details={"http_status": status, "code": code},
    )


def auth_url(settings: FacebookSettings, redirect_uri: str, state: str) -> str:
    app_id, _ = settings.credentials()
    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
        "scope": ",".join(SCOPES),
    }
    return f"{DIALOG_URL.format(version=settings.api_version)}?{urlencode(params)}"


class FacebookClient:
    def __init__(self, settings: FacebookSettings, *, http: httpx.Client | None = None) -> None:
        self.settings = settings
        self._http = http or httpx.Client(timeout=httpx.Timeout(60.0, read=300.0))

    def _send(self, method: str, url: str, what: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise AIError(
                ErrorCategory.PROVIDER_UNAVAILABLE,
                f"Could not reach Facebook ({what}). Check the internet connection.",
                provider="facebook",
                retryable=True,
                cause=exc,
            ) from exc
        if response.status_code != 200:
            raise _meta_error(response, what)
        try:
            data = response.json()
        except ValueError as exc:
            raise AIError(
                ErrorCategory.INVALID_RESPONSE,
                f"Facebook sent an unreadable answer ({what}).",
                provider="facebook",
                retryable=False,
            ) from exc
        return data if isinstance(data, dict) else {}

    def exchange_code(self, code: str, redirect_uri: str) -> str:
        """Code -> short-lived user token -> long-lived user token."""
        app_id, secret = self.settings.credentials()
        base = self.settings.graph_url()
        short = self._send(
            "GET",
            f"{base}/oauth/access_token",
            "sign-in",
            params={
                "client_id": app_id,
                "client_secret": secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )
        token = short.get("access_token")
        if not token:
            raise AIError(
                ErrorCategory.INVALID_RESPONSE,
                "Facebook did not return an access token.",
                provider="facebook",
                retryable=False,
            )
        long_lived = self._send(
            "GET",
            f"{base}/oauth/access_token",
            "sign-in",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": secret,
                "fb_exchange_token": token,
            },
        )
        return str(long_lived.get("access_token") or token)

    def list_pages(self, user_token: str) -> list[dict[str, Any]]:
        """Pages this user manages, each with its own Page token (needed to publish)."""
        base = self.settings.graph_url()
        pages: list[dict[str, Any]] = []
        url = f"{base}/me/accounts"
        params: dict[str, Any] | None = {
            "fields": "id,name,access_token,tasks",
            "limit": 100,
            "access_token": user_token,
        }
        for _ in range(5):
            data = self._send("GET", url, "Pages", params=params)
            for row in data.get("data", []):
                if row.get("id") and row.get("access_token"):
                    pages.append(
                        {
                            "id": str(row["id"]),
                            "name": str(row.get("name") or row["id"]),
                            "access_token": str(row["access_token"]),
                            "tasks": [str(t) for t in row.get("tasks", [])],
                        }
                    )
            nxt = data.get("paging", {}).get("next")
            if not nxt:
                break
            url, params = nxt, None
        return pages

    def publish_reel(
        self,
        *,
        page_id: str,
        page_token: str,
        path: Path,
        description: str,
        publish_at: datetime | None,
    ) -> dict[str, Any]:
        """Send one Reel. publish_at=None publishes now; a time schedules it on Meta's side."""
        size = path.stat().st_size
        if size <= 0:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "The clip file is empty.",
                provider="facebook",
                retryable=False,
            )
        base = self.settings.graph_url()
        edge = f"{base}/{page_id}/video_reels"
        started = self._send(
            "POST",
            edge,
            "Reel start",
            data={"upload_phase": "start", "access_token": page_token},
        )
        video_id = str(started.get("video_id") or "")
        upload_url = str(started.get("upload_url") or "")
        if not video_id:
            raise AIError(
                ErrorCategory.INVALID_RESPONSE,
                "Facebook did not return a video id for the Reel.",
                provider="facebook",
                retryable=False,
            )
        if not upload_url:
            upload_url = (
                f"https://{UPLOAD_HOST}/video-upload/{self.settings.api_version}/{video_id}"
            )
        with path.open("rb") as handle:
            self._send(
                "POST",
                upload_url,
                "Reel upload",
                headers={
                    "Authorization": f"OAuth {page_token}",
                    "offset": "0",
                    "file_size": str(size),
                },
                content=handle.read(),
            )
        finish: dict[str, Any] = {
            "upload_phase": "finish",
            "video_id": video_id,
            "description": description,
            "access_token": page_token,
        }
        if publish_at is None:
            finish["video_state"] = "PUBLISHED"
        else:
            finish["video_state"] = "SCHEDULED"
            finish["scheduled_publish_time"] = int(publish_at.astimezone(UTC).timestamp())
        done = self._send("POST", edge, "Reel finish", data=finish)
        if done.get("success") is False:
            raise AIError(
                ErrorCategory.PROVIDER_UNAVAILABLE,
                "Facebook did not accept the Reel. Check the Page in Facebook before trying "
                "again, so a clip is never posted twice.",
                provider="facebook",
                retryable=False,
            )
        return {"video_id": video_id}
