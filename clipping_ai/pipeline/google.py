"""Read-only Google access for the pipeline: a private Google Sheet and YouTube Analytics.

Two separate sign-ins, each with one read-only scope, each in its own token file under the
data directory (git-ignored, mode 0600). Neither scope can upload, publish, edit or delete
anything. They reuse the OAuth client already set up for YouTube uploads. Tokens and secrets
are never logged, returned by the API or shown in the UI.
"""

from __future__ import annotations

import csv
import io
import re
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.pipeline.sheet import SheetRow, parse_rows
from clipping_ai.youtube import auth
from clipping_ai.youtube.config import YouTubeSettings

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
YOUTUBE_READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
# One sign-in per kind. "analytics" also reads the channel's own video list (read-only) so the
# tool can see what is already scheduled. "upload" is the only scope that can publish.
SCOPES = {
    "sheets": SHEETS_SCOPE,
    "analytics": f"{ANALYTICS_SCOPE} {YOUTUBE_READ_SCOPE}",
    "upload": UPLOAD_SCOPE,
}
GLOBAL_KINDS = {"sheets"}  # one Google account for sheets; analytics and upload are per channel
DEFAULT_CHANNEL = "default"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
ANALYTICS_API = "https://youtubeanalytics.googleapis.com/v2/reports"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
_SHEET_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
_CHANNEL_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
MAX_ROWS = 2000
METRICS = "views,estimatedMinutesWatched,averageViewDuration,likes,comments,subscribersGained"
FLOW_TTL_SECONDS = 600
MAX_PENDING_FLOWS = 8
SCHEDULE_PAGES = 6  # up to 300 most recent uploads; scheduled videos are the newest ones


def valid_channel_id(channel_id: str) -> bool:
    return bool(_CHANNEL_ID.match(channel_id))


def token_path(
    data_dir: Path,
    kind: str,
    channel_id: str = DEFAULT_CHANNEL,
    yt: YouTubeSettings | None = None,
) -> Path:
    """Where one sign-in is stored. The default channel keeps the paths used before channels
    existed, so earlier sign-ins keep working."""
    if kind not in SCOPES:
        raise ValueError(kind)
    if not valid_channel_id(channel_id):
        raise ValueError(channel_id)
    if kind in GLOBAL_KINDS:
        return (data_dir / "google" / f"{kind}_token.json").resolve()
    if channel_id == DEFAULT_CHANNEL:
        if kind == "upload":
            return (yt or YouTubeSettings()).resolved_token_path(data_dir)
        return (data_dir / "google" / f"{kind}_token.json").resolve()
    return (data_dir / "google" / "channels" / channel_id / f"{kind}_token.json").resolve()


def is_authorised(
    data_dir: Path,
    kind: str,
    channel_id: str = DEFAULT_CHANNEL,
    yt: YouTubeSettings | None = None,
) -> bool:
    token = auth.load_token(token_path(data_dir, kind, channel_id, yt))
    return bool(token and token.get("refresh_token"))


def can_read_schedule(
    data_dir: Path, channel_id: str = DEFAULT_CHANNEL, yt: YouTubeSettings | None = None
) -> bool:
    """True when the analytics sign-in of this channel includes reading its video list.
    Sign-ins made before this feature only have the analytics scope: sign in again."""
    token = auth.load_token(token_path(data_dir, "analytics", channel_id, yt))
    if not (token and token.get("refresh_token")):
        return False
    return YOUTUBE_READ_SCOPE in str(token.get("scope", "")).split()


def forget(
    data_dir: Path, kind: str, channel_id: str = DEFAULT_CHANNEL, yt: YouTubeSettings | None = None
) -> bool:
    """Delete the saved token on this computer. It does not revoke access at Google."""
    path = token_path(data_dir, kind, channel_id, yt)
    if path.is_file():
        path.unlink()
        return True
    return False


