"""HTTP routes for the content pipeline (Sheet links, queue, daily review).

Served by the same local API as the rest of the app. That API has no authentication and
should listen on localhost only. Nothing here uploads or schedules anything: the user sends a
link to clipping with one click, and uploads stay in the YouTube panel on the clip page.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field

from clipping_ai.clips.models import ClipStatus
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.pipeline.review import build_review
from clipping_ai.pipeline.sheet import csv_export_url, fetch_rows
from clipping_ai.pipeline.store import PipelineStore
from clipping_ai.settings import Settings
from clipping_ai.youtube.service import UploadStore


class SheetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheet_url: str = Field(min_length=1, max_length=2000)


def _not_found() -> AIError:
    return AIError(ErrorCategory.NOT_FOUND, "Queue item not found.", retryable=False)


def register_pipeline_routes(
    app: FastAPI,
    *,
    settings: Settings,
    uploads_path: Path,
    http_client: httpx.Client | None = None,
) -> None:
    store = PipelineStore(settings.data_dir / "pipeline.db")
    uploads = UploadStore(uploads_path)
    send_lock = threading.Lock()

    def sync(sheet_url: str) -> int:
        try:
            rows = fetch_rows(sheet_url, client=http_client)
        except AIError as exc:
            store.record_sync(exc.message if hasattr(exc, "message") else str(exc))
            raise
        added = store.add_rows(rows)
        store.record_sync(None)
        return added

    @app.get("/api/pipeline/source")
    def pipeline_source() -> dict[str, Any]:
        return {**store.source(), "counts": store.counts()}

    @app.put("/api/pipeline/source")
    def pipeline_set_source(body: SheetBody) -> dict[str, Any]:
        csv_export_url(body.sheet_url)  # validates host and shape
        store.set_sheet_url(body.sheet_url.strip())
        return {**store.source(), "counts": store.counts()}

    @app.post("/api/pipeline/sync")
    def pipeline_sync() -> dict[str, Any]:
        sheet_url = store.source()["sheet_url"]
        if not sheet_url:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Save a Google Sheet link first.",
                retryable=False,
            )
        added = sync(sheet_url)
        return {"added": added, **store.source(), "counts": store.counts()}

    @app.get("/api/pipeline/items")
    def pipeline_items() -> dict[str, Any]:
        return {"items": store.list_items(), "counts": store.counts()}

    @app.post("/api/pipeline/items/{item_id}/send")
    def pipeline_send(request: Request, item_id: int) -> dict[str, Any]:
        with send_lock:
            item = store.get(item_id)
            if item is None:
                raise _not_found()
            if item["status"] == "sent":
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    "This link was already sent to clipping.",
                    retryable=False,
                )
            job, duplicate = request.app.state.video.create_url(item["url"])
            store.set_status(item_id, "sent", job.id)
        return {"item": store.get(item_id), "job_id": job.id, "duplicate": duplicate}

    @app.post("/api/pipeline/items/{item_id}/skip")
    def pipeline_skip(item_id: int) -> dict[str, Any]:
        item = store.get(item_id)
        if item is None:
            raise _not_found()
        if item["status"] == "sent":
            raise AIError(
                ErrorCategory.INVALID_REQUEST, "A sent link cannot be skipped.", retryable=False
            )
        store.set_status(item_id, "skipped")
        return {"item": store.get(item_id)}

    @app.post("/api/pipeline/items/{item_id}/restore")
    def pipeline_restore(item_id: int) -> dict[str, Any]:
        item = store.get(item_id)
        if item is None:
            raise _not_found()
        if item["status"] == "skipped":
            store.set_status(item_id, "new")
        return {"item": store.get(item_id)}

    @app.get("/api/pipeline/review")
    def pipeline_review(request: Request) -> dict[str, Any]:
        clips = request.app.state.clips
        ready: list[str] = []
        failed = 0
        try:
            ready = [
                c.id for c in clips.list(status=ClipStatus.READY, limit=500) if not c.published
            ]
            failed = len(clips.list(status=ClipStatus.FAILED, limit=500))
        except (AttributeError, TypeError):
            pass
        return build_review(
            now=datetime.now().astimezone(),
            source=store.source(),
            counts=store.counts(),
            uploads=uploads.load(),
            ready_clip_ids=ready,
            failed_clips=failed,
        )
