"""Publish state of each clip, and the plan for "Auto-publish ready clips".

Auto-publish never makes anything public right away. Every planned clip is uploaded as
private with a publish time (YouTube makes it public at that time). The user sees the plan
and confirms it first. A clip that is already uploaded, marked published by hand, or excluded
is never part of a plan, so one clip is never sent twice and scheduled clips stay marked.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

MAX_BATCH = 5  # one upload costs about 1600 of the 10,000 daily YouTube quota units
MIN_LEAD = timedelta(minutes=30)
HORIZON_DAYS = 60


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def publish_state(record: dict[str, Any] | None, *, manual_published: bool, now: datetime) -> str:
    """not_uploaded | manual | scheduled | went_live | public | unlisted | private."""
    if not record:
        return "manual" if manual_published else "not_uploaded"
    when = _parse(record.get("publish_at"))
    if when is not None:
        return "scheduled" if when > now else "went_live"
    privacy = record.get("privacy")
    return privacy if privacy in {"public", "unlisted", "private"} else "private"


def plan_slots(
    *,
    now: datetime,
    uploads: dict[str, dict[str, Any]],
    count: int,
    hour: int,
    minute: int,
    per_day: int,
) -> list[datetime]:
    """Next free publish times: `per_day` per calendar day at hour:minute, in now's timezone.
    Days that already have scheduled releases only get the missing ones."""
    per_day = max(1, min(per_day, 5))
    taken: dict[str, int] = {}
    for record in uploads.values():
        when = _parse(record.get("publish_at"))
        if when is not None and when > now:
            key = when.astimezone(now.tzinfo).date().isoformat()
            taken[key] = taken.get(key, 0) + 1
    slots: list[datetime] = []
    day = now.date()
    for _ in range(HORIZON_DAYS):
        slot = datetime.combine(day, time(hour, minute), tzinfo=now.tzinfo)
        if slot >= now + MIN_LEAD:
            free = per_day - taken.get(day.isoformat(), 0)
            for _i in range(max(0, free)):
                if len(slots) < count:
                    slots.append(slot + timedelta(minutes=len(slots) % per_day))
        if len(slots) >= count:
            break
        day += timedelta(days=1)
    return slots
