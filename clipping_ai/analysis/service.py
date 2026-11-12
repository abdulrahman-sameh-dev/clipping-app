"""Transcription and highlight-analysis orchestration for prepared video jobs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from clipping_ai.analysis.config import AnalysisConfig, HighlightSettings, load_analysis_config
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.highlights.engine import detect_highlights
from clipping_ai.highlights.models import ContentType
from clipping_ai.schemas import TranscriptResult, TranscriptSegment, TranscriptWord
from clipping_ai.settings import Settings, get_settings
from clipping_ai.video.config import VideoConfig, load_video_config
from clipping_ai.video.models import JobStatus, VideoJob
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
    ) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self.config = config or load_analysis_config(self.settings.config_dir)
        self.video_config = video_config or load_video_config(self.settings.config_dir)
        self.store = store or VideoJobStore(self.settings.jobs_db_path)
        self.manager = manager

    def _job(self, job_id: str) -> VideoJob:
        return self.store.require(job_id)

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
        config = self.config.transcription
        try:
            response = self.manager.transcribe(
                audio_path,
                provider=provider,
                model=model,
                language=language if language is not None else config.default_language,
                word_timestamps=(
                    config.word_timestamps if word_timestamps is None else word_timestamps
                ),
                prompt=prompt,
            )
            raw = response.get("transcript")
            transcript = TranscriptResult.model_validate(raw)
            transcript = self._normalise_transcript(
                transcript,
                duration=job.metadata.duration_seconds if job.metadata else None,
            )
            transcript = transcript.model_copy(
                update={
                    "provider": transcript.provider or response.get("provider"),
                    "model": transcript.model or response.get("model"),
                }
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

    def highlights(
        self,
        job_id: str,
        *,
        content_type: ContentType = ContentType.AUTO,
        provider: str | None = None,
        model: str | None = None,
        use_llm: bool | None = None,
        refresh: bool = False,
    ) -> VideoJob:
        job = self._job(job_id)
        if job.status not in {JobStatus.READY, JobStatus.PROCESSING}:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Video job '{job_id}' is {job.status.value}; prepare it first.",
                retryable=False,
            )
        if job.highlight_result is not None and not refresh:
            return job
        if job.metadata is None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "The job has no metadata; prepare it first.",
                retryable=False,
            )
        media_path = self._media_path(job)
        config = self.config.highlights
        if use_llm is not None:
            config = config.model_copy(update={"use_llm": use_llm})
        source_hint = " ".join(
            part for part in (job.original_filename, job.source_value) if part
        )
        try:
            result = detect_highlights(
                job_id=job_id,
                media_path=media_path,
                metadata=job.metadata,
                transcript=job.transcript,
                config=config,
                content_type=content_type,
                manager=self.manager,
                provider=provider,
                model=model,
                ffmpeg_binary=self.video_config.ffmpeg_binary,
                source_hint=source_hint or None,
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
        )

    def config_dict(self) -> dict[str, object]:
        highlights: HighlightSettings = self.config.highlights
        return {
            "transcription": self.config.transcription.model_dump(mode="json"),
            "highlights": highlights.model_dump(mode="json"),
        }
