"""Daily review: plain facts and suggestions computed from local data. Suggest only."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

SCHEDULE_DAYS = 14
STALE_SYNC_HOURS = 24


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def build_review(
    *,
    now: datetime,
    source: dict[str, Any],
    counts: dict[str, int],
    uploads: dict[str, dict[str, Any]],
    ready_clip_ids: list[str],
    failed_clips: int,
    schedule_known: bool | None = None,
) -> dict[str, Any]:
    """`now` must be timezone-aware. Days are in now's timezone."""
    today = now.date()
    days = [today + timedelta(days=i) for i in range(SCHEDULE_DAYS)]
    scheduled = {d.isoformat(): 0 for d in days}
    for record in uploads.values():
        when = _parse(record.get("publish_at"))
        if when is None or when <= now:
            continue
        key = when.astimezone(now.tzinfo).date().isoformat()
        if key in scheduled:
            scheduled[key] += 1
    schedule = [{"date": k, "count": v} for k, v in scheduled.items()]
    empty_days = [row["date"] for row in schedule if row["count"] == 0]
    not_uploaded = [cid for cid in ready_clip_ids if cid not in uploads]

    tips: list[dict[str, Any]] = []
    if schedule_known is False:
        tips.append({"kind": "schedule_unknown", "level": "warn", "count": 0})
    if not source.get("sheet_url"):
        tips.append({"kind": "no_sheet", "level": "info", "count": 0})
    else:
        synced = _parse(source.get("last_sync_at"))
        if source.get("last_error"):
            tips.append({"kind": "sync_error", "level": "warn", "count": 0})
        elif synced is None or now - synced > timedelta(hours=STALE_SYNC_HOURS):
            tips.append({"kind": "stale_sync", "level": "info", "count": 0})
    if counts.get("new", 0):
        tips.append({"kind": "new_links", "level": "action", "count": counts["new"]})
    elif source.get("sheet_url") and not not_uploaded:
        tips.append({"kind": "queue_empty", "level": "info", "count": 0})
    if not_uploaded:
        tips.append({"kind": "ready_clips", "level": "action", "count": len(not_uploaded)})
    if failed_clips:
        tips.append({"kind": "failed_clips", "level": "warn", "count": failed_clips})
    if empty_days and len(empty_days) == SCHEDULE_DAYS:
        tips.append({"kind": "nothing_scheduled", "level": "info", "count": SCHEDULE_DAYS})
    elif empty_days:
        tips.append({"kind": "empty_days", "level": "info", "count": len(empty_days)})
    return {
        "generated_at": now.isoformat(),
        "counts": counts,
        "ready_not_uploaded": len(not_uploaded),
        "failed_clips": failed_clips,
        "schedule": schedule,
        "schedule_known": schedule_known,
        "tips": tips,
    }
