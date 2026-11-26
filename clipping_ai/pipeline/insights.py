"""Turn YouTube Analytics numbers into plain suggestions. Suggest only: nothing is changed.

Rules (deliberately simple and explained in PIPELINE.md):
- Only videos that are live and at least 2 days old are judged.
- With fewer than 3 judged videos there is not enough data to compare.
- Strong: views at least 1.5 x the median of the judged videos.
- Weak: views at most 0.5 x the median.
- Low retention: average watch time under 35% of the clip length (when the length is known).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import Any

MIN_AGE = timedelta(days=2)
MIN_JUDGED = 3
STRONG = 1.5
WEAK = 0.5
LOW_RETENTION = 0.35


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def live_since(record: dict[str, Any]) -> datetime | None:
    """When the video became public, or None when it is not public (yet)."""
    when = _parse(record.get("publish_at"))
    if when is not None:
        return when
    if record.get("privacy") == "public":
        return _parse(record.get("uploaded_at"))
    return None


def build_insights(
    *,
    now: datetime,
    uploads: dict[str, dict[str, Any]],
    snapshots: list[dict[str, Any]],
    clip_info: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """`clip_info[clip_id]` may hold `title` and `duration`. Returns rows and decisions."""
    by_video = {s["video_id"]: s for s in snapshots}
    rows: list[dict[str, Any]] = []
    for clip_id, record in uploads.items():
        video_id = record.get("video_id")
        if not video_id:
            continue
        info = clip_info.get(clip_id, {})
        since = live_since(record)
        snap = by_video.get(video_id)
        if since is None or since > now:
            verdict = "not_live"
        elif now - since < MIN_AGE:
            verdict = "too_new"
        else:
            verdict = "judge"
        views = int(snap["views"]) if snap else 0
        duration = info.get("duration")
        retention = None
        if snap and duration:
            retention = round(float(snap["avg_view_seconds"]) / float(duration), 3)
        rows.append(
            {
                "clip_id": clip_id,
                "video_id": video_id,
                "title": info.get("title") or clip_id,
                "studio_url": record.get("studio_url"),
                "verdict": verdict,
                "views": views,
                "likes": int(snap["likes"]) if snap else 0,
                "comments": int(snap["comments"]) if snap else 0,
                "avg_view_seconds": float(snap["avg_view_seconds"]) if snap else 0.0,
                "retention": retention,
                "has_data": snap is not None,
                "fetched_at": snap["fetched_at"] if snap else None,
            }
        )
    judged = [r for r in rows if r["verdict"] == "judge"]
    decisions: list[dict[str, Any]] = []
    base = None
    if len(judged) < MIN_JUDGED:
        decisions.append({"kind": "need_more_data", "count": len(judged), "clips": []})
    else:
        base = float(median(r["views"] for r in judged))
        for r in judged:
            if base > 0 and r["views"] >= STRONG * base:
                r["verdict"] = "strong"
            elif r["views"] <= WEAK * base:
                r["verdict"] = "weak"
            else:
                r["verdict"] = "ok"
        strong = [r for r in judged if r["verdict"] == "strong"]
        weak = [r for r in judged if r["verdict"] == "weak"]
        low = [r for r in judged if r["retention"] is not None and r["retention"] < LOW_RETENTION]
        if strong:
            decisions.append(
                {"kind": "make_more", "count": len(strong), "clips": [r["clip_id"] for r in strong]}
            )
        if weak:
            decisions.append(
                {
                    "kind": "change_content",
                    "count": len(weak),
                    "clips": [r["clip_id"] for r in weak],
                }
            )
        if low:
            decisions.append(
                {"kind": "improve_hook", "count": len(low), "clips": [r["clip_id"] for r in low]}
            )
        if len(weak) == len(judged):
            decisions.append({"kind": "all_weak", "count": len(weak), "clips": []})
        if not strong and not weak:
            decisions.append({"kind": "keep_going", "count": len(judged), "clips": []})
    missing = [
        r for r in rows if r["verdict"] in {"judge", "strong", "weak", "ok"} and not r["has_data"]
    ]
    if missing:
        decisions.append(
            {
                "kind": "no_numbers_yet",
                "count": len(missing),
                "clips": [r["clip_id"] for r in missing],
            }
        )
    rows.sort(key=lambda r: r["views"], reverse=True)
    return {"median_views": base, "videos": rows, "decisions": decisions}
