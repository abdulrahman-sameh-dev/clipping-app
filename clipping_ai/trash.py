"""Unified soft-delete, restore, and permanent deletion across sources and clips."""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from typing import Any

from clipping_ai.clips.service import ClipService
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.video.service import VideoService


@dataclass(frozen=True)
class TrashTarget:
    kind: str
    id: str


class TrashService:
    def __init__(self, video: VideoService, clips: ClipService) -> None:
        self.video = video
        self.clips = clips

    @staticmethod
    def _kind(value: str) -> str:
        kind = value.strip().lower()
        if kind not in {"source", "clip"}:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Unknown trash item type '{value}'.",
                retryable=False,
            )
        return kind

    def list(self) -> builtins.list[dict[str, Any]]:
        items: builtins.list[dict[str, Any]] = []
        for source in self.video.list_deleted(limit=500):
            items.append(
                {
                    "kind": "source",
                    "id": source.id,
                    "title": self.video.display_title(source),
                    "deleted_at": source.deleted_at,
                    "created_at": source.created_at,
                    "status": source.status.value,
                    "stage": source.stage.value,
                    "duration": (
                        source.metadata.duration_seconds if source.metadata else None
                    ),
                }
            )
        for clip in self.clips.list_deleted(limit=500):
            items.append(
                {
                    "kind": "clip",
                    "id": clip.id,
                    "title": self.clips.display_title(clip),
                    "deleted_at": clip.deleted_at,
                    "created_at": clip.created_at,
                    "status": clip.status.value,
                    "stage": clip.stage.value,
                    "duration": clip.duration,
                    "format": clip.format.value,
                    "published": clip.published,
                    "source_job_id": clip.source_job_id,
                }
            )
        return sorted(items, key=lambda item: item.get("deleted_at") or "", reverse=True)

    def _apply(self, targets: builtins.list[TrashTarget], *, permanent: bool) -> dict[str, Any]:
        completed: builtins.list[dict[str, str]] = []
        errors: builtins.list[dict[str, str]] = []
        for target in targets:
            try:
                kind = self._kind(target.kind)
                if kind == "source":
                    if permanent:
                        self.video.permanently_delete(target.id)
                    else:
                        self.video.restore(target.id)
                else:
                    if permanent:
                        self.clips.permanently_delete(target.id)
                    else:
                        self.clips.restore(target.id)
                completed.append({"kind": kind, "id": target.id})
            except AIError as exc:
                errors.append(
                    {
                        "kind": target.kind,
                        "id": target.id,
                        "message": str(exc),
                    }
                )
        return {
            "restored" if not permanent else "deleted": completed,
            "errors": errors,
            "count": len(completed),
        }

    def restore(self, targets: builtins.list[TrashTarget]) -> dict[str, Any]:
        return self._apply(targets, permanent=False)

    def permanently_delete(self, targets: builtins.list[TrashTarget]) -> dict[str, Any]:
        return self._apply(targets, permanent=True)

    def empty(self) -> dict[str, Any]:
        targets = [
            TrashTarget(kind=str(item["kind"]), id=str(item["id"]))
            for item in self.list()
        ]
        return self.permanently_delete(targets)
