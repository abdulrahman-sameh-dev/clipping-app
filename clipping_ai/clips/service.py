"""Clip job orchestration: create, render, cancel, retry, and recover."""

from __future__ import annotations

import builtins
import json
import logging
import shutil
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from clipping_ai.clips.config import ClipConfig, load_clip_config
from clipping_ai.clips.editing import EditPlan, build_edit_plan
from clipping_ai.clips.metadata import ClipMetadataService
from clipping_ai.clips.models import (
    ClipFit,
    ClipFormat,
    ClipJob,
    ClipMetadataStatus,
    ClipQuality,
    ClipRange,
    ClipStage,
    ClipStatus,
)
from clipping_ai.clips.recovery import (
    STAGE_CAPTIONS,
    STAGE_FINALIZE,
    STAGE_OUTPUT,
    STAGE_PREPARE,
    STAGE_VALIDATE,
    ClipStageRequest,
    DurableClipRunner,
    cleanup_clip_temporaries,
    preserved_clip_temporaries,
    release_clip_stages,
    reset_failed_clip_stages,
)
from clipping_ai.clips.renderer import MediaInfo, RenderResult, probe_media
from clipping_ai.clips.store import ClipJobStore
from clipping_ai.composition.engine import build_composition_plan
from clipping_ai.composition.models import VisualSettings
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.highlights.models import Highlight
from clipping_ai.recovery.idempotency import stable_fingerprint
from clipping_ai.recovery.stages import StageStore
from clipping_ai.settings import Settings, get_settings
from clipping_ai.video.ingest import is_safe_job_id
from clipping_ai.video.models import JobStatus, VideoJob
from clipping_ai.video.service import VideoService

logger = logging.getLogger(__name__)

_MAX_CLIP_RANGES = 100
_SOURCE_DURATION_TOLERANCE = 0.05
_DURATION_TOLERANCE = 1e-6


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _visual_settings(
    value: VisualSettings | dict[str, Any] | None,
) -> VisualSettings:
    if value is None:
        return VisualSettings()
    if isinstance(value, VisualSettings):
        return value
    try:
        return VisualSettings.model_validate(value)
    except Exception as exc:  # noqa: BLE001
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            f"Invalid visual settings: {exc}",
            retryable=False,
        ) from exc


