"""Transcription and highlight-analysis orchestration for prepared video jobs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from clipping_ai.analysis.config import (
    AnalysisConfig,
    HighlightSettings,
    MultiPassSettings,
    load_analysis_config,
)
from clipping_ai.analysis.recovery import (
    AnalysisStageRequest,
    CandidateArtifact,
    DurableAnalysisRunner,
    LlmStageResult,
    prepare_candidate_artifact,
    run_llm_stage,
)
from clipping_ai.analysis.transcription import (
    DurableTranscriptionRunner,
    PreparedTranscriptionAudio,
)
from clipping_ai.audio import hash_file
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.highlights.candidates import Candidate
from clipping_ai.highlights.engine import HighlightPreparation, detect_highlights
from clipping_ai.highlights.models import ContentType, HighlightResult, LlmOutcome, MediaSignals
from clipping_ai.highlights.observability import DiscoveryObserver
from clipping_ai.jobs import TranscriptionCache
from clipping_ai.recovery.stages import StageStatus, StageStore
from clipping_ai.schemas import TranscriptResult, TranscriptSegment, TranscriptWord
from clipping_ai.settings import Settings, get_settings
from clipping_ai.video.config import VideoConfig, load_video_config
from clipping_ai.video.models import JobStatus, VideoJob
from clipping_ai.video.preparation import STAGE_CHUNKS, chunk_fingerprint, chunks_from_checkpoint
from clipping_ai.video.store import VideoJobStore

if TYPE_CHECKING:
    from clipping_ai.manager import ProviderManager


def _now() -> str:
    return datetime.now(UTC).isoformat()


class AnalysisService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        config: AnalysisConfig | None = None,
        video_config: VideoConfig | None = None,
        store: VideoJobStore | None = None,
        manager: ProviderManager | None = None,
        stage_store: StageStore | None = None,
        transcription_cache: TranscriptionCache | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self.config = config or load_analysis_config(self.settings.config_dir)
        self.video_config = video_config or load_video_config(self.settings.config_dir)
        self.store = store or VideoJobStore(self.settings.jobs_db_path)
        self.manager = manager
        self.stage_store = stage_store or StageStore(self.settings.jobs_db_path)
        self.transcription_cache = transcription_cache or TranscriptionCache(
            self.settings.jobs_db_path
        )

    def _job(self, job_id: str) -> VideoJob:
        job = self.store.require(job_id)
        if job.deleted_at is not None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "This source video is in the trash. Restore it before analyzing it.",
                retryable=False,
            )
        return job

    def _safe_data_path(self, path: Path | str) -> Path:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.settings.data_dir.resolve()):
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Job file path is outside the configured data directory.",
                retryable=False,
            )
        return resolved

    def _media_path(self, job: VideoJob) -> Path:
        if job.input_path is None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "The job has no input file.",
                retryable=False,
            )
        path = self._safe_data_path(job.input_path)
        if not path.is_file():
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "The job input file is missing.",
                retryable=False,
            )
        return path

    def _audio_path(self, job: VideoJob, media_path: Path) -> Path:
        if job.audio_path is None:
            return media_path
        path = self._safe_data_path(job.audio_path)
        return path if path.is_file() else media_path

    def _prepared_audio(self, job: VideoJob, audio_path: Path) -> PreparedTranscriptionAudio | None:
        """Return only a manifest that still matches the current audio and config."""
        if job.metadata is None:
            return None
        duration = job.metadata.duration_seconds
        if duration <= 0:
            return None
        try:
            audio_sha256 = hash_file(audio_path)
        except OSError:
            return None
        fingerprint = chunk_fingerprint(audio_sha256, duration, self.video_config)
        record = self.stage_store.get(job_type="video", job_id=job.id, stage=STAGE_CHUNKS)
        if (
            record is None
            or record.status is not StageStatus.DONE
            or record.input_fingerprint != fingerprint
        ):
            return None
        chunks_dir = self.settings.data_dir / "videos" / "jobs" / job.id / "audio" / "chunks"
        chunks = chunks_from_checkpoint(
            record.checkpoint,
            output_dir=chunks_dir,
            audio_sha256=audio_sha256,
            duration=duration,
            config=self.video_config,
        )
        if chunks is None:
            return None
        return PreparedTranscriptionAudio(
            source_sha256=audio_sha256,
            manifest_fingerprint=fingerprint,
            chunks=tuple(chunks),
        )

    def _normalise_transcript(
        self,
        transcript: TranscriptResult,
        *,
        duration: float | None,
    ) -> TranscriptResult:
        config = self.config.transcription
        limit = (
            float(duration)
            if config.clamp_timestamps and duration is not None and duration > 0
            else None
        )

        def clamp(value: float) -> float:
            if limit is None:
                return max(0.0, value)
            return min(max(0.0, value), limit)

        segments: list[TranscriptSegment] = []
        for segment in transcript.segments:
            start = clamp(segment.start)
            end = clamp(segment.end)
            end = max(start, end)
            if end <= start and segment.text.strip():
                continue
            words: list[TranscriptWord] | None = None
            if segment.words is not None:
                words = []
                for word in segment.words:
                    if (
                        word.probability is not None
                        and word.probability < config.min_word_probability
                    ):
                        continue
                    word_start = clamp(word.start)
                    word_end = max(word_start, clamp(word.end))
                    if word_end <= word_start:
                        continue
                    words.append(
                        TranscriptWord(
                            word=word.word,
                            start=word_start,
                            end=word_end,
                            probability=word.probability,
                        )
                    )
            segments.append(
                TranscriptSegment(
                    id=segment.id,
                    start=start,
                    end=end,
                    text=segment.text,
                    words=words,
                )
            )

        words = []
        for word in transcript.words:
            if word.probability is not None and word.probability < config.min_word_probability:
                continue
            start = clamp(word.start)
            end = max(start, clamp(word.end))
            if end <= start:
                continue
            words.append(
                TranscriptWord(
                    word=word.word,
                    start=start,
                    end=end,
                    probability=word.probability,
                )
            )

        text = transcript.text.strip()
        if not text:
            text = " ".join(segment.text.strip() for segment in segments)
        if len(text) > config.max_transcript_chars:
            text = text[: config.max_transcript_chars].rstrip()
        update: dict[str, object] = {
            "segments": segments,
            "words": words,
            "text": text,
        }
        if limit is not None:
            update["duration"] = limit
        elif transcript.duration is None and duration is not None:
            update["duration"] = duration
        return transcript.model_copy(update=update)

    def get_transcript(self, job_id: str) -> TranscriptResult | None:
        return self._job(job_id).transcript

    def transcribe(
        self,
        job_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        language: str | None = None,
        word_timestamps: bool | None = None,
        prompt: str | None = None,
        refresh: bool = False,
    ) -> VideoJob:
        job = self._job(job_id)
        if job.status not in {JobStatus.READY, JobStatus.PROCESSING}:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Video job '{job_id}' is {job.status.value}; prepare it first.",
                retryable=False,
            )
        if job.transcript is not None and not refresh:
            return job
        if self.manager is None:
            raise AIError(
                ErrorCategory.PROVIDER_UNAVAILABLE,
                "No provider manager is configured for transcription.",
                retryable=False,
            )
        media_path = self._media_path(job)
        audio_path = self._audio_path(job, media_path)
        analysis_runner = DurableAnalysisRunner.for_settings(
            self.settings, job.id, self.stage_store
        )
        if refresh:
            analysis_runner.reset_analysis_stages()
        config = self.config.transcription
        effective_language = language if language is not None else config.default_language
        effective_word_timestamps = (
            config.word_timestamps if word_timestamps is None else word_timestamps
        )
        force_retry = refresh or bool(provider or model)
        prepared: PreparedTranscriptionAudio | None = None
        supports_prepared = getattr(self.manager, "can_transcribe_prepared_chunks", None)
        if callable(supports_prepared) and supports_prepared(provider=provider, model=model):
            prepared = self._prepared_audio(job, audio_path)
        try:
            if prepared is not None:
                runner = DurableTranscriptionRunner(
                    manager=self.manager,
                    stage_store=self.stage_store,
                    cache=self.transcription_cache,
                    job_id=job.id,
                )
                response = runner.run(
                    prepared,
                    language=effective_language,
                    word_timestamps=effective_word_timestamps,
                    prompt=prompt,
                    requested_provider=provider,
                    requested_model=model,
                    force_retry=force_retry,
                )
            else:
                response = self.manager.transcribe(
                    audio_path,
                    provider=provider,
                    model=model,
                    language=effective_language,
                    word_timestamps=effective_word_timestamps,
                    prompt=prompt,
                )
            raw = response.get("transcript")
            transcript = TranscriptResult.model_validate(raw)
            transcript = transcript.model_copy(
                update={
                    "provider": transcript.provider or response.get("provider"),
                    "model": transcript.model or response.get("model"),
                }
            )
            transcript = analysis_runner.run_normalization(
                transcript,
                duration=job.metadata.duration_seconds if job.metadata else None,
                config=config,
                normalizer=lambda value, limit: self._normalise_transcript(
                    value, duration=limit
                ),
            )
            return self.store.update(
                job_id,
                transcript=transcript,
                transcript_error=None,
                transcript_provider=response.get("provider"),
                transcript_model=response.get("model"),
                transcript_at=_now(),
            )
        except AIError as exc:
            self.store.update(job_id, transcript_error=exc.message, transcript_at=_now())
            raise
        except Exception as exc:
            error = AIError(
                ErrorCategory.INTERNAL_ERROR,
                f"Transcription failed: {exc}",
                retryable=False,
                cause=exc,
            )
            self.store.update(job_id, transcript_error=error.message, transcript_at=_now())
            raise error from exc

    def _analysis_request(
        self,
        job: VideoJob,
        *,
        config: HighlightSettings,
        multi_pass_config: MultiPassSettings,
        content_type: ContentType,
        provider: str | None,
        model: str | None,
    ) -> AnalysisStageRequest:
        if job.metadata is None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "The job has no metadata; prepare it first.",
                retryable=False,
            )
        media_path = self._media_path(job)
        audio_path = self._audio_path(job, media_path)
        source_hint = " ".join(part for part in (job.original_filename, job.source_value) if part)
        return AnalysisStageRequest(
            job_id=job.id,
            media_path=media_path,
            audio_path=audio_path,
            metadata=job.metadata,
            transcript=job.transcript,
            config=config,
            content_type=content_type,
            provider=provider,
            model=model,
            ffmpeg_binary=self.video_config.ffmpeg_binary,
            source_hint=source_hint or None,
            manager=self.manager,
            multi_pass=multi_pass_config,
        )

    def highlights(
        self,
        job_id: str,
        *,
        content_type: ContentType = ContentType.AUTO,
        provider: str | None = None,
        model: str | None = None,
        use_llm: bool | None = None,
        refresh: bool = False,
        multi_pass: bool | None = None,
        window_seconds: float | None = None,
        overlap_seconds: float | None = None,
        max_highlights: int | None = None,
        observer: DiscoveryObserver | None = None,
    ) -> VideoJob:
        job = self._job(job_id)
        if job.status not in {JobStatus.READY, JobStatus.PROCESSING}:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Video job '{job_id}' is {job.status.value}; prepare it first.",
                retryable=False,
            )
        config_data = self.config.highlights.model_dump(mode="json")
        if use_llm is not None:
            config_data["use_llm"] = use_llm
        if max_highlights is not None:
            config_data["max_highlights"] = max_highlights
        multi_pass_data = self.config.multi_pass.model_dump(mode="json")
        if multi_pass is not None:
            multi_pass_data["enabled"] = multi_pass
        if window_seconds is not None:
            multi_pass_data["window_seconds"] = window_seconds
        if overlap_seconds is not None:
            multi_pass_data["overlap_seconds"] = overlap_seconds
        try:
            config = HighlightSettings.model_validate(config_data)
            multi_pass_config = MultiPassSettings.model_validate(multi_pass_data)
        except ValidationError as exc:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Invalid highlight analysis settings.",
                retryable=False,
                details={"validation": [error["msg"] for error in exc.errors()]},
            ) from exc
        runner = DurableAnalysisRunner.for_settings(self.settings, job.id, self.stage_store)
        request = self._analysis_request(
            job,
            config=config,
            multi_pass_config=multi_pass_config,
            content_type=content_type,
            provider=provider,
            model=model,
        )
        if job.highlight_result is not None and not refresh:
            if not multi_pass_config.enabled:
                return job
            if runner.cached_multi_pass_result(request) is not None:
                return job
            runner.clear_multi_pass_result()
            self.store.update(
                job.id,
                highlight_result=None,
                highlight_error=None,
                highlight_at=None,
            )
            job = self._job(job.id)
        if refresh:
            runner.reset_analysis_stages()
        metadata = request.metadata
        media_path = request.media_path
        source_hint = request.source_hint

        def prepare(signals: MediaSignals) -> CandidateArtifact:
            return prepare_candidate_artifact(
                job_id=job.id,
                media_path=media_path,
                metadata=metadata,
                transcript=job.transcript,
                config=config,
                content_type=content_type,
                manager=self.manager,
                signals=signals,
                source_hint=source_hint or None,
            )

        def run_llm(
            candidates: list[Candidate], signals: MediaSignals
        ) -> LlmStageResult:
            return run_llm_stage(
                self.manager,
                transcript=job.transcript,
                candidates=candidates,
                content_type=content_type,
                signals=signals,
                config=config,
                provider=provider,
                model=model,
            )

        def prepare_window(
            signals: MediaSignals, transcript: TranscriptResult | None
        ) -> CandidateArtifact:
            return prepare_candidate_artifact(
                job_id=job.id,
                media_path=media_path,
                metadata=metadata,
                transcript=transcript,
                config=config,
                content_type=content_type,
                manager=self.manager,
                signals=signals,
                source_hint=source_hint or None,
            )

        def run_llm_window(
            candidates: list[Candidate],
            signals: MediaSignals,
            transcript: TranscriptResult | None,
        ) -> LlmStageResult:
            return run_llm_stage(
                self.manager,
                transcript=transcript,
                candidates=candidates,
                content_type=content_type,
                signals=signals,
                config=config,
                provider=provider,
                model=model,
            )

        def assemble(
            candidate_artifact: CandidateArtifact,
            signals: MediaSignals,
            outcome: LlmOutcome,
        ) -> HighlightResult:
            resolved_type = (
                candidate_artifact.classification.content_type
                if candidate_artifact.classification is not None
                else content_type
            )
            preparation = HighlightPreparation(
                result=None,
                notes=list(candidate_artifact.notes),
                signals=signals,
                classification=candidate_artifact.classification,
                candidates=candidate_artifact.candidate_objects(),
                resolved_type=resolved_type,
            )
            return detect_highlights(
                job_id=job.id,
                media_path=media_path,
                metadata=metadata,
                transcript=job.transcript,
                config=config,
                content_type=content_type,
                manager=self.manager,
                provider=provider,
                model=model,
                ffmpeg_binary=self.video_config.ffmpeg_binary,
                signals=signals,
                classification=candidate_artifact.classification,
                candidates=candidate_artifact.candidate_objects(),
                llm_outcome=outcome,
                source_hint=source_hint or None,
                preparation=preparation,
            )

        try:
            if multi_pass_config.enabled:
                result = runner.run_multi_pass_highlights(
                    request,
                    prepare_window=prepare_window,
                    run_llm_window=run_llm_window,
                    observer=observer,
                )
            else:
                result = runner.run_highlights(
                    request,
                    prepare=prepare,
                    run_llm=run_llm,
                    assemble=assemble,
                )
            return self.store.update(
                job_id,
                highlight_result=result,
                highlight_error=None,
                highlight_at=_now(),
            )
        except AIError as exc:
            self.store.update(job_id, highlight_error=exc.message, highlight_at=_now())
            raise
        except Exception as exc:
            error = AIError(
                ErrorCategory.INTERNAL_ERROR,
                f"Highlight detection failed: {exc}",
                retryable=False,
                cause=exc,
            )
            self.store.update(job_id, highlight_error=error.message, highlight_at=_now())
            raise error from exc

    def analyze(
        self,
        job_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        language: str | None = None,
        content_type: ContentType = ContentType.AUTO,
        use_llm: bool | None = None,
        refresh: bool = False,
        multi_pass: bool | None = None,
        window_seconds: float | None = None,
        overlap_seconds: float | None = None,
        max_highlights: int | None = None,
        observer: DiscoveryObserver | None = None,
    ) -> VideoJob:
        job = self._job(job_id)
        if job.transcript is None or refresh:
            try:
                job = self.transcribe(
                    job_id,
                    provider=provider,
                    model=model,
                    language=language,
                    refresh=refresh,
                )
            except AIError:
                job = self._job(job_id)
        return self.highlights(
            job_id,
            content_type=content_type,
            provider=provider,
            model=model,
            use_llm=use_llm,
            refresh=refresh,
            multi_pass=multi_pass,
            window_seconds=window_seconds,
            overlap_seconds=overlap_seconds,
            max_highlights=max_highlights,
            observer=observer,
        )

    def discovery_progress(self, job_id: str) -> dict[str, object]:
        job = self._job(job_id)
        if job.metadata is None:
            return {
                "enabled": self.config.multi_pass.enabled,
                "total": 0,
                "complete": 0,
                "pending": 0,
                "running": 0,
                "failed": 0,
                "stale": 0,
                "next_window_id": None,
                "windows": [],
            }
        request = self._analysis_request(
            job,
            config=self.config.highlights,
            multi_pass_config=self.config.multi_pass,
            content_type=ContentType.AUTO,
            provider=None,
            model=None,
        )
        runner = DurableAnalysisRunner.for_settings(self.settings, job.id, self.stage_store)
        return runner.discovery_progress(request)

    def config_dict(self) -> dict[str, object]:
        highlights: HighlightSettings = self.config.highlights
        return {
            "transcription": self.config.transcription.model_dump(mode="json"),
            "highlights": highlights.model_dump(mode="json"),
            "multi_pass": self.config.multi_pass.model_dump(mode="json"),
        }
