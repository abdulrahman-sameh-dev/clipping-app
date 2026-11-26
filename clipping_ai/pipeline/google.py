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
import webbrowser
from collections.abc import Callable
from datetime import date
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
SCOPES = {"sheets": SHEETS_SCOPE, "analytics": ANALYTICS_SCOPE}
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
ANALYTICS_API = "https://youtubeanalytics.googleapis.com/v2/reports"
_SHEET_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
MAX_ROWS = 2000
METRICS = "views,estimatedMinutesWatched,averageViewDuration,likes,comments,subscribersGained"


def token_path(data_dir: Path, kind: str) -> Path:
    if kind not in SCOPES:
        raise ValueError(kind)
    return (data_dir / "google" / f"{kind}_token.json").resolve()


def is_authorised(data_dir: Path, kind: str) -> bool:
    token = auth.load_token(token_path(data_dir, kind))
    return bool(token and token.get("refresh_token"))


def authorize(
    kind: str,
    settings: YouTubeSettings,
    data_dir: Path,
    *,
    http: httpx.Client | None = None,
    open_url: Callable[[str], Any] | None = None,
    announce: Callable[[str], None] = print,
) -> None:
    """Browser consent on this computer for one read-only scope. Same PKCE flow as uploads."""
    scope = SCOPES[kind]
    client_id, client_secret = settings.credentials()
    verifier, challenge = auth.make_pkce()
    state = secrets.token_urlsafe(24)
    server = auth._CallbackServer()
    params = {
        "client_id": client_id,
        "redirect_uri": server.redirect_uri,
        "response_type": "code",
        "scope": scope,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{auth.AUTH_URL}?{urlencode(params)}"
    announce(f"Open this URL to give read-only {kind} access (this scope only):")
    announce(url)
    (open_url or webbrowser.open)(url)
    result = server.wait(auth.CALLBACK_TIMEOUT_SECONDS)
    if result.get("error"):
        raise auth._auth_error(f"Authorisation was not granted ({result['error']}).")
    if not result.get("code"):
        raise auth._auth_error("Timed out waiting for the browser authorisation.", retryable=True)
    if not secrets.compare_digest(result.get("state", ""), state):
        raise auth._auth_error("The authorisation response did not match this request.")
    owns = http is None
    client = http or httpx.Client()
    try:
        payload = auth._post_token(
            client,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "code": result["code"],
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": server.redirect_uri,
            },
        )
    finally:
        if owns:
            client.close()
    record = auth._token_record(payload)
    record["scope"] = scope
    auth.save_token(token_path(data_dir, kind), record)


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