def _auth_url(client_id: str, redirect_uri: str, kind: str, state: str, challenge: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES[kind],
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{auth.AUTH_URL}?{urlencode(params)}"


def _exchange(
    settings: YouTubeSettings,
    kind: str,
    data_dir: Path,
    channel_id: str,
    code: str,
    verifier: str,
    redirect_uri: str,
    http: httpx.Client | None,
) -> None:
    client_id, client_secret = settings.credentials()
    owns = http is None
    client = http or httpx.Client()
    try:
        payload = auth._post_token(
            client,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
    finally:
        if owns:
            client.close()
    record = auth._token_record(payload)
    record["scope"] = SCOPES[kind]
    auth.save_token(token_path(data_dir, kind, channel_id, settings), record)


def authorize(
    kind: str,
    settings: YouTubeSettings,
    data_dir: Path,
    *,
    channel_id: str = DEFAULT_CHANNEL,
    http: httpx.Client | None = None,
    open_url: Callable[[str], Any] | None = None,
    announce: Callable[[str], None] = print,
) -> None:
    """Terminal sign-in: browser consent on this computer. The web UI does the same with
    `WebConnect`; this stays as a fallback."""
    client_id, _ = settings.credentials()
    verifier, challenge = auth.make_pkce()
    state = secrets.token_urlsafe(24)
    server = auth._CallbackServer()
    url = _auth_url(client_id, server.redirect_uri, kind, state, challenge)
    announce(f"Open this URL to give {kind} access (this scope only):")
    announce(url)
    (open_url or webbrowser.open)(url)
    result = server.wait(auth.CALLBACK_TIMEOUT_SECONDS)
    if result.get("error"):
        raise auth._auth_error(f"Authorisation was not granted ({result['error']}).")
    if not result.get("code"):
        raise auth._auth_error("Timed out waiting for the browser authorisation.", retryable=True)
    if not secrets.compare_digest(result.get("state", ""), state):
        raise auth._auth_error("The authorisation response did not match this request.")
    _exchange(
        settings, kind, data_dir, channel_id, result["code"], verifier, server.redirect_uri, http
    )


class WebConnect:
    """Sign-in started from the web UI. The redirect comes back to this same local API.

    The PKCE verifier and the random `state` live only in memory. A state works once and
    expires after ten minutes, so a stray or replayed callback cannot save a token.
    """

    def __init__(self, now: Callable[[], float] | None = None) -> None:
        self._now = now or (lambda: time.monotonic())
        self._lock = threading.Lock()
        self._pending: dict[str, dict[str, Any]] = {}

    def start(
        self,
        *,
        kind: str,
        channel_id: str,
        redirect_uri: str,
        return_to: str,
        settings: YouTubeSettings,
    ) -> str:
        if kind not in SCOPES or not valid_channel_id(channel_id):
            raise AIError(ErrorCategory.INVALID_REQUEST, "Unknown sign-in type.", retryable=False)
        client_id, _ = settings.credentials()
        verifier, challenge = auth.make_pkce()
        state = secrets.token_urlsafe(32)
        with self._lock:
            cutoff = self._now() - FLOW_TTL_SECONDS
            self._pending = {k: v for k, v in self._pending.items() if v["at"] > cutoff}
            while len(self._pending) >= MAX_PENDING_FLOWS:
                self._pending.pop(next(iter(self._pending)))
            self._pending[state] = {
                "kind": kind,
                "channel_id": channel_id,
                "verifier": verifier,
                "redirect_uri": redirect_uri,
                "return_to": return_to,
                "at": self._now(),
            }
        return _auth_url(client_id, redirect_uri, kind, state, challenge)

    def finish(
        self,
        *,
        state: str,
        code: str | None,
        error: str | None,
        settings: YouTubeSettings,
        data_dir: Path,
        http: httpx.Client | None = None,
    ) -> dict[str, str]:
        with self._lock:
            flow = self._pending.pop(state, None) if state else None
        if flow is None or self._now() - flow["at"] > FLOW_TTL_SECONDS:
            raise auth._auth_error("This sign-in link is not valid or has expired. Start again.")
        info = {
            "kind": flow["kind"],
            "channel_id": flow["channel_id"],
            "return_to": flow["return_to"],
        }
        if error or not code:
            raise ConnectDenied(info, "denied")
        try:
            _exchange(
                settings,
                flow["kind"],
                data_dir,
                flow["channel_id"],
                code,
                flow["verifier"],
                flow["redirect_uri"],
                http,
            )
        except AIError as exc:
            raise ConnectDenied(info, "failed") from exc
        return info


class ConnectDenied(Exception):
    """The user declined or the code exchange failed. Carries where to send the browser and a
    short reason code ("denied" or "failed"). Nothing from Google's reply is passed on."""

    def __init__(self, info: dict[str, str], reason: str) -> None:
        super().__init__(reason)
        self.info = info
        self.reason = reason


def _google_error(response: httpx.Response, what: str) -> AIError:
    reason = ""
    try:
        errors = response.json().get("error", {})
        reason = str(errors.get("status") or errors.get("message") or "")[:120]
    except (ValueError, AttributeError):
        pass
    status = response.status_code
    if status == 401:
        message = f"Google rejected the {what} access token. Sign in again (see PIPELINE.md)."
        category = ErrorCategory.AUTHENTICATION_FAILED
    elif status == 403:
        message = (
            f"Google refused the {what} request. Make sure the API is enabled in your Google "
            f"Cloud project and this account can open it ({reason or 'forbidden'})."
        )
        category = ErrorCategory.AUTHENTICATION_FAILED
    elif status == 404:
        message = f"Google could not find that {what} (check the link and the account)."
        category = ErrorCategory.NOT_FOUND
    elif status == 429:
        message = f"Google is rate limiting the {what} requests. Try again later."
        category = ErrorCategory.RATE_LIMIT_EXCEEDED
    else:
        message = f"Google answered the {what} request with status {status}."
        category = (
            ErrorCategory.PROVIDER_UNAVAILABLE if status >= 500 else ErrorCategory.INVALID_REQUEST
        )
    return AIError(category, message, provider="google", retryable=status >= 500)


def _get(http: httpx.Client, url: str, token: str, what: str, params: dict[str, str] | None = None):
    try:
        response = http.get(
            url, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=30.0
        )
    except httpx.HTTPError as exc:
        raise AIError(
            ErrorCategory.PROVIDER_UNAVAILABLE,
            f"Could not reach Google ({what}). Check the internet connection.",
            provider="google",
            retryable=True,
        ) from exc
    if response.status_code != 200:
        raise _google_error(response, what)
    return response.json()


def sheet_ref(sheet_url: str) -> tuple[str, str | None]:
    """(spreadsheet id, gid). Only docs.google.com links are accepted."""
    parsed = urlparse(sheet_url.strip())
    if parsed.scheme != "https" or parsed.hostname != "docs.google.com":
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "Use a https://docs.google.com/spreadsheets/... link.",
            provider="sheet",
            retryable=False,
        )
    match = _SHEET_ID.search(parsed.path)
    if not match:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "Private sheets need the normal sheet link (…/spreadsheets/d/ID/…), "
            "not a published-to-web link.",
            provider="sheet",
            retryable=False,
        )
    gid = (parse_qs(parsed.query).get("gid") or parse_qs(parsed.fragment).get("gid") or [None])[0]
    return match.group(1), gid if gid and gid.isdigit() else None


