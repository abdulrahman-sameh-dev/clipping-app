"""Day view and analytics-based re-timing of scheduled videos. Pure functions, no network.

Rules, kept simple and explained in PIPELINE.md:
- A past video counts as evidence only when it went public at least 2 days ago and its numbers
  were read. Its score is views per day live (over at most 28 days), so a video that has had
  longer does not win just for being older.
- Videos are grouped by the hour they were published in (local time). A group needs at least 2
  videos to count.
- A new time is proposed only when there are at least 6 scored videos, at least 2 hours with
  2 or more videos each, and the best hour beats the median of the other hours by 1.25 times.
  Otherwise the plan changes nothing and says why. Too few numbers is a normal answer.
- Only the time of day moves, and a video due within 30 minutes is left alone. Each video stays on its day. Nothing is applied here: the caller
  shows the plan and applies it only after the user confirms.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from statistics import median
from typing import Any

MIN_SAMPLES = 6
MIN_PER_HOUR = 2
MIN_HOURS = 2
MIN_LIFT = 1.25
MIN_AGE = timedelta(days=2)
WINDOW_DAYS = 28
MIN_LEAD = timedelta(minutes=30)
MAX_CHANGES = 20  # one update costs about 50 of the 10,000 daily YouTube quota units
SAME_TIME = timedelta(minutes=1)


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def scheduled_videos(
    uploads: dict[str, dict[str, Any]],
    remote: list[dict[str, Any]],
    *,
    now: datetime,
    titles: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Videos waiting for a future release on one channel: this tool's own uploads plus the ones
    read from YouTube. A video that is in both lists appears once. Sorted by time."""
    titles = titles or {}
    out: dict[str, dict[str, Any]] = {}
    for clip_id, record in uploads.items():
        video_id = record.get("video_id")
        when = _parse(record.get("publish_at"))
        if not video_id or when is None or when <= now:
            continue
        studio = record.get("studio_url")
        out[str(video_id)] = {
            "video_id": str(video_id),
            "clip_id": clip_id,
            "title": titles.get(clip_id) or record.get("title") or str(video_id),
            "publish_at": when.isoformat(),
            "source": "tool",
            "studio_url": studio
            if isinstance(studio, str) and studio.startswith("https://studio.youtube.com/")
            else None,
        }
    for video in remote:
        video_id = str(video.get("video_id"))
        when = _parse(video.get("publish_at"))
        if video_id in out or when is None or when <= now:
            continue
        out[video_id] = {
            "video_id": video_id,
            "clip_id": None,
            "title": video.get("title") or video_id,
            "publish_at": when.isoformat(),
            "source": "youtube",
            "studio_url": f"https://studio.youtube.com/video/{video_id}/edit",
        }
    return sorted(out.values(), key=lambda v: v["publish_at"])


def videos_on_day(videos: list[dict[str, Any]], day: str, tz: Any) -> list[dict[str, Any]]:
    return [
        v
        for v in videos
        if (w := _parse(v["publish_at"])) is not None and w.astimezone(tz).date().isoformat() == day
    ]


def history_points(
    uploads: dict[str, dict[str, Any]],
    snapshots: list[dict[str, Any]],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """One point per judged video: the local hour it went public and views per day live."""
    by_video = {s["video_id"]: s for s in snapshots}
    points: list[dict[str, Any]] = []
    for record in uploads.values():
        snap = by_video.get(record.get("video_id"))
        since = _parse(record.get("publish_at"))
        if snap is None or since is None or now - since < MIN_AGE:
            continue
        end = _parse(snap.get("fetched_at")) or now
        days = max(1.0, min(float(WINDOW_DAYS), (end - since).total_seconds() / 86400))
        local = since.astimezone(now.tzinfo)
        points.append(
            {
                "video_id": record["video_id"],
                "hour": local.hour,
                "rate": int(snap["views"]) / days,
            }
        )
    return points


def rank_hours(points: list[dict[str, Any]]) -> dict[str, Any]:
    """Which publish hours did better. `confidence` is "ok" only when the rules above hold."""
    groups: dict[int, list[float]] = {}
    for p in points:
        groups.setdefault(p["hour"], []).append(p["rate"])
    hours = sorted(
        (
            {"hour": h, "videos": len(v), "median_rate": round(float(median(v)), 2)}
            for h, v in groups.items()
            if len(v) >= MIN_PER_HOUR
        ),
        key=lambda row: (-row["median_rate"], row["hour"]),
    )
    result: dict[str, Any] = {
        "samples": len(points),
        "hours": hours,
        "confidence": "none",
        "reason": None,
        "lift": None,
    }
    if len(points) < MIN_SAMPLES:
        result["reason"] = "not_enough_videos"
    elif len(hours) < MIN_HOURS:
        result["reason"] = "not_enough_hours"
    else:
        rest = float(median(row["median_rate"] for row in hours[1:]))
        best = hours[0]["median_rate"]
        lift = best / rest if rest > 0 else (float("inf") if best > 0 else 0.0)
        result["lift"] = None if lift == float("inf") else round(lift, 2)
        if best <= 0 or lift < MIN_LIFT:
            result["reason"] = "no_clear_difference"
        else:
            result["confidence"] = "ok"
    return result


def build_plan(
    *,
    now: datetime,
    upcoming: list[dict[str, Any]],
    ranking: dict[str, Any],
) -> dict[str, Any]:
    """Old and new time for each scheduled video. Empty when the numbers are not enough."""
    plan: dict[str, Any] = {
        **ranking,
        "items": [],
        "capped": False,
        "skipped_too_soon": 0,
        "upcoming": len(upcoming),
    }
    if ranking["confidence"] != "ok":
        return plan
    ranked = [row["hour"] for row in ranking["hours"]]
    per_day: dict[str, int] = {}
    items: list[dict[str, Any]] = []
    for video in sorted(upcoming, key=lambda v: v["publish_at"]):
        old = _parse(video["publish_at"])
        if old is None:
            continue
        local = old.astimezone(now.tzinfo)
        key = local.date().isoformat()
        index = per_day.get(key, 0)
        per_day[key] = index + 1
        if index < len(ranked):
            hour, minute = ranked[index], 0
        else:  # more videos that day than good hours: stack after the best hour
            hour, minute = ranked[0], 10 * (index - len(ranked) + 1)
        new = datetime.combine(local.date(), time(hour, minute), tzinfo=now.tzinfo)
        if abs(new - old) < SAME_TIME:
            continue
        if old < now + MIN_LEAD or new < now + MIN_LEAD:
            plan["skipped_too_soon"] += 1
            continue
        items.append(
            {
                "video_id": video["video_id"],
                "clip_id": video.get("clip_id"),
                "title": video["title"],
                "old_publish_at": old.isoformat(),
                "new_publish_at": new.isoformat(),
            }
        )
    plan["capped"] = len(items) > MAX_CHANGES
    plan["items"] = items[:MAX_CHANGES]
    return plan
