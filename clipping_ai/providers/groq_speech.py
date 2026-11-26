"""Groq Whisper speech-to-text provider.

Uses the official Groq Python SDK, supports verbose JSON with word/segment
timestamps, chunks oversized audio when ffmpeg is available, and persists
completed chunks so an interrupted job can resume.
"""

from __future__ import annotations

import logging
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clipping_ai.audio import AudioChunk, ffmpeg_available, hash_file, split_audio
from clipping_ai.errors import AIError, ErrorCategory, policy_violation
from clipping_ai.jobs import TranscriptionCache
from clipping_ai.providers.base import BaseSpeechProvider, ConnectionResult
from clipping_ai.providers.error_mapping import map_provider_exception
from clipping_ai.registry import ModelEntry
from clipping_ai.schemas import TranscriptResult, TranscriptSegment, TranscriptWord

logger = logging.getLogger(__name__)


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:
            pass
    if hasattr(value, "dict"):
        try:
            return value.dict()
        except Exception:
            pass
    return {}


class GroqSpeechProvider(BaseSpeechProvider):
    def __init__(
        self,
        provider_id: str,
        config: Any,
        *,
        secret_getter: Callable[[str], str | None],
        cache: TranscriptionCache,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        super().__init__(provider_id, config)
        self._secret_getter = secret_getter
        self._cache = cache
        self._client_factory = client_factory
        self._client: Any = None

    # ------------------------------------------------------------------ client
    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        env_var = self.config.api_key_env or "GROQ_API_KEY"
        api_key = self._secret_getter(env_var)
        if not api_key:
            raise AIError(
                ErrorCategory.MISSING_API_KEY,
                f"Groq speech provider is unavailable because {env_var} is not set.",
                provider=self.provider_id,
                retryable=False,
            )
        if self._client_factory is None:
            from groq import Groq

            self._client = Groq(api_key=api_key)
        else:
            self._client = self._client_factory(api_key)
        return self._client

    def available(self) -> tuple[bool, str]:
        if not self.config.enabled:
            return False, "provider disabled in configuration"
        env_var = self.config.api_key_env or "GROQ_API_KEY"
        if not self._secret_getter(env_var):
            return False, f"{env_var} is not set"
        return True, "ok"

    # -------------------------------------------------------------- transcription
    @property
    def supports_prepared_chunks(self) -> bool:
        return True

    def transcribe_chunk(
        self,
        entry: ModelEntry,
        chunk: AudioChunk,
        *,
        language: str | None = None,
        word_timestamps: bool = True,
        prompt: str | None = None,
        timeout: float | None = None,
    ) -> TranscriptResult:
        """Transcribe an already prepared chunk without re-splitting the source."""
        if not entry.free or not entry.is_enabled():
            raise policy_violation(
                "Blocked: only explicitly approved, enabled free models may execute.",
                provider=entry.provider,
                model=entry.id,
            )
        path = Path(chunk.path)
        if not path.exists() or not path.is_file():
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Audio chunk does not exist: {path}",
                provider=self.provider_id,
                model=entry.id,
                retryable=False,
            )
        if path.stat().st_size <= 0:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Audio chunk is empty: {path}",
                provider=self.provider_id,
                model=entry.id,
                retryable=False,
            )
        return self._transcribe_chunk(
            entry,
            chunk,
            language=language,
            word_timestamps=word_timestamps,
            prompt=prompt,
            timeout=timeout,
        )

    def transcribe(
        self,
        entry: ModelEntry,
        audio_path: Path,
        *,
        language: str | None = None,
        word_timestamps: bool = True,
        prompt: str | None = None,
        timeout: float | None = None,
    ) -> TranscriptResult:
        if not entry.free or not entry.is_enabled():
            raise policy_violation(
                "Blocked: only explicitly approved, enabled free models may execute.",
                provider=entry.provider,
                model=entry.id,
            )
        audio_path = Path(audio_path)
        if not audio_path.exists() or not audio_path.is_file():
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Audio file does not exist: {audio_path}",
                provider=self.provider_id,
                model=entry.id,
                retryable=False,
            )

        source_hash = hash_file(audio_path)
        file_size = audio_path.stat().st_size
        needs_chunking = file_size > self.config.max_audio_bytes
        temp_dir: tempfile.TemporaryDirectory[str] | None = None
        try:
            if needs_chunking:
                if not ffmpeg_available(self.config.ffmpeg_binary):
                    raise AIError(
                        ErrorCategory.INVALID_REQUEST,
                        f"Audio is {file_size / 1_000_000:.1f} MB, above the "
                        f"{self.config.max_audio_bytes / 1_000_000:.0f} MB limit, and ffmpeg "
                        "is not installed. Install ffmpeg or supply a smaller file.",
                        provider=self.provider_id,
                        model=entry.id,
                        retryable=False,
                    )
                temp_dir = tempfile.TemporaryDirectory(prefix="clipping-ai-chunks-")
                chunks = split_audio(
                    audio_path,
                    chunk_seconds=self.config.chunk_seconds,
                    overlap_seconds=self.config.chunk_overlap_seconds,
                    ffmpeg_binary=self.config.ffmpeg_binary,
                    output_dir=Path(temp_dir.name),
                    force_reencode=True,
                )
            else:
                chunks = [AudioChunk(index=0, path=audio_path, offset=0.0)]

            results: list[TranscriptResult] = []
            for position, chunk in enumerate(chunks):
                cache_key = self._cache_key(
                    source_hash, entry, chunk, language, word_timestamps, prompt
                )
                cached = self._cache.get(cache_key)
                if cached is not None:
                    results.append(cached)
                    continue
                try:
                    result = self._transcribe_chunk(
                        entry,
                        chunk,
                        language=language,
                        word_timestamps=word_timestamps,
                        prompt=prompt,
                        timeout=timeout,
                    )
                except AIError as exc:
                    exc.details.setdefault("completed_chunks", position)
                    exc.details.setdefault("total_chunks", len(chunks))
                    exc.details.setdefault("resume_supported", True)
                    raise
                self._cache.put(
                    cache_key,
                    provider=self.provider_id,
                    model=entry.id,
                    source_hash=source_hash,
                    chunk_index=chunk.index,
                    offset=chunk.offset,
                    result=result,
                )
                results.append(result)

            merged = self._merge(entry, results, chunks)
            return merged
        finally:
            if temp_dir is not None:
                temp_dir.cleanup()

    def _cache_key(
        self,
        source_hash: str,
        entry: ModelEntry,
        chunk: AudioChunk,
        language: str | None,
        word_timestamps: bool,
        prompt: str | None,
    ) -> str:
        return "|".join(
            [
                source_hash,
                self.provider_id,
                entry.id,
                str(chunk.index),
                f"{chunk.offset:.3f}",
                language or "",
                "1" if word_timestamps else "0",
                prompt or "",
            ]
        )

    def _transcribe_chunk(
        self,
        entry: ModelEntry,
        chunk: AudioChunk,
        *,
        language: str | None,
        word_timestamps: bool,
        prompt: str | None,
        timeout: float | None,
    ) -> TranscriptResult:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": entry.provider_model_id,
            "response_format": "verbose_json",
            "temperature": 0.0,
            "timestamp_granularities": ["word", "segment"] if word_timestamps else ["segment"],
        }
        if language:
            kwargs["language"] = language
        if prompt:
            kwargs["prompt"] = prompt
        if timeout or self.config.timeout_seconds:
            kwargs["timeout"] = timeout or self.config.timeout_seconds

        try:
            with chunk.path.open("rb") as fh:
                data = fh.read()
            response = client.audio.transcriptions.create(
                file=(chunk.path.name, data), **kwargs
            )
        except BaseException as exc:  # noqa: BLE001
            raise map_provider_exception(
                exc, provider=self.provider_id, model=entry.id, task="speech_to_text"
            ) from exc

        payload = _as_dict(response)
        return self._parse_chunk(payload, entry, chunk)

    def _parse_chunk(
        self, payload: dict[str, Any], entry: ModelEntry, chunk: AudioChunk
    ) -> TranscriptResult:
        offset = chunk.offset
        text = str(payload.get("text") or "").strip()
        language = payload.get("language")
        duration = payload.get("duration")
        if not isinstance(duration, (int, float)):
            duration = chunk.duration

        segments: list[TranscriptSegment] = []
        for raw in payload.get("segments") or []:
            seg = _as_dict(raw)
            try:
                start = float(seg.get("start", 0.0)) + offset
                end = float(seg.get("end", start - offset)) + offset
            except (TypeError, ValueError):
                continue
            seg_text = str(seg.get("text") or "").strip()
            if not seg_text:
                continue
            segments.append(
                TranscriptSegment(
                    id=len(segments),
                    start=max(start, 0.0),
                    end=max(end, start),
                    text=seg_text,
                )
            )

        words: list[TranscriptWord] = []
        for raw in payload.get("words") or []:
            word = _as_dict(raw)
            token = str(word.get("word") or word.get("text") or "").strip()
            if not token:
                continue
            try:
                start = float(word.get("start", 0.0)) + offset
                end = float(word.get("end", start - offset)) + offset
            except (TypeError, ValueError):
                continue
            probability = word.get("probability")
            words.append(
                TranscriptWord(
                    word=token,
                    start=max(start, 0.0),
                    end=max(end, start),
                    probability=float(probability)
                    if isinstance(probability, (int, float))
                    else None,
                )
            )

        if not segments and text:
            end = offset + float(duration) if isinstance(duration, (int, float)) else offset
            segments = [TranscriptSegment(id=0, start=offset, end=max(end, offset), text=text)]

        return TranscriptResult(
            text=text,
            language=str(language) if language else None,
            duration=float(duration) if isinstance(duration, (int, float)) else None,
            segments=segments,
            words=words,
            provider=self.provider_id,
            model=entry.id,
        )

    def _merge(
        self,
        entry: ModelEntry,
        results: list[TranscriptResult],
        chunks: list[AudioChunk],
    ) -> TranscriptResult:
        texts = [r.text.strip() for r in results if r.text and r.text.strip()]
        segments: list[TranscriptSegment] = []
        words: list[TranscriptWord] = []
        seen_words: set[tuple[float, float, str]] = set()
        for result in results:
            for segment in result.segments:
                segments.append(
                    TranscriptSegment(
                        id=len(segments),
                        start=segment.start,
                        end=segment.end,
                        text=segment.text,
                    )
                )
            for word in result.words:
                key = (round(word.start, 2), round(word.end, 2), word.word)
                if key in seen_words:
                    continue
                seen_words.add(key)
                words.append(word)

        language = next((r.language for r in results if r.language), None)
        duration: float | None
        if segments:
            duration = max(segment.end for segment in segments)
        elif words:
            duration = max(word.end for word in words)
        else:
            duration = sum(chunk.duration or 0.0 for chunk in chunks) or None

        return TranscriptResult(
            text=" ".join(texts),
            language=language,
            duration=duration,
            segments=segments,
            words=words,
            provider=self.provider_id,
            model=entry.id,
        )

    # --------------------------------------------------------------- connection
    def test_connection(self, entry: ModelEntry | None = None) -> ConnectionResult:
        started = time.perf_counter()
        try:
            client = self._get_client()
            models = client.models.list()
            items = getattr(models, "data", models)
            if not isinstance(items, list):
                items = list(items)
            ids = [getattr(item, "id", None) or _as_dict(item).get("id") for item in items]
            whisper_ids = [mid for mid in ids if isinstance(mid, str) and "whisper" in mid]
        except AIError as exc:
            return ConnectionResult(
                ok=False,
                provider=self.provider_id,
                model=entry.id if entry else None,
                message=exc.message,
                latency_ms=int((time.perf_counter() - started) * 1000),
                details={"category": exc.category.value},
            )
        except BaseException as exc:  # noqa: BLE001
            mapped = map_provider_exception(exc, provider=self.provider_id)
            return ConnectionResult(
                ok=False,
                provider=self.provider_id,
                model=entry.id if entry else None,
                message=mapped.message,
                latency_ms=int((time.perf_counter() - started) * 1000),
                details={"category": mapped.category.value},
            )
        return ConnectionResult(
            ok=True,
            provider=self.provider_id,
            model=entry.id if entry else None,
            message="Connected to Groq; speech models visible.",
            latency_ms=int((time.perf_counter() - started) * 1000),
            details={"whisper_models": whisper_ids},
        )