def fetch_private_rows(
    sheet_url: str, token_provider: Callable[[], str], *, http: httpx.Client | None = None
) -> list[SheetRow]:
    """Read a private sheet with the signed-in account (read-only) and find the links in it."""
    sheet_id, gid = sheet_ref(sheet_url)
    owns = http is None
    client = http or httpx.Client()
    try:
        token = token_provider()
        meta = _get(
            client,
            f"{SHEETS_API}/{sheet_id}",
            token,
            "sheet",
            {"fields": "sheets.properties(sheetId,title)"},
        )
        sheets = [s.get("properties", {}) for s in meta.get("sheets", [])]
        if not sheets:
            raise AIError(
                ErrorCategory.INVALID_RESPONSE,
                "The sheet has no tabs.",
                provider="sheet",
                retryable=False,
            )
        chosen = next(
            (s for s in sheets if gid is not None and str(s.get("sheetId")) == gid), sheets[0]
        )
        title = str(chosen.get("title", "")).replace("'", "''")
        rng = quote(f"'{title}'", safe="")
        values = _get(
            client,
            f"{SHEETS_API}/{sheet_id}/values/{rng}",
            token,
            "sheet",
            {"majorDimension": "ROWS", "valueRenderOption": "FORMATTED_VALUE"},
        )
    finally:
        if owns:
            client.close()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in (values.get("values") or [])[: MAX_ROWS + 1]:
        writer.writerow([str(cell) for cell in row])
    return parse_rows(buffer.getvalue())


