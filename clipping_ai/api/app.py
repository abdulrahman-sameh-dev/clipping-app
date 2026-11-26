"""Local, loopback-first FastAPI application."""

from __future__ import annotations

import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Self

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from clipping_ai.analysis.service import AnalysisService
from clipping_ai.clips.models import ClipFit, ClipFormat, ClipQuality, ClipRange, ClipStatus
from clipping_ai.clips.service import ClipService
from clipping_ai.clips.worker import ClipWorker
from clipping_ai.composition.models import VisualSettings
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.highlights.models import ContentType
from clipping_ai.manager import ProviderManager
from clipping_ai.recovery import (
    DataDirectoryLock,
    StageStore,
    StartupReconciler,
)
from clipping_ai.settings import get_settings
from clipping_ai.skills.executor import SkillExecutor
from clipping_ai.skills.registry import SkillRegistry
from clipping_ai.trash import TrashService, TrashTarget
from clipping_ai.video.models import JobStatus
from clipping_ai.video.service import VideoService
from clipping_ai.video.worker import VideoWorker

logger = logging.getLogger(__name__)


class ConnectionRequest(BaseModel):
    provider: str
    model: str | None = None


class AnalyzeRequest(BaseModel):
    task: str
    text: str = Field(min_length=1)
    instructions: str | None = None
    context: str | None = None
    target_language: str | None = None
    provider: str | None = None
    model: str | None = None
    payload: dict[str, Any] | None = None


class TranscribeRequest(BaseModel):
    audio_path: str = Field(min_length=1)
    provider: str | None = None
    model: str | None = None
    language: str | None = None
    word_timestamps: bool = True
    prompt: str | None = None


class VideoUrlRequest(BaseModel):
    url: str = Field(min_length=1, max_length=4096)


class TranscribeJobRequest(BaseModel):
    provider: str | None = None
    model: str | None = None
    language: str | None = None
    word_timestamps: bool | None = None
    prompt: str | None = None
    refresh: bool = False


class HighlightRequestBase(BaseModel):
    multi_pass: bool | None = None
    window_seconds: float | None = Field(default=None, gt=0)
    overlap_seconds: float | None = Field(default=None, ge=0)
    max_highlights: int | None = Field(default=None, ge=1, le=50)

    @model_validator(mode="after")
    def validate_window_settings(self) -> Self:
        if (
            self.window_seconds is not None
            and self.overlap_seconds is not None
            and self.overlap_seconds >= self.window_seconds
        ):
            raise ValueError("overlap_seconds must be smaller than window_seconds")
        return self


class HighlightJobRequest(HighlightRequestBase):
    content_type: ContentType = ContentType.AUTO
    provider: str | None = None
    model: str | None = None
    use_llm: bool | None = None
    refresh: bool = False


class AnalyzeJobRequest(HighlightRequestBase):
    provider: str | None = None
    model: str | None = None
    language: str | None = None
    content_type: ContentType = ContentType.AUTO
    use_llm: bool | None = None
    refresh: bool = False


class ProviderToggleRequest(BaseModel):
    enabled: bool


class SkillToggleRequest(BaseModel):
    enabled: bool


class SkillReviewRequest(BaseModel):
    reviewed: bool = True


class SkillExecuteRequest(BaseModel):
    context: dict[str, Any] = Field(default_factory=dict)


class ClipCreateRequest(BaseModel):
    formats: list[ClipFormat] | None = None
    quality: ClipQuality | None = None
    fit: ClipFit | None = None
    captions: bool = False
    caption_language: str | None = None
    ranges: list[ClipRange] | None = Field(default=None, max_length=100)
    highlight_indices: list[int] | None = None
    limit: int | None = Field(default=None, ge=1, le=100)
    visual_settings: VisualSettings | None = None
    render: bool = True


class ClipMetadataUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=10000)
    hashtags: list[str] = Field(default_factory=list, max_length=20)
    language: str | None = Field(default=None, max_length=32)


class ClipMetadataGenerateRequest(BaseModel):
    provider: str | None = None
    model: str | None = None
    force: bool = True


class ClipPublishRequest(BaseModel):
    published: bool


class SourceMetadataGenerateRequest(BaseModel):
    provider: str | None = None
    model: str | None = None
    force: bool = False
    retry_failed: bool = True


