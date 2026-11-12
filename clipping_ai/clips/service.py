"""Clip job orchestration: create, render, cancel, retry, and recover."""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from uuid import uuid4

from clipping_ai.clips.captions import build_ass_file
from clipping_ai.clips.config import ClipConfig, load_clip_config
from clipping_ai.clips.editing import EditPlan, build_edit_plan
from clipping_ai.clips.models import (
    ClipFit,
    ClipFormat,
    ClipJob,
    ClipQuality,
    ClipStage,
    ClipStatus,
)
from clipping_ai.clips.renderer import MediaInfo, RenderResult, probe_media, render_clip
from clipping_ai.clips.store import ClipJobStore
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.highlights.models import Highlight
from clipping_ai.settings import Settings, get_settings
from clipping_ai.video.ingest import is_safe_job_id
from clipping_ai.video.models import JobStatus, VideoJob
from clipping_ai.video.service import VideoService

logger = logging.getLogger(__name__)


class ClipService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        video_service: VideoService | None = None,
        config: ClipConfig | None = None,
        store: ClipJobStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self.video = video_service or VideoService(self.settings)
        self.config = config or load_clip_config(self.settings.config_dir)
        self.store = store or ClipJobStore(self.settings.jobs_db_path)
        self.data_dir = self.settings.data_dir.resolve()
        self.clips_root = self.data_dir / "clips" / "jobs"
        self.clips_root.mkdir(parents=True, exist_ok=True)
        self._render_lock = threading.Lock()

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Clip rendering is disabled in config/clips.json.",
                retryable=False,
            )

    def clip_dir(self, clip_id: str) -> Path:
        if not is_safe_job_id(clip_id):
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Invalid clip ID.",
                retryable=False,
            )
        return self.clips_root / clip_id

    def _safe_source_path(self, job: VideoJob) -> Path:
        if job.input_path is None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "The source video job has no input file.",
                retryable=False,
            )
        path = Path(job.input_path).resolve()
        if not path.is_relative_to(self.data_dir.resolve()):
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Source file is outside the configured data directory.",
                retryable=False,
            )
        if not path.is_file():
            raise AIError(
                ErrorCategory.NOT_FOUND,
                "The source video file is missing.",
                retryable=False,
            )
        return path

    def _source_info(self, job: VideoJob, source_path: Path) -> MediaInfo:
        metadata = job.metadata
        if metadata is not None:
            return MediaInfo(
                duration=metadata.duration_seconds,
                has_video=metadata.has_video,
                has_audio=metadata.has_audio,
                video_codec=(
                    metadata.video_streams[0].codec_name if metadata.video_streams else None
                ),
                audio_codec=(
                    metadata.audio_streams[0].codec_name if metadata.audio_streams else None
                ),
                width=(
                    metadata.video_streams[0].width if metadata.video_streams else None
                ),
                height=(
                    metadata.video_streams[0].height if metadata.video_streams else None
                ),
            )
        return probe_media(source_path, self.config)

    @staticmethod
    def _formats(values: Iterable[str | ClipFormat] | None, config: ClipConfig) -> list[ClipFormat]:
        if values is None:
            return list(config.default_formats)
        formats: list[ClipFormat] = []
        for value in values:
            try:
                fmt = value if isinstance(value, ClipFormat) else ClipFormat(value)
            except ValueError as exc:
                allowed = ", ".join(item.value for item in ClipFormat)
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    f"Unknown clip format '{value}'. Allowed: {allowed}.",
                    retryable=False,
                ) from exc
            if fmt not in formats:
                formats.append(fmt)
        if not formats:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "At least one clip format is required.",
                retryable=False,
            )
        return formats

    @staticmethod
    def _quality(value: str | ClipQuality | None, config: ClipConfig) -> ClipQuality:
        if value is None:
            return config.default_quality
        try:
            return value if isinstance(value, ClipQuality) else ClipQuality(value)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in ClipQuality)
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Unknown clip quality '{value}'. Allowed: {allowed}.",
                retryable=False,
            ) from exc

    @staticmethod
    def _fit(value: str | ClipFit | None, config: ClipConfig) -> ClipFit:
        if value is None:
            return config.default_fit
        try:
            return value if isinstance(value, ClipFit) else ClipFit(value)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in ClipFit)
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Unknown clip fit '{value}'. Allowed: {allowed}.",
                retryable=False,
            ) from exc

    def _fit_for_format(
        self,
        fmt: ClipFormat,
        selected_fit: ClipFit,
        *,
        explicit: bool,
        source_info: MediaInfo,
    ) -> ClipFit:
        if explicit or not self.config.auto_vertical_fit:
            return selected_fit
        if (
            fmt == ClipFormat.VERTICAL
            and source_info.width
            and source_info.height
            and source_info.width > source_info.height
        ):
            return self.config.vertical_landscape_fit
        return selected_fit

    @staticmethod
    def _highlight_for_clip(
        source_job: VideoJob,
        clip: ClipJob,
    ) -> Highlight | None:
        if source_job.highlight_result is None or clip.highlight_index is None:
            return None
        highlights = source_job.highlight_result.highlights
        if 0 <= clip.highlight_index < len(highlights):
            return highlights[clip.highlight_index]
        return None

    def create_for_job(
        self,
        source_job_id: str,
        *,
        formats: Iterable[str | ClipFormat] | None = None,
        quality: str | ClipQuality | None = None,
        fit: str | ClipFit | None = None,
        captions: bool = False,
        caption_language: str | None = None,
        highlight_indices: Iterable[int] | None = None,
        limit: int | None = None,
    ) -> list[ClipJob]:
        self._require_enabled()
        if not is_safe_job_id(source_job_id):
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Invalid video job ID.",
                retryable=False,
            )
        source_job = self.video.store.require(source_job_id)
        if source_job.status != JobStatus.READY:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Video job '{source_job_id}' is {source_job.status.value}; "
                "finish preparation before creating clips.",
                retryable=False,
            )
        if source_job.highlight_result is None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "No highlight result exists for this video. Run highlight analysis first.",
                retryable=False,
            )
        source_path = self._safe_source_path(source_job)
        source_info = self._source_info(source_job, source_path)
        if not source_info.has_video:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Source media has no video stream.",
                retryable=False,
            )
        if captions and source_job.transcript is None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Captions were requested but this video has no transcript. "
                "Transcribe it first or create the clips without captions.",
                retryable=False,
            )

        selected_formats = self._formats(formats, self.config)
        selected_quality = self._quality(quality, self.config)
        selected_fit = self._fit(fit, self.config)
        explicit_fit = fit is not None
        all_highlights = list(source_job.highlight_result.highlights)
        selected: list[tuple[int, Any]] = list(enumerate(all_highlights))
        if highlight_indices is not None:
            indices = list(highlight_indices)
            invalid = [index for index in indices if index < 0 or index >= len(all_highlights)]
            if invalid:
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    f"Highlight index out of range: {invalid[0]}.",
                    retryable=False,
                )
            selected = [(index, all_highlights[index]) for index in indices]
        if limit is not None:
            if limit < 1:
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    "limit must be at least 1.",
                    retryable=False,
                )
            selected = selected[:limit]
        if not selected:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "No highlights are available to render.",
                retryable=False,
            )

        for _, highlight in selected:
            if highlight.start_time < 0 or highlight.end_time <= highlight.start_time:
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    f"Highlight has invalid timestamps: {highlight.start_time} - "
                    f"{highlight.end_time}.",
                    retryable=False,
                )
            duration = highlight.end_time - highlight.start_time
            if duration < self.config.min_clip_seconds:
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    f"Highlight is shorter than the minimum clip duration "
                    f"({duration:.2f}s < {self.config.min_clip_seconds:.2f}s).",
                    retryable=False,
                )
            if duration > self.config.max_clip_seconds:
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    f"Highlight is longer than the maximum clip duration "
                    f"({duration:.2f}s > {self.config.max_clip_seconds:.2f}s).",
                    retryable=False,
                )
            if source_info.duration > 0 and highlight.end_time > source_info.duration + 0.05:
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    f"Highlight end ({highlight.end_time:.2f}s) exceeds source duration "
                    f"({source_info.duration:.2f}s).",
                    retryable=False,
                )

        created: list[ClipJob] = []
        for original_index, highlight in selected:
            for fmt in selected_formats:
                profile = self.config.format_profile(fmt)
                clip_fit = self._fit_for_format(
                    fmt,
                    selected_fit,
                    explicit=explicit_fit,
                    source_info=source_info,
                )
                clip_id = uuid4().hex
                clip = self.store.create(
                    id=clip_id,
                    source_job_id=source_job.id,
                    highlight_index=original_index,
                    start_time=highlight.start_time,
                    end_time=highlight.end_time,
                    format=fmt,
                    width=profile.width,
                    height=profile.height,
                    fps=self.config.default_fps,
                    quality=selected_quality,
                    fit=clip_fit,
                    captions=captions,
                    caption_language=caption_language,
                    status=ClipStatus.QUEUED,
                    stage=ClipStage.QUEUED,
                )
                created.append(clip)
        return created

    def process(self, clip_id: str) -> ClipJob:
        with self._render_lock:
            return self._process(clip_id)

    def _process(self, clip_id: str) -> ClipJob:
        job = self.store.require(clip_id)
        if job.status == ClipStatus.READY:
            return job
        job = self.store.mark_processing(clip_id)
        if job.cancel_requested:
            if not self.config.keep_failed_artifacts:
                self._cleanup_failed(clip_id)
            return self.store.mark_cancelled(clip_id)

        source_job = self.video.store.require(job.source_job_id)
        source_path = self._safe_source_path(source_job)
        source_info = self._source_info(source_job, source_path)
        directory = self.clip_dir(clip_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        output_path = directory / f"clip_{job.format.value}_{job.quality.value}.mp4"
        highlight = self._highlight_for_clip(source_job, job)
        signals = (
            source_job.highlight_result.signals
            if source_job.highlight_result is not None
            else {}
        )
        edit_plan = build_edit_plan(
            source_job.transcript,
            highlight,
            start_time=job.start_time,
            end_time=job.end_time,
            config=self.config,
            signals=signals,
            source_width=source_info.width,
            source_height=source_info.height,
        )

        try:
            if job.captions:
                self.store.update(clip_id, stage=ClipStage.CAPTIONS, progress=0.1)
                if source_job.transcript is None:
                    raise AIError(
                        ErrorCategory.INVALID_REQUEST,
                        "Captions were requested but the source transcript is missing.",
                        retryable=False,
                    )
                caption_path, language = build_ass_file(
                    source_job.transcript,
                    start_time=job.start_time,
                    end_time=job.end_time,
                    output_path=directory / "captions.ass",
                    width=job.width,
                    height=job.height,
                    config=self.config,
                    language=job.caption_language,
                    highlight=highlight,
                    signals=signals,
                )
                job = self.store.update(
                    clip_id,
                    caption_path=caption_path,
                    caption_language=language,
                )

            if self.store.is_cancel_requested(clip_id):
                if not self.config.keep_failed_artifacts:
                    self._cleanup_failed(clip_id)
                return self.store.mark_cancelled(clip_id)

            self.store.update(clip_id, stage=ClipStage.RENDERING, progress=0.25)
            result = render_clip(
                job,
                source_path=source_path,
                output_path=output_path,
                config=self.config,
                cancel_check=lambda: self.store.is_cancel_requested(clip_id),
                source_info=source_info,
                edit_plan=edit_plan,
            )
            self._write_render_diagnostics(
                directory,
                job=job,
                edit_plan=edit_plan,
                result=result,
            )
            logger.info(
                "Clip %s rendered with effects %s",
                clip_id,
                [effect.kind for effect in edit_plan.effects],
            )
            self.store.update(clip_id, stage=ClipStage.VERIFYING, progress=0.95)
            return self.store.mark_ready(
                clip_id,
                output_path=result.path,
                size_bytes=result.size_bytes,
                caption_path=job.caption_path,
            )
        except AIError as exc:
            if self.store.is_cancel_requested(clip_id):
                if not self.config.keep_failed_artifacts:
                    self._cleanup_failed(clip_id)
                return self.store.mark_cancelled(clip_id)
            if not self.config.keep_failed_artifacts:
                self._cleanup_failed(clip_id)
            return self.store.fail(clip_id, exc.to_dict())
        except Exception as exc:  # pragma: no cover - defensive
            if not self.config.keep_failed_artifacts:
                self._cleanup_failed(clip_id)
            error = AIError(
                ErrorCategory.INTERNAL_ERROR,
                f"Unexpected clip rendering failure: {exc}",
                retryable=False,
            )
            return self.store.fail(clip_id, error.to_dict())

    def _write_render_diagnostics(
        self,
        directory: Path,
        *,
        job: ClipJob,
        edit_plan: EditPlan,
        result: RenderResult,
    ) -> None:
        payload = {
            "clip_id": job.id,
            "source_job_id": job.source_job_id,
            "start_time": job.start_time,
            "end_time": job.end_time,
            "duration": job.duration,
            "format": job.format.value,
            "width": job.width,
            "height": job.height,
            "fit": job.fit.value,
            "captions": job.captions,
            "caption_path": str(job.caption_path) if job.caption_path else None,
            "caption_style": {
                "font_size": self.config.caption_font_size
                or max(16, int(job.height * self.config.caption_font_size_ratio)),
                "border_style": self.config.caption_border_style,
                "back_colour": self.config.caption_back_colour,
                "encoding": self.config.caption_encoding,
            },
            "effects": [
                {
                    "kind": effect.kind,
                    "start": effect.start,
                    "end": effect.end,
                    "intensity": effect.intensity,
                }
                for effect in edit_plan.effects
            ],
            "effect_filters": list(result.effect_filters),
            "filter_graph": result.filter_graph,
            "output_path": str(result.path),
            "size_bytes": result.size_bytes,
            "rendered_duration": result.duration,
        }
        path = directory / "render_diagnostics.json"
        try:
            path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Could not write render diagnostics for %s: %s", job.id, exc)

    def _cleanup_failed(self, clip_id: str) -> None:
        with contextlib.suppress(OSError):
            shutil.rmtree(self.clip_dir(clip_id), ignore_errors=True)

    def process_next(self) -> ClipJob | None:
        job = self.store.claim_next_queued()
        if job is None:
            return None
        return self.process(job.id)

    def recover_interrupted(self) -> int:
        count = self.store.recover_interrupted()
        if not self.config.keep_failed_artifacts:
            for job in self.store.list(status=ClipStatus.CANCELLED, limit=500):
                self._cleanup_failed(job.id)
        return count

    def get(self, clip_id: str) -> ClipJob:
        return self.store.require(clip_id)

    def list(
        self,
        *,
        source_job_id: str | None = None,
        status: ClipStatus | None = None,
        limit: int = 50,
    ) -> list[ClipJob]:
        return self.store.list(source_job_id=source_job_id, status=status, limit=limit)

    def retry(self, clip_id: str) -> ClipJob:
        return self.store.retry(clip_id)

    def cancel(self, clip_id: str) -> ClipJob:
        return self.store.request_cancel(clip_id)

    def output_path(self, clip_id: str) -> Path:
        job = self.store.require(clip_id)
        if job.status != ClipStatus.READY or job.output_path is None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Clip job '{clip_id}' is not ready for download.",
                retryable=False,
            )
        path = Path(job.output_path).resolve()
        if not path.is_file():
            raise AIError(
                ErrorCategory.NOT_FOUND,
                "Rendered clip file is missing.",
                retryable=False,
            )
        return path

    def config_dict(self) -> dict[str, Any]:
        return self.config.model_dump(mode="json")