def fetch_video_analytics(
    video_ids: list[str],
    token_provider: Callable[[], str],
    *,
    start: date,
    end: date,
    http: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Per-video numbers for the signed-in channel. Read-only. Invalid ids are dropped."""
    ids = [v for v in dict.fromkeys(video_ids) if _VIDEO_ID.match(v)]
    if not ids:
        return []
    owns = http is None
    client = http or httpx.Client()
    out: list[dict[str, Any]] = []
    try:
        token = token_provider()
        for i in range(0, len(ids), 50):
            batch = ids[i : i + 50]
            payload = _get(
                client,
                ANALYTICS_API,
                token,
                "analytics",
                {
                    "ids": "channel==MINE",
                    "startDate": start.isoformat(),
                    "endDate": end.isoformat(),
                    "metrics": METRICS,
                    "dimensions": "video",
                    "filters": "video==" + ",".join(batch),
                    "sort": "-views",
                    "maxResults": "50",
                },
            )
            names = [h.get("name") for h in payload.get("columnHeaders", [])]
            for row in payload.get("rows") or []:
                data = dict(zip(names, row, strict=False))
                out.append(
                    {
                        "video_id": str(data.get("video")),
                        "views": int(data.get("views") or 0),
                        "minutes_watched": float(data.get("estimatedMinutesWatched") or 0),
                        "avg_view_seconds": float(data.get("averageViewDuration") or 0),
                        "likes": int(data.get("likes") or 0),
                        "comments": int(data.get("comments") or 0),
                        "subscribers_gained": int(data.get("subscribersGained") or 0),
                        "window_start": start.isoformat(),
                        "window_end": end.isoformat(),
                    }
                )
    finally:
        if owns:
            client.close()
    return out


def _parse_when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def fetch_channel_schedule(
    token_provider: Callable[[], str],
    *,
    now: datetime,
    http: httpx.Client | None = None,
) -> dict[str, Any]:
    """The signed-in channel's own videos that are scheduled to go public later. Read-only.

    Reads the channel's uploads list (private videos included, because the owner is signed in)
    and keeps the ones with a future `publishAt`, whoever uploaded them: this tool, YouTube
    Studio or the phone app. Returns {"channel": {...}, "videos": [...]}.
    """
    owns = http is None
    client = http or httpx.Client()
    try:
        token = token_provider()
        mine = _get(
            client,
            f"{YOUTUBE_API}/channels",
            token,
            "channel",
            {"part": "snippet,contentDetails", "mine": "true"},
        )
        items = mine.get("items") or []
        if not items:
            raise AIError(
                ErrorCategory.NOT_FOUND,
                "This Google account has no YouTube channel. Pick the channel when you sign in.",
                provider="youtube",
                retryable=False,
            )
        channel = items[0]
        playlist = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        info = {
            "id": str(channel.get("id", "")),
            "title": str(channel.get("snippet", {}).get("title", "")),
        }
        ids: list[str] = []
        page_token: str | None = None
        for _ in range(SCHEDULE_PAGES if playlist else 0):
            params = {"part": "contentDetails", "playlistId": playlist, "maxResults": "50"}
            if page_token:
                params["pageToken"] = page_token
            page = _get(client, f"{YOUTUBE_API}/playlistItems", token, "channel videos", params)
            ids.extend(
                str(i.get("contentDetails", {}).get("videoId"))
                for i in page.get("items", [])
                if i.get("contentDetails", {}).get("videoId")
            )
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        videos: list[dict[str, Any]] = []
        for i in range(0, len(ids), 50):
            batch = ids[i : i + 50]
            payload = _get(
                client,
                f"{YOUTUBE_API}/videos",
                token,
                "channel videos",
                {"part": "snippet,status", "id": ",".join(batch), "maxResults": "50"},
            )
            for item in payload.get("items", []):
                when = _parse_when(item.get("status", {}).get("publishAt"))
                if when is None or when <= now:
                    continue
                videos.append(
                    {
                        "video_id": str(item.get("id")),
                        "title": str(item.get("snippet", {}).get("title", ""))[:200],
                        "publish_at": when.isoformat(),
                    }
                )
    finally:
        if owns:
            client.close()
    videos.sort(key=lambda v: v["publish_at"])
    return {"channel": info, "videos": videos}
