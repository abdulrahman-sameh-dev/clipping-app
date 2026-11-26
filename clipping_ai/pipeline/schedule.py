"""What is already scheduled on a channel, read from YouTube itself.

The planner must not rely only on what this tool uploaded: videos scheduled in YouTube Studio
or on the phone count too. This module reads them (read-only), keeps a copy in pipeline.db and
merges it with this tool's own upload records so the plan, the daily review and the free-day
count all see the same schedule.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from clipping_ai.errors import AIError
from clipping_ai.pipeline import google
from clipping_ai.pipeline.store import DEFAULT_CHANNEL, PipelineStore
from clipping_ai.youtube import auth
from clipping_ai.youtube.config import YouTubeSettings

RECENT = timedelta(minutes=10)  # a read this new is reused instead of asking YouTube again
KNOWN_FOR = timedelta(hours=6)  # how old a read may be and still be called "known" in the UI


def record_channel(record: dict[str, Any]) -> str:
    return str(record.get("channel_id") or DEFAULT_CHANNEL)


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def is_known(store: PipelineStore, channel_id: str, now: datetime, max_age: timedelta) -> bool:
    channel = store.get_channel(channel_id) or {}
    if channel.get("schedule_error"):
        return False  # the latest read failed, so what we have may be out of date
    fetched = _parse(channel.get("schedule_fetched_at"))
    return fetched is not None and now - fetched <= max_age


def refresh(
    store: PipelineStore,
    data_dir: Path,
    yt: YouTubeSettings,
    channel_id: str,
    *,
    now: datetime,
    http: httpx.Client | None = None,
    force: bool = False,
    token_provider: Callable[[], str] | None = None,
) -> str | None:
    """Read the channel's scheduled videos. Returns an error message, or None when the copy is
    fresh. Never raises: a failed read is recorded and the caller decides what to do."""
    if not google.can_read_schedule(data_dir, channel_id, yt):
        return None
    if not force and is_known(store, channel_id, now, RECENT):
        return None
    path = google.token_path(data_dir, "analytics", channel_id, yt)
    provider = token_provider or (lambda: auth.get_access_token(yt, path, http=http))
    try:
        result = google.fetch_channel_schedule(provider, now=now, http=http)
    except AIError as exc:
        message = exc.message if hasattr(exc, "message") else str(exc)
        store.record_schedule_error(channel_id, message)
        return message
    store.save_remote_schedule(channel_id, result["videos"], result["channel"])
    return None


def merged_uploads(
    store: PipelineStore, uploads: dict[str, dict[str, Any]], channel_id: str
) -> dict[str, dict[str, Any]]:
    """This channel's own upload records plus its videos scheduled on YouTube. A video that this
    tool uploaded appears in both places and is counted once."""
    mine = {cid: r for cid, r in uploads.items() if record_channel(r) == channel_id}
    own_ids = {r.get("video_id") for r in mine.values()}
    merged = dict(mine)
    for video in store.remote_schedule(channel_id):
        if video["video_id"] not in own_ids:
            merged[f"youtube:{video['video_id']}"] = {
                "video_id": video["video_id"],
                "publish_at": video["publish_at"],
                "privacy": "private",
            }
    return merged


def view(store: PipelineStore, data_dir: Path, yt: YouTubeSettings, channel_id: str, now: datetime):
    channel = store.get_channel(channel_id) or {}
    upcoming = [
        v for v in store.remote_schedule(channel_id) if (_parse(v["publish_at"]) or now) > now
    ]
    return {
        "channel_id": channel_id,
        "can_read": google.can_read_schedule(data_dir, channel_id, yt),
        "reconnect_needed": google.is_authorised(data_dir, "analytics", channel_id, yt)
        and not google.can_read_schedule(data_dir, channel_id, yt),
        "known": is_known(store, channel_id, now, KNOWN_FOR),
        "fetched_at": channel.get("schedule_fetched_at"),
        "error": channel.get("schedule_error"),
        "youtube_channel_title": channel.get("youtube_channel_title"),
        "videos": upcoming,
    }
