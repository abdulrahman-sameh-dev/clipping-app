"""Local, loopback-first FastAPI application."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from clipping_ai.analysis.service import AnalysisService
from clipping_ai.clips.models import ClipFit, ClipFormat, ClipQuality, ClipStatus
from clipping_ai.clips.service import ClipService
from clipping_ai.clips.worker import ClipWorker
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.highlights.models import ContentType
from clipping_ai.manager import ProviderManager
from clipping_ai.settings import get_settings
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


class HighlightJobRequest(BaseModel):
    content_type: ContentType = ContentType.AUTO
    provider: str | None = None
    model: str | None = None
    use_llm: bool | None = None
    refresh: bool = False


class AnalyzeJobRequest(BaseModel):
    provider: str | None = None
    model: str | None = None
    language: str | None = None
    content_type: ContentType = ContentType.AUTO
    use_llm: bool | None = None
    refresh: bool = False


class ClipCreateRequest(BaseModel):
    formats: list[ClipFormat] | None = None
    quality: ClipQuality | None = None
    fit: ClipFit | None = None
    captions: bool = False
    caption_language: str | None = None
    highlight_indices: list[int] | None = None
    limit: int | None = Field(default=None, ge=1, le=100)
    render: bool = True


def _manager(request: Request) -> ProviderManager:
    return request.app.state.manager


def _video(request: Request) -> VideoService:
    return request.app.state.video


def _analysis(request: Request) -> AnalysisService:
    return request.app.state.analysis


def _clips(request: Request) -> ClipService:
    return request.app.state.clips


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
    analysis = analysis_service or AnalysisService(
        settings,
        video_config=video.config,
        store=video.store,
        manager=provider_manager,
    )
    clips = clip_service or ClipService(
        settings,
        video_service=video,
    )
    worker = VideoWorker(video) if start_worker else None
    clip_worker_enabled = start_worker if start_clip_worker is None else start_clip_worker
    clip_worker = ClipWorker(clips) if clip_worker_enabled else None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
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
    app.state.video_worker = worker
    app.state.clip_worker = clip_worker

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
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

    @app.post("/api/ai/reload")
    def reload(request: Request) -> dict[str, str]:
        _manager(request).reload()
        return {"status": "reloaded"}

    @app.get("/api/video/config")
    def video_config(request: Request) -> dict[str, Any]:
        return _video(request).config_dict()

    @app.post("/api/video/upload")
    def upload_video(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
        job, duplicate = _video(request).create_upload(file.filename, file.file)
        return {"job": job.to_dict(), "duplicate": duplicate}

    @app.post("/api/video/url")
    def add_video_url(request: Request, body: VideoUrlRequest) -> dict[str, Any]:
        job, duplicate = _video(request).create_url(body.url)
        return {"job": job.to_dict(), "duplicate": duplicate}

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
        return {"jobs": [job.to_dict() for job in jobs]}

    @app.get("/api/video/jobs/{job_id}")
    def get_video_job(request: Request, job_id: str) -> dict[str, Any]:
        return {"job": _video(request).get(job_id).to_dict()}

    @app.get("/api/video/jobs/{job_id}/status")
    def get_video_job_status(request: Request, job_id: str) -> dict[str, Any]:
        return _video(request).status(job_id)

    @app.post("/api/video/jobs/{job_id}/retry")
    def retry_video_job(request: Request, job_id: str) -> dict[str, Any]:
        return {"job": _video(request).retry(job_id).to_dict()}

    @app.post("/api/video/jobs/{job_id}/cancel")
    def cancel_video_job(request: Request, job_id: str) -> dict[str, Any]:
        return {"job": _video(request).cancel(job_id).to_dict()}

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
        return {"job": job.to_dict()}

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
        )
        return {"job": job.to_dict()}

    @app.get("/api/video/jobs/{job_id}/highlights")
    def get_video_highlights(request: Request, job_id: str) -> dict[str, Any]:
        job = _video(request).get(job_id)
        return {
            "highlight_result": (
                job.highlight_result.model_dump(mode="json") if job.highlight_result else None
            ),
            "highlight_error": job.highlight_error,
            "highlight_at": job.highlight_at,
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
        )
        return {"job": job.to_dict()}

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
            highlight_indices=options.highlight_indices,
            limit=options.limit,
        )
        if options.render:
            jobs = [service.process(job.id) for job in jobs]
        return {"jobs": [job.to_dict() for job in jobs], "count": len(jobs)}

    @app.get("/api/video/jobs/{job_id}/clips")
    def list_source_clips(
        request: Request,
        job_id: str,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        jobs = _clips(request).list(source_job_id=job_id, limit=limit)
        return {"jobs": [job.to_dict() for job in jobs]}

    @app.get("/api/clip-jobs")
    def list_clip_jobs(
        request: Request,
        limit: int = Query(default=50, ge=1, le=500),
        status: str | None = Query(default=None),
        source_job_id: str | None = Query(default=None),
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
        jobs = _clips(request).list(
            source_job_id=source_job_id,
            status=parsed_status,
            limit=limit,
        )
        return {"jobs": [job.to_dict() for job in jobs]}

    @app.get("/api/clip-jobs/{clip_id}")
    def get_clip_job(request: Request, clip_id: str) -> dict[str, Any]:
        return {"job": _clips(request).get(clip_id).to_dict()}

    @app.get("/api/clip-jobs/{clip_id}/status")
    def get_clip_job_status(request: Request, clip_id: str) -> dict[str, Any]:
        return _clips(request).get(clip_id).status_dict()

    @app.post("/api/clip-jobs/{clip_id}/render")
    def render_clip_job(request: Request, clip_id: str) -> dict[str, Any]:
        return {"job": _clips(request).process(clip_id).to_dict()}

    @app.post("/api/clip-jobs/{clip_id}/retry")
    def retry_clip_job(request: Request, clip_id: str) -> dict[str, Any]:
        return {"job": _clips(request).retry(clip_id).to_dict()}

    @app.post("/api/clip-jobs/{clip_id}/cancel")
    def cancel_clip_job(request: Request, clip_id: str) -> dict[str, Any]:
        return {"job": _clips(request).cancel(clip_id).to_dict()}

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
