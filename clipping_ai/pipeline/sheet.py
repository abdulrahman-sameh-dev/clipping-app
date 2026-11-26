"""Read video links from a Google Sheet that is shared as "anyone with the link can view".

Only https://docs.google.com is fetched, so the sheet URL cannot point at other hosts.
No Google login or secret is used. Private sheets are not supported.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from clipping_ai.errors import AIError, ErrorCategory

MAX_BYTES = 2_000_000
MAX_ROWS = 2000
_SHEET_ID = re.compile(r"/spreadsheets/d/(?:e/)?([A-Za-z0-9_-]+)")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_LINK_HEADERS = {"url", "link", "video", "video url", "video link", "رابط", "الرابط", "لينك"}
_TITLE_HEADERS = {"title", "name", "عنوان", "العنوان", "الاسم"}
_NOTE_HEADERS = {"notes", "note", "comment", "ملاحظات", "ملاحظة"}


@dataclass(frozen=True)
class SheetRow:
    url: str
    title: str | None
    notes: str | None


def _bad(message: str) -> AIError:
    return AIError(ErrorCategory.INVALID_REQUEST, message, provider="sheet", retryable=False)


def csv_export_url(sheet_url: str) -> str:
    """Turn a Google Sheets link into its CSV export link. Rejects any other host."""
    parsed = urlparse(sheet_url.strip())
    if parsed.scheme != "https" or parsed.hostname != "docs.google.com":
        raise _bad("Use a https://docs.google.com/spreadsheets/... link.")
    match = _SHEET_ID.search(parsed.path)
    if not match:
        raise _bad("That does not look like a Google Sheets link.")
    sheet_id = match.group(1)
    query = parse_qs(parsed.query)
    fragment = parse_qs(parsed.fragment)
    gid = (query.get("gid") or fragment.get("gid") or [None])[0]
    if "/spreadsheets/d/e/" in parsed.path:
        url = f"https://docs.google.com/spreadsheets/d/e/{sheet_id}/pub?output=csv"
    else:
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    if gid and gid.isdigit():
        url += f"&gid={gid}"
    return url


_YT_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"}
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
_TRACKING = ("utm_", "fbclid", "gclid", "si", "feature", "pp", "ab_channel", "igshid")


def canonical_key(url: str) -> str:
    """A key that is the same for different spellings of the same video link.

    youtu.be/ID, youtube.com/watch?v=ID&utm_x=1, /shorts/ID and /embed/ID all give
    `youtube:ID`. Other links lose the fragment, tracking parameters, `www.` and a trailing
    slash, and their host is lower-cased.
    """
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path or ""
    query = parse_qs(parsed.query)
    video_id = None
    if host == "youtu.be":
        video_id = path.strip("/").split("/")[0]
    elif host in _YT_HOSTS:
        if path == "/watch":
            video_id = (query.get("v") or [""])[0]
        else:
            parts = [p for p in path.split("/") if p]
            if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
                video_id = parts[1]
    if video_id and _YT_ID.match(video_id):
        return f"youtube:{video_id}"
    kept = sorted(
        (k, v)
        for k, vals in query.items()
        for v in vals
        if not k.lower().startswith(_TRACKING) and k.lower() not in _TRACKING
    )
    key = f"{host}{path.rstrip('/')}"
    if kept:
        key += "?" + urlencode(kept)
    return key.lower() if not kept else key


def normalise_video_url(value: str) -> str | None:
    match = _URL.search(value or "")
    if not match:
        return None
    return match.group(0).strip().rstrip(".,;)")


def parse_rows(text: str) -> list[SheetRow]:
    """Find a link in each row. Works with a header row or a Google Form responses sheet."""
    table = [row for row in csv.reader(io.StringIO(text)) if any(c.strip() for c in row)]
    if not table:
        return []
    header = [c.strip().lower() for c in table[0]]
    link_col = next((i for i, h in enumerate(header) if h in _LINK_HEADERS), None)
    title_col = next((i for i, h in enumerate(header) if h in _TITLE_HEADERS), None)
    note_col = next((i for i, h in enumerate(header) if h in _NOTE_HEADERS), None)
    has_header = not any(normalise_video_url(c) for c in table[0])
    body = table[1:] if has_header else table
    rows: list[SheetRow] = []
    seen: set[str] = set()
    for record in body[:MAX_ROWS]:
        url = None
        if link_col is not None and link_col < len(record):
            url = normalise_video_url(record[link_col])
        if url is None:
            for cell in record:
                url = normalise_video_url(cell)
                if url:
                    break
        if not url or url in seen:
            continue
        seen.add(url)

        def cell(i: int | None, record: list[str] = record) -> str | None:
            if i is None or i >= len(record):
                return None
            return record[i].strip()[:300] or None

        rows.append(SheetRow(url=url, title=cell(title_col), notes=cell(note_col)))
    return rows


def fetch_rows(sheet_url: str, *, client: httpx.Client | None = None) -> list[SheetRow]:
    export = csv_export_url(sheet_url)
    owns = client is None
    http = client or httpx.Client(timeout=20, follow_redirects=True)
    try:
        response = http.get(export)
    except httpx.HTTPError as exc:
        raise AIError(
            ErrorCategory.PROVIDER_UNAVAILABLE,
            "Could not reach Google Sheets. Check the internet connection.",
            provider="sheet",
            retryable=True,
        ) from exc
    finally:
        if owns:
            http.close()
    content_type = response.headers.get("content-type", "")
    if response.status_code in (401, 403, 404) or "text/html" in content_type:
        raise _bad(
            "The sheet is not readable. Share it as 'Anyone with the link can view' "
            "(or publish it to the web) and try again."
        )
    if response.status_code >= 400:
        raise _bad(f"Google Sheets answered with status {response.status_code}.")
    if len(response.content) > MAX_BYTES:
        raise _bad("The sheet is too large.")
    return parse_rows(response.text)