class ClipService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        video_service: VideoService | None = None,
        config: ClipConfig | None = None,
        store: ClipJobStore | None = None,
        stage_store: StageStore | None = None,
        manager: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self.video = video_service or VideoService(self.settings)
        self.config = config or load_clip_config(self.settings.config_dir)
        self.store = store or ClipJobStore(self.settings.jobs_db_path)
        self.stage_store = stage_store or StageStore(self.settings.jobs_db_path)
        self.data_dir = self.settings.data_dir.resolve()
        self.clips_root = self.data_dir / "clips" / "jobs"
        self.clips_root.mkdir(parents=True, exist_ok=True)
        self.manager = manager
        self.metadata = ClipMetadataService(self.store, self.video, manager, self.config)
        self._render_lock = threading.Lock()
        self._metadata_lock = threading.Lock()
        self._metadata_sources: set[str] = set()
        self._metadata_reschedule: set[str] = set()

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
    def _normalize_ranges(
        ranges: Iterable[ClipRange | dict[str, Any]],
    ) -> list[ClipRange]:
        values = list(ranges)
        if not values:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "At least one clip range is required.",
                retryable=False,
            )
        if len(values) > _MAX_CLIP_RANGES:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"At most {_MAX_CLIP_RANGES} clip ranges are allowed.",
                retryable=False,
            )
        normalized: list[ClipRange] = []
        for value in values:
            if isinstance(value, ClipRange):
                normalized.append(value)
                continue
            try:
                normalized.append(ClipRange.model_validate(value))
            except ValidationError as exc:
                detail = exc.errors()[0].get("msg", "invalid value")
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    f"Invalid clip range: {detail}",
                    retryable=False,
                ) from exc
        return normalized

    def _validate_clip_bounds(
        self,
        start_time: float,
        end_time: float,
        source_duration: float,
        *,
        label: str,
        tolerance: float = 0.0,
    ) -> None:
        if start_time < 0 or end_time <= start_time:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"{label} has invalid timestamps: {start_time} - {end_time}.",
                retryable=False,
            )
        duration = end_time - start_time
        if duration + tolerance < self.config.min_clip_seconds:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"{label} is shorter than the minimum clip duration "
                f"({duration:.2f}s < {self.config.min_clip_seconds:.2f}s).",
                retryable=False,
            )
        if duration - tolerance > self.config.max_clip_seconds:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"{label} is longer than the maximum clip duration "
                f"({duration:.2f}s > {self.config.max_clip_seconds:.2f}s).",
                retryable=False,
            )
        if source_duration > 0 and end_time > source_duration + _SOURCE_DURATION_TOLERANCE:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"{label} end ({end_time:.2f}s) exceeds source duration "
                f"({source_duration:.2f}s).",
                retryable=False,
            )

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
        ranges: Iterable[ClipRange | dict[str, Any]] | None = None,
        highlight_indices: Iterable[int] | None = None,
        limit: int | None = None,
        visual_settings: VisualSettings | dict[str, Any] | None = None,
    ) -> list[ClipJob]:
        self._require_enabled()
        if not is_safe_job_id(source_job_id):
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Invalid video job ID.",
                retryable=False,
            )
        source_job = self.video.store.require(source_job_id)
        if source_job.deleted_at is not None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "This source video is in the trash. Restore it before creating clips.",
                retryable=False,
            )
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

        visual = _visual_settings(visual_settings)
        selected_formats = self._formats(formats, self.config)
        selected_quality = self._quality(quality, self.config)
        selected_fit = self._fit(fit, self.config)
        explicit_fit = fit is not None
        all_highlights = list(source_job.highlight_result.highlights)
        selected: list[tuple[int, float, float]]
        if ranges is not None:
            if highlight_indices is not None:
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    "ranges cannot be combined with highlight_indices.",
                    retryable=False,
                )
            if limit is not None:
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    "ranges cannot be combined with limit.",
                    retryable=False,
                )
            selected = []
            seen_indices: set[int] = set()
            for item in self._normalize_ranges(ranges):
                index = item.highlight_index
                if index >= len(all_highlights):
                    raise AIError(
                        ErrorCategory.INVALID_REQUEST,
                        f"Highlight index out of range: {index}.",
                        retryable=False,
                    )
                if index in seen_indices:
                    raise AIError(
                        ErrorCategory.INVALID_REQUEST,
                        f"Duplicate highlight index in clip ranges: {index}.",
                        retryable=False,
                    )
                seen_indices.add(index)
                selected.append((index, item.start_time, item.end_time))
        else:
            selected_highlights: list[tuple[int, Any]] = list(enumerate(all_highlights))
            if highlight_indices is not None:
                indices = list(highlight_indices)
                invalid = [
                    index for index in indices if index < 0 or index >= len(all_highlights)
                ]
                if invalid:
                    raise AIError(
                        ErrorCategory.INVALID_REQUEST,
                        f"Highlight index out of range: {invalid[0]}.",
                        retryable=False,
                    )
                selected_highlights = [
                    (index, all_highlights[index]) for index in indices
                ]
            if limit is not None:
                if limit < 1:
                    raise AIError(
                        ErrorCategory.INVALID_REQUEST,
                        "limit must be at least 1.",
                        retryable=False,
                    )
                selected_highlights = selected_highlights[:limit]
            if not selected_highlights:
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    "No highlights are available to render.",
                    retryable=False,
                )
            selected = [
                (index, highlight.start_time, highlight.end_time)
                for index, highlight in selected_highlights
            ]

        label = "Clip range" if ranges is not None else "Highlight"
        for _, start_time, end_time in selected:
            self._validate_clip_bounds(
                start_time,
                end_time,
                source_info.duration,
                label=label,
                tolerance=_DURATION_TOLERANCE if ranges is not None else 0.0,
            )

        created: list[ClipJob] = []
        for original_index, start_time, end_time in selected:
            for fmt in selected_formats:
                profile = self.config.format_profile(fmt)
                clip_fit = self._fit_for_format(
                    fmt,
                    selected_fit,
                    explicit=explicit_fit,
                    source_info=source_info,
                )
                clip_id = uuid4().hex
                clip, _duplicate = self.store.create_idempotent(
                    idempotency_key=stable_fingerprint(
                        scope="clip.create",
                        source_job_id=source_job.id,
                        highlight_index=original_index,
                        start_time=start_time,
                        end_time=end_time,
                        format=fmt.value,
                        quality=selected_quality.value,
                        fit=clip_fit.value,
                        captions=captions,
                        caption_language=caption_language,
                        visual_settings=visual.model_dump(mode="json"),
                    ),
                    id=clip_id,
                    source_job_id=source_job.id,
                    highlight_index=original_index,
                    start_time=start_time,
                    end_time=end_time,
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
                    visual_settings=visual.model_dump(mode="json"),
                    metadata_status=(
                        ClipMetadataStatus.PENDING
                        if self.config.metadata_auto_generate
                        else None
                    ),
                )
                created.append(clip)
        if created:
            if source_job.all_clips_published:
                self.video.set_all_clips_published(source_job.id, False)
            self._schedule_metadata_batch(source_job.id)
        return created

    def process(self, clip_id: str) -> ClipJob:
        with self._render_lock:
            return self._process(clip_id)

    def _process(self, clip_id: str) -> ClipJob:
        job = self.store.require(clip_id)
        if job.deleted_at is not None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Restore this clip before rendering it.",
                retryable=False,
            )
        if job.status == ClipStatus.READY:
            return job
        job = self.store.mark_processing(clip_id)
        if job.cancel_requested:
            self._cleanup_failed(clip_id)
            return self.store.mark_cancelled(clip_id)

        source_job = self.video.store.require(job.source_job_id)
        source_path = self._safe_source_path(source_job)
        source_info = self._source_info(source_job, source_path)
        directory = self.clip_dir(clip_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        highlight = self._highlight_for_clip(source_job, job)
        signals = (
            source_job.highlight_result.signals
            if source_job.highlight_result is not None
            else {}
        )
        base_plan = build_edit_plan(
            source_job.transcript,
            highlight,
            start_time=job.start_time,
            end_time=job.end_time,
            config=self.config,
            signals=signals,
            source_width=source_info.width,
            source_height=source_info.height,
        )
        visual = _visual_settings(job.visual_settings)
        composition = build_composition_plan(
            base_plan,
            highlight=highlight,
            start_time=job.start_time,
            end_time=job.end_time,
            settings=visual,
            config=self.config,
        )
        edit_plan = EditPlan(
            caption_cues=base_plan.caption_cues,
            effects=composition.effects,
            notes=base_plan.notes + composition.notes,
        )
        request = ClipStageRequest(
            job=job,
            source_job=source_job,
            source_path=source_path,
            source_info=source_info,
            config=self.config,
            visual=visual,
            edit_plan=edit_plan,
            composition=composition,
            highlight=highlight,
            signals=signals,
        )

        def report_stage(stage: str) -> None:
            if stage == STAGE_PREPARE:
                self.store.update(clip_id, stage=ClipStage.QUEUED, progress=0.05)
            elif stage == STAGE_CAPTIONS:
                self.store.update(clip_id, stage=ClipStage.CAPTIONS, progress=0.1)
            elif stage == STAGE_OUTPUT:
                self.store.update(clip_id, stage=ClipStage.RENDERING, progress=0.25)
            elif stage == STAGE_VALIDATE:
                self.store.update(clip_id, stage=ClipStage.VERIFYING, progress=0.95)
            elif stage == STAGE_FINALIZE:
                self.store.update(clip_id, stage=ClipStage.VERIFYING, progress=0.98)

        try:
            runner = DurableClipRunner(
                stage_store=self.stage_store,
                request=request,
                artifact_dir=directory,
                cancel_check=lambda: self.store.is_cancel_requested(clip_id),
                on_stage=report_stage,
            )
            result = runner.run()
            if self.store.is_cancel_requested(clip_id):
                self._cleanup_failed(clip_id)
                return self.store.mark_cancelled(clip_id)
            job = self.store.update(
                clip_id,
                caption_path=result.caption_path,
                caption_language=request.job.caption_language,
            )
            render = result.render or RenderResult(
                path=result.output_path,
                size_bytes=result.size_bytes,
                duration=job.duration,
            )
            self._write_render_diagnostics(
                directory,
                job=job,
                edit_plan=edit_plan,
                result=render,
                composition=composition,
            )
            logger.info(
                "Clip %s rendered with effects %s",
                clip_id,
                [effect.kind for effect in edit_plan.effects],
            )
            return self.store.mark_ready(
                clip_id,
                output_path=result.output_path,
                size_bytes=result.size_bytes,
                caption_path=result.caption_path,
            )
        except AIError as exc:
            if self.store.is_cancel_requested(clip_id):
                self._cleanup_failed(clip_id)
                return self.store.mark_cancelled(clip_id)
            if exc.details.get("reason") == "lease_held":
                raise
            self._cleanup_failed(clip_id)
            return self.store.fail(clip_id, exc.to_dict())
        except Exception as exc:  # pragma: no cover - defensive
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
        composition: Any | None = None,
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
            "composition": composition.to_dict() if composition is not None else None,
            "overlay_path": str(result.overlay_path) if result.overlay_path else None,
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
        directory = self.clip_dir(clip_id)
        if not directory.is_dir():
            return
        cleanup_clip_temporaries(
            directory,
            preserve=preserved_clip_temporaries(self.stage_store, clip_id, directory),
        )

    def _schedule_metadata_batch(self, source_job_id: str) -> None:
        if self.manager is None or not self.config.metadata_auto_generate:
            return
        with self._metadata_lock:
            if source_job_id in self._metadata_sources:
                self._metadata_reschedule.add(source_job_id)
                return
            self._metadata_sources.add(source_job_id)
        thread = threading.Thread(
            target=self._generate_metadata_batch_safely,
            args=(source_job_id,),
            daemon=True,
            name=f"clip-metadata-{source_job_id[:8]}",
        )
        thread.start()

    def _generate_metadata_batch_safely(self, source_job_id: str) -> None:
        try:
            while True:
                try:
                    self.metadata.generate_batch(source_job_id)
                except Exception as exc:  # pragma: no cover - defensive background task
                    logger.warning(
                        "Background metadata generation failed for source %s: %s",
                        source_job_id,
                        exc,
                    )
                with self._metadata_lock:
                    if source_job_id in self._metadata_reschedule:
                        self._metadata_reschedule.discard(source_job_id)
                        continue
                    self._metadata_sources.discard(source_job_id)
                    break
        except Exception:  # pragma: no cover - defensive background task
            with self._metadata_lock:
                self._metadata_sources.discard(source_job_id)
            logger.exception(
                "Background metadata worker crashed for source %s", source_job_id
            )

    def resume_pending_metadata(self) -> None:
        for source_job_id in self.store.pending_metadata_source_ids():
            self._schedule_metadata_batch(source_job_id)

    def generate_metadata_batch(
        self,
        source_job_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        force: bool = False,
        retry_failed: bool = False,
    ) -> list[ClipJob]:
        return self.metadata.generate_batch(
            source_job_id,
            provider=provider,
            model=model,
            force=force,
            retry_failed=retry_failed,
        )

    def process_next(self) -> ClipJob | None:
        job = self.store.claim_next_queued()
        if job is None:
            return None
        return self.process(job.id)

    def recover_interrupted(self) -> int:
        count = self.store.recover_interrupted()
        release_clip_stages(self.stage_store)
        for job in self.store.list(limit=1000):
            self._cleanup_failed(job.id)
        return count

    def get(self, clip_id: str) -> ClipJob:
        return self.store.require(clip_id)

    def list(
        self,
        *,
        source_job_id: str | None = None,
        status: ClipStatus | None = None,
        ids: list[str] | None = None,
        limit: int = 50,
        deleted: bool = False,
    ) -> list[ClipJob]:
        return self.store.list(
            source_job_id=source_job_id,
            status=status,
            ids=ids,
            limit=limit,
            deleted=deleted,
        )

    def list_deleted(self, *, limit: int = 500) -> builtins.list[ClipJob]:
        return self.store.list_deleted(limit=limit)

    def display_title(self, clip: ClipJob) -> str:
        if clip.title:
            return clip.title
        source = self.video.store.get(clip.source_job_id)
        if (
            source is not None
            and source.highlight_result is not None
            and clip.highlight_index is not None
            and 0 <= clip.highlight_index < len(source.highlight_result.highlights)
        ):
            return source.highlight_result.highlights[clip.highlight_index].title
        return clip.display_title()

    def retry(self, clip_id: str) -> ClipJob:
        job = self.store.require(clip_id)
        if job.deleted_at is not None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Restore this clip before retrying it.",
                retryable=False,
            )
        reset_failed_clip_stages(self.stage_store, clip_id)
        return self.store.retry(clip_id)

    def cancel(self, clip_id: str) -> ClipJob:
        job = self.store.require(clip_id)
        if job.deleted_at is not None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Restore this clip before cancelling it.",
                retryable=False,
            )
        return self.store.request_cancel(clip_id)

    def update_visual_settings(
        self,
        clip_id: str,
        settings: VisualSettings | dict[str, Any],
        *,
        rerender: bool = False,
    ) -> ClipJob:
        job = self.store.require(clip_id)
        if job.deleted_at is not None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Restore this clip before changing its visual settings.",
                retryable=False,
            )
        visual = _visual_settings(settings)
        fields: dict[str, Any] = {"visual_settings": visual.model_dump(mode="json")}
        if rerender and job.status == ClipStatus.READY:
            fields.update(
                status=ClipStatus.QUEUED,
                stage=ClipStage.QUEUED,
                progress=0.0,
                error=None,
                output_path=None,
                size_bytes=None,
                started_at=None,
                completed_at=None,
            )
        return self.store.update(clip_id, **fields)

    def generate_metadata(
        self,
        clip_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        force: bool = False,
    ) -> ClipJob:
        return self.metadata.generate(
            clip_id,
            provider=provider,
            model=model,
            force=force,
        )

    def update_metadata(
        self,
        clip_id: str,
        *,
        title: str,
        description: str,
        hashtags: builtins.list[str] | str | None,
        language: str | None = None,
    ) -> ClipJob:
        return self.metadata.update(
            clip_id,
            title=title,
            description=description,
            hashtags=hashtags,
            language=language,
        )

    def set_published(self, clip_id: str, published: bool) -> ClipJob:
        return self.metadata.set_published(clip_id, published)

    def soft_delete(self, clip_id: str) -> ClipJob:
        job = self.store.require(clip_id)
        if job.deleted_at is not None:
            return job
        return self.store.update(clip_id, deleted_at=_now())

    def restore(self, clip_id: str) -> ClipJob:
        job = self.store.require(clip_id)
        if job.deleted_at is None:
            return job
        return self.store.update(clip_id, deleted_at=None)

    def permanently_delete(self, clip_id: str) -> None:
        job = self.store.require(clip_id)
        if job.deleted_at is None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Move this clip to trash before deleting it permanently.",
                retryable=False,
            )
        if job.status in {ClipStatus.QUEUED, ClipStatus.PROCESSING}:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Wait for the clip job to stop before deleting it permanently.",
                retryable=False,
            )
        directory = self.clip_dir(clip_id).resolve()
        for path in (job.output_path, job.caption_path):
            if path is None:
                continue
            resolved = Path(path).resolve()
            if not resolved.is_relative_to(directory):
                raise AIError(
                    ErrorCategory.INTERNAL_ERROR,
                    "Refusing to delete a clip file outside its owned directory.",
                    retryable=False,
                )
        if directory.exists():
            shutil.rmtree(directory)
        self.store.delete_row(clip_id)

    def output_path(self, clip_id: str) -> Path:
        job = self.store.require(clip_id)
        if job.deleted_at is not None:
            raise AIError(
                ErrorCategory.NOT_FOUND,
                "This clip is in the trash. Restore it before downloading.",
                retryable=False,
            )
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