class TrashItemRequest(BaseModel):
    kind: Literal["source", "clip"]
    id: str = Field(min_length=1, max_length=64)


class TrashItemsRequest(BaseModel):
    items: list[TrashItemRequest] = Field(min_length=1, max_length=500)


def _manager(request: Request) -> ProviderManager:
    return request.app.state.manager


def _video(request: Request) -> VideoService:
    return request.app.state.video


def _analysis(request: Request) -> AnalysisService:
    return request.app.state.analysis


def _clips(request: Request) -> ClipService:
    return request.app.state.clips


def _trash(request: Request) -> TrashService:
    return request.app.state.trash


def create_app(
    manager: ProviderManager | None = None,
    *,
    video_service: VideoService | None = None,
    analysis_service: AnalysisService | None = None,
    clip_service: ClipService | None = None,
    start_worker: bool = True,
    start_clip_worker: bool | None = None,
) -> FastAPI:
    settings = manager.settings if manager is not None else get_settings()
    video = video_service or VideoService(settings)
    provider_manager = manager or ProviderManager.from_config(settings)
    data_lock = DataDirectoryLock(settings.data_dir)
    stage_store = StageStore(settings.jobs_db_path)
    analysis = analysis_service or AnalysisService(
        settings,
        video_config=video.config,
        store=video.store,
        manager=provider_manager,
        stage_store=stage_store,
    )
    clips = clip_service or ClipService(
        settings,
        video_service=video,
        stage_store=stage_store,
        manager=provider_manager,
    )
    trash = TrashService(video, clips)
    worker = VideoWorker(video) if start_worker else None
    clip_worker_enabled = start_worker if start_clip_worker is None else start_clip_worker
    clip_worker = ClipWorker(clips) if clip_worker_enabled else None
    skills = SkillRegistry(settings.config_dir)
    skill_executor = SkillExecutor(skills)
    reconciler = StartupReconciler(settings.data_dir, stage_store)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        data_lock.acquire()
        try:
            app.state.reconciliation = reconciler.run()
            if worker is not None:
                worker.start()
            if clip_worker is not None:
                clip_worker.start()
            try:
                yield
            finally:
                if clip_worker is not None:
                    clip_worker.stop()
                if worker is not None:
                    worker.stop()
        finally:
            data_lock.release()

    app = FastAPI(
        title="Clipping AI",
        version="0.1.0",
        description="Local free-tier-only AI provider and video preparation service.",
        lifespan=lifespan,
    )
    app.state.manager = provider_manager
    app.state.video = video
    app.state.analysis = analysis
    app.state.clips = clips
    app.state.trash = trash
    app.state.video_worker = worker
    app.state.clip_worker = clip_worker
    app.state.skills = skills
    app.state.skill_executor = skill_executor
    app.state.data_lock = data_lock
    app.state.stage_store = stage_store
    app.state.reconciliation = None

    def video_payload(job: Any) -> dict[str, Any]:
        counts = clips.store.counts_by_source([job.id]).get(job.id, {"total": 0, "published": 0})
        return {**job.public_dict(), "clip_counts": counts}

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    @app.exception_handler(AIError)
    async def ai_error_handler(_: Request, exc: AIError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.to_dict()})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/ai/providers")
    def providers(request: Request) -> dict[str, Any]:
        return {"providers": _manager(request).list_providers()}

    @app.get("/api/ai/models")
    def models(
        request: Request,
        task: str | None = Query(default=None),
    ) -> dict[str, Any]:
        items = _manager(request).list_models()
        if task:
            items = [item for item in items if task in item.get("tasks", [])]
        return {"models": items}

    @app.get("/api/ai/status")
    def status(
        request: Request,
        include_health: bool = Query(default=False),
    ) -> dict[str, Any]:
        return _manager(request).status(include_health=include_health)

    @app.get("/api/ai/usage")
    def usage(
        request: Request,
        days: int = Query(default=7, ge=1, le=365),
        recent_limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        tracker = _manager(request).usage
        return {
            "totals": tracker.totals(),
            "summary": tracker.summary(days),
            "daily": tracker.daily_counts(days),
            "recent": tracker.recent(recent_limit),
        }

    @app.post("/api/ai/test-connection")
    def test_connection(request: Request, body: ConnectionRequest) -> dict[str, Any]:
        return _manager(request).test_connection(body.provider, body.model)

    @app.post("/api/ai/analyze")
    def analyze(request: Request, body: AnalyzeRequest) -> dict[str, Any]:
        return _manager(request).analyze(
            body.task,
            text=body.text,
            instructions=body.instructions,
            context=body.context,
            target_language=body.target_language,
            provider=body.provider,
            model=body.model,
            payload=body.payload,
        )

    @app.post("/api/ai/transcribe")
    def transcribe(request: Request, body: TranscribeRequest) -> dict[str, Any]:
        return _manager(request).transcribe(
            body.audio_path,
            provider=body.provider,
            model=body.model,
            language=body.language,
            word_timestamps=body.word_timestamps,
            prompt=body.prompt,
        )

    @app.post("/api/ai/verify-free/{model_id}")
    def verify_free(request: Request, model_id: str) -> dict[str, Any]:
        return _manager(request).verify_free(model_id)

    @app.post("/api/ai/providers/{provider_id}/discover")
    def discover_provider_models(
        request: Request,
        provider_id: str,
        force: bool = Query(default=True),
    ) -> dict[str, Any]:
        if provider_id != "nvidia":
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Catalog discovery is not implemented for provider '{provider_id}'.",
                retryable=False,
            )
        return _manager(request).discover_nvidia_models(force=force)

    @app.patch("/api/ai/providers/{provider_id}")
    def toggle_provider(
        request: Request, provider_id: str, body: ProviderToggleRequest
    ) -> dict[str, Any]:
        manager = _manager(request)
        manager.registry.set_provider_enabled(provider_id, body.enabled)
        manager.reload()
        providers = _manager(request).list_providers()
        provider = next((item for item in providers if item["id"] == provider_id), None)
        return {"provider": provider}

    @app.post("/api/ai/reload")
    def reload(request: Request) -> dict[str, str]:
        _manager(request).reload()
        return {"status": "reloaded"}

    @app.get("/api/skills")
    def list_skills(request: Request) -> dict[str, Any]:
        registry = request.app.state.skills
        return {
            "skills": registry.list(),
            "tools": registry.tools.list(),
            "external_dir": str(registry.external_dir),
        }

    @app.patch("/api/skills/{skill_id}")
    def toggle_skill(request: Request, skill_id: str, body: SkillToggleRequest) -> dict[str, Any]:
        record = request.app.state.skills.set_enabled(skill_id, body.enabled)
        return {"skill": record.public_dict()}

    @app.post("/api/skills/{skill_id}/review")
    def review_skill(request: Request, skill_id: str, body: SkillReviewRequest) -> dict[str, Any]:
        record = request.app.state.skills.review(skill_id, reviewed=body.reviewed)
        return {"skill": record.public_dict()}

    @app.post("/api/skills/{skill_id}/execute")
    def execute_skill(request: Request, skill_id: str, body: SkillExecuteRequest) -> dict[str, Any]:
        return request.app.state.skill_executor.execute(skill_id, body.context)

    @app.get("/api/video/config")
    def video_config(request: Request) -> dict[str, Any]:
        return _video(request).config_dict()

    @app.post("/api/video/upload")
    def upload_video(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
        job, duplicate = _video(request).create_upload(file.filename, file.file)
        return {"job": video_payload(job), "duplicate": duplicate}

    @app.post("/api/video/url")
    def add_video_url(request: Request, body: VideoUrlRequest) -> dict[str, Any]:
        job, duplicate = _video(request).create_url(body.url)
        return {"job": video_payload(job), "duplicate": duplicate}

    @app.get("/api/video/jobs")
    def list_video_jobs(
        request: Request,
        limit: int = Query(default=50, ge=1, le=500),
        status: str | None = Query(default=None),
    ) -> dict[str, Any]:
        parsed_status: JobStatus | None = None
        if status is not None:
            try:
                parsed_status = JobStatus(status)
            except ValueError as exc:
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    f"Unknown job status '{status}'.",
                    retryable=False,
                ) from exc
        jobs = _video(request).list(limit=limit, status=parsed_status)
        return {"jobs": [video_payload(job) for job in jobs]}

    @app.get("/api/video/jobs/{job_id}")
    def get_video_job(request: Request, job_id: str) -> dict[str, Any]:
        return {"job": video_payload(_video(request).get(job_id))}

    @app.get("/api/video/jobs/{job_id}/media")
    def get_video_media(request: Request, job_id: str) -> FileResponse:
        _job, path = _video(request).source_media_path(job_id)
        media_type = mimetypes.guess_type(path.name)[0]
        if media_type is None or not media_type.startswith("video/"):
            media_type = "application/octet-stream"
        return FileResponse(
            path,
            media_type=media_type,
            filename=path.name,
            content_disposition_type="inline",
        )

    @app.get("/api/video/jobs/{job_id}/status")
    def get_video_job_status(request: Request, job_id: str) -> dict[str, Any]:
        return _video(request).status(job_id)

    @app.post("/api/video/jobs/{job_id}/retry")
    def retry_video_job(request: Request, job_id: str) -> dict[str, Any]:
        return {"job": video_payload(_video(request).retry(job_id))}

    @app.post("/api/video/jobs/{job_id}/cancel")
    def cancel_video_job(request: Request, job_id: str) -> dict[str, Any]:
        return {"job": video_payload(_video(request).cancel(job_id))}

    @app.post("/api/video/jobs/{job_id}/publish")
    def publish_video_job(
        request: Request,
        job_id: str,
        body: ClipPublishRequest,
    ) -> dict[str, Any]:
        return {
            "job": video_payload(_video(request).set_all_clips_published(job_id, body.published))
        }

    @app.post("/api/video/jobs/{job_id}/metadata/regenerate")
    def regenerate_video_metadata(
        request: Request,
        job_id: str,
        body: SourceMetadataGenerateRequest | None = None,
    ) -> dict[str, Any]:
        options = body or SourceMetadataGenerateRequest()
        jobs = _clips(request).generate_metadata_batch(
            job_id,
            provider=options.provider,
            model=options.model,
            force=options.force,
            retry_failed=options.retry_failed,
        )
        return {"jobs": [job.public_dict() for job in jobs]}

    @app.delete("/api/video/jobs/{job_id}")
    def delete_video_job(request: Request, job_id: str) -> dict[str, Any]:
        return {"job": video_payload(_video(request).soft_delete(job_id))}

    @app.post("/api/video/jobs/{job_id}/restore")
    def restore_video_job(request: Request, job_id: str) -> dict[str, Any]:
        return {"job": video_payload(_video(request).restore(job_id))}

    @app.delete("/api/video/jobs/{job_id}/permanent")
    def permanently_delete_video_job(request: Request, job_id: str) -> dict[str, str]:
        _video(request).permanently_delete(job_id)
        return {"status": "deleted"}

    @app.get("/api/analysis/config")
    def analysis_config(request: Request) -> dict[str, Any]:
        return _analysis(request).config_dict()

    @app.post("/api/video/jobs/{job_id}/transcribe")
    def transcribe_video_job(
        request: Request,
        job_id: str,
        body: TranscribeJobRequest | None = None,
    ) -> dict[str, Any]:
        options = body or TranscribeJobRequest()
        job = _analysis(request).transcribe(
            job_id,
            provider=options.provider,
            model=options.model,
            language=options.language,
            word_timestamps=options.word_timestamps,
            prompt=options.prompt,
            refresh=options.refresh,
        )
        return {"job": job.public_dict()}

    @app.get("/api/video/jobs/{job_id}/transcript")
    def get_video_transcript(request: Request, job_id: str) -> dict[str, Any]:
        job = _video(request).get(job_id)
        return {
            "transcript": job.transcript.model_dump(mode="json") if job.transcript else None,
            "transcript_error": job.transcript_error,
            "transcript_provider": job.transcript_provider,
            "transcript_model": job.transcript_model,
            "transcript_at": job.transcript_at,
        }

    @app.post("/api/video/jobs/{job_id}/highlights")
    def highlight_video_job(
        request: Request,
        job_id: str,
        body: HighlightJobRequest | None = None,
    ) -> dict[str, Any]:
        options = body or HighlightJobRequest()
        job = _analysis(request).highlights(
            job_id,
            content_type=options.content_type,
            provider=options.provider,
            model=options.model,
            use_llm=options.use_llm,
            refresh=options.refresh,
            multi_pass=options.multi_pass,
            window_seconds=options.window_seconds,
            overlap_seconds=options.overlap_seconds,
            max_highlights=options.max_highlights,
        )
        return {"job": job.public_dict()}

    @app.get("/api/video/jobs/{job_id}/highlights")
    def get_video_highlights(request: Request, job_id: str) -> dict[str, Any]:
        job = _video(request).get(job_id)
        return {
            "highlight_result": (
                job.highlight_result.model_dump(mode="json") if job.highlight_result else None
            ),
            "highlight_error": job.highlight_error,
            "highlight_at": job.highlight_at,
            "discovery_progress": _analysis(request).discovery_progress(job_id),
        }


    @app.post("/api/video/jobs/{job_id}/analyze")
    def analyze_video_job(
        request: Request,
        job_id: str,
        body: AnalyzeJobRequest | None = None,
    ) -> dict[str, Any]:
        options = body or AnalyzeJobRequest()
        job = _analysis(request).analyze(
            job_id,
            provider=options.provider,
            model=options.model,
            language=options.language,
            content_type=options.content_type,
            use_llm=options.use_llm,
            refresh=options.refresh,
            multi_pass=options.multi_pass,
            window_seconds=options.window_seconds,
            overlap_seconds=options.overlap_seconds,
            max_highlights=options.max_highlights,
        )
        return {"job": job.public_dict()}

    @app.get("/api/clips/config")
    def clips_config(request: Request) -> dict[str, Any]:
        return _clips(request).config_dict()

    @app.post("/api/video/jobs/{job_id}/clips")
    def create_clips(
        request: Request,
        job_id: str,
        body: ClipCreateRequest | None = None,
    ) -> dict[str, Any]:
        options = body or ClipCreateRequest()
        service = _clips(request)
        jobs = service.create_for_job(
            job_id,
            formats=options.formats,
            quality=options.quality,
            fit=options.fit,
            captions=options.captions,
            caption_language=options.caption_language,
            ranges=options.ranges,
            highlight_indices=options.highlight_indices,
            limit=options.limit,
            visual_settings=options.visual_settings,
        )
        if options.render:
            jobs = [service.process(job.id) for job in jobs]
        return {"jobs": [job.public_dict() for job in jobs], "count": len(jobs)}

    @app.get("/api/video/jobs/{job_id}/clips")
    def list_source_clips(
        request: Request,
        job_id: str,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        jobs = _clips(request).list(source_job_id=job_id, limit=limit)
        return {"jobs": [job.public_dict() for job in jobs]}

    @app.get("/api/clip-jobs")
    def list_clip_jobs(
        request: Request,
        limit: int = Query(default=50, ge=1, le=500),
        status: str | None = Query(default=None),
        source_job_id: str | None = Query(default=None),
        ids: str | None = Query(default=None),
    ) -> dict[str, Any]:
        parsed_status: ClipStatus | None = None
        if status is not None:
            try:
                parsed_status = ClipStatus(status)
            except ValueError as exc:
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    f"Unknown clip status '{status}'.",
                    retryable=False,
                ) from exc
        parsed_ids = [value.strip() for value in ids.split(",") if value.strip()] if ids else None
        if parsed_ids is not None and not parsed_ids:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "The ids parameter must contain at least one clip id.",
                retryable=False,
            )
        effective_limit = min(max(limit, len(parsed_ids or [])), 500)
        jobs = _clips(request).list(
            source_job_id=source_job_id,
            status=parsed_status,
            ids=parsed_ids,
            limit=effective_limit,
        )
        return {"jobs": [job.public_dict() for job in jobs]}

    @app.get("/api/clip-jobs/{clip_id}")
    def get_clip_job(request: Request, clip_id: str) -> dict[str, Any]:
        return {"job": _clips(request).get(clip_id).public_dict()}

    @app.get("/api/clip-jobs/{clip_id}/status")
    def get_clip_job_status(request: Request, clip_id: str) -> dict[str, Any]:
        return _clips(request).get(clip_id).status_dict()

    @app.post("/api/clip-jobs/{clip_id}/render")
    def render_clip_job(request: Request, clip_id: str) -> dict[str, Any]:
        return {"job": _clips(request).process(clip_id).public_dict()}

    @app.patch("/api/clip-jobs/{clip_id}/visual")
    def update_clip_visual(request: Request, clip_id: str, body: VisualSettings) -> dict[str, Any]:
        job = _clips(request).update_visual_settings(clip_id, body, rerender=True)
        return {"job": job.public_dict()}

    @app.post("/api/clip-jobs/{clip_id}/retry")
    def retry_clip_job(request: Request, clip_id: str) -> dict[str, Any]:
        return {"job": _clips(request).retry(clip_id).public_dict()}

    @app.post("/api/clip-jobs/{clip_id}/cancel")
    def cancel_clip_job(request: Request, clip_id: str) -> dict[str, Any]:
        return {"job": _clips(request).cancel(clip_id).public_dict()}

    @app.patch("/api/clip-jobs/{clip_id}/metadata")
    def update_clip_metadata(
        request: Request,
        clip_id: str,
        body: ClipMetadataUpdateRequest,
    ) -> dict[str, Any]:
        job = _clips(request).update_metadata(
            clip_id,
            title=body.title,
            description=body.description,
            hashtags=body.hashtags,
            language=body.language,
        )
        return {"job": job.public_dict()}

    @app.post("/api/clip-jobs/{clip_id}/metadata/regenerate")
    def regenerate_clip_metadata(
        request: Request,
        clip_id: str,
        body: ClipMetadataGenerateRequest | None = None,
    ) -> dict[str, Any]:
        options = body or ClipMetadataGenerateRequest()
        job = _clips(request).generate_metadata(
            clip_id,
            provider=options.provider,
            model=options.model,
            force=options.force,
        )
        return {"job": job.public_dict()}

    @app.post("/api/clip-jobs/{clip_id}/publish")
    def publish_clip_job(
        request: Request,
        clip_id: str,
        body: ClipPublishRequest,
    ) -> dict[str, Any]:
        return {"job": _clips(request).set_published(clip_id, body.published).public_dict()}

    @app.delete("/api/clip-jobs/{clip_id}")
    def delete_clip_job(request: Request, clip_id: str) -> dict[str, Any]:
        return {"job": _clips(request).soft_delete(clip_id).public_dict()}

    @app.post("/api/clip-jobs/{clip_id}/restore")
    def restore_clip_job(request: Request, clip_id: str) -> dict[str, Any]:
        return {"job": _clips(request).restore(clip_id).public_dict()}

    @app.delete("/api/clip-jobs/{clip_id}/permanent")
    def permanently_delete_clip_job(request: Request, clip_id: str) -> dict[str, str]:
        _clips(request).permanently_delete(clip_id)
        return {"status": "deleted"}

    @app.get("/api/trash")
    def list_trash(request: Request) -> dict[str, Any]:
        return {"items": _trash(request).list()}

    @app.post("/api/trash/restore")
    def restore_trash(request: Request, body: TrashItemsRequest) -> dict[str, Any]:
        targets = [TrashTarget(item.kind, item.id) for item in body.items]
        return _trash(request).restore(targets)

    @app.post("/api/trash/permanent")
    def permanently_delete_trash(
        request: Request,
        body: TrashItemsRequest,
    ) -> dict[str, Any]:
        targets = [TrashTarget(item.kind, item.id) for item in body.items]
        return _trash(request).permanently_delete(targets)

    @app.delete("/api/trash")
    def empty_trash(request: Request) -> dict[str, Any]:
        return _trash(request).empty()

    @app.get("/api/clip-jobs/{clip_id}/download")
    def download_clip_job(request: Request, clip_id: str) -> FileResponse:
        path = _clips(request).output_path(clip_id)
        return FileResponse(path, media_type="video/mp4", filename=path.name)

    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    if frontend_dir.is_dir():
        app.mount("/ui", StaticFiles(directory=frontend_dir, html=True), name="studio")

        @app.get("/", include_in_schema=False)
        def studio() -> RedirectResponse:
            return RedirectResponse(url="/ui/")

    return app


_APP: FastAPI | None = None


def __getattr__(name: str) -> Any:
    """Build the default app lazily so importing this module has no side effects."""
    global _APP
    if name == "app":
        if _APP is None:
            _APP = create_app()
        return _APP
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
