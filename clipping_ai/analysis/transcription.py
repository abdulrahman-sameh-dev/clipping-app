"""Durable per-chunk transcription orchestration.

The durable unit is one validated Phase 5B audio chunk.  Completed chunk
results live in ``TranscriptionCache``; ``StageStore`` records the attempt,
status, error classification and a reference to the cache entry.  The final
transcript is always assembled from those chunk results, so it can be rebuilt
without provider calls when only the assembled artifact is missing.

Provider requests are at-least-once.  A crash after a provider accepted a
request but before its response was persisted can cause that chunk to be sent
again on recovery; exactly-once provider execution is not claimed.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import ValidationError

from clipping_ai.audio import AudioChunk, hash_file
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.jobs import TranscriptionCache
from clipping_ai.recovery.idempotency import stable_fingerprint
from clipping_ai.recovery.stages import (
    StageError,
    StageOwnershipError,
    StageRecord,
    StageStatus,
    StageStore,
)
from clipping_ai.schemas import TranscriptResult, TranscriptSegment, TranscriptWord
from clipping_ai.video.models import AudioChunkInfo

if TYPE_CHECKING:
    from clipping_ai.manager import ProviderManager

ANALYSIS_JOB_TYPE = "analysis"
STAGE_TRANSCRIBE = "transcribe"

_TRANSCRIPTION_VERSION = 1
_TIMESTAMP_TOLERANCE = 1e-3
_RETRYABLE_STAGE_CATEGORIES = frozenset(
    {
        ErrorCategory.RATE_LIMIT_EXCEEDED,
        ErrorCategory.FREE_QUOTA_EXHAUSTED,
        ErrorCategory.MODEL_UNAVAILABLE,
        ErrorCategory.PROVIDER_UNAVAILABLE,
        ErrorCategory.NETWORK_TIMEOUT,
        ErrorCategory.INVALID_RESPONSE,
        ErrorCategory.SCHEMA_VALIDATION_FAILED,
        ErrorCategory.UNKNOWN_PROVIDER_ERROR,
    }
)


@dataclass(frozen=True)
class PreparedTranscriptionAudio:
    """Validated Phase 5B audio and its deterministic chunk manifest."""

    source_sha256: str
    manifest_fingerprint: str
    chunks: tuple[AudioChunkInfo, ...]


@dataclass(frozen=True)
class TranscriptChunkPlan:
    chunk: AudioChunkInfo
    sha256: str

    @property
    def unit_key(self) -> str:
        return chunk_unit_key(self.chunk.index)


@dataclass(frozen=True)
class _ChunkOutcome:
    result: TranscriptResult
    used_fallback: bool = False
    attempts: tuple[dict[str, Any], ...] = ()


def chunk_unit_key(index: int) -> str:
    return f"chunk-{index:06d}"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _result_sha256(result: TranscriptResult) -> str:
    return _sha256_text(result.model_dump_json())


def transcription_config_fingerprint(
    *,
    source_sha256: str,
    manifest_fingerprint: str,
    chunk: AudioChunkInfo,
    chunk_sha256: str,
    language: str | None,
    word_timestamps: bool,
    prompt: str | None,
    requested_provider: str | None,
    requested_model: str | None,
) -> str:
    return stable_fingerprint(
        scope="analysis.transcribe",
        version=_TRANSCRIPTION_VERSION,
        source_sha256=source_sha256,
        manifest_fingerprint=manifest_fingerprint,
        chunk_index=chunk.index,
        chunk_offset=round(float(chunk.offset), 6),
        chunk_duration=round(float(chunk.duration), 6),
        chunk_sha256=chunk_sha256,
        language=language or "",
        word_timestamps=word_timestamps,
        prompt_sha256=_sha256_text(prompt) if prompt else "",
        requested_provider=requested_provider or "",
        requested_model=requested_model or "",
    )


def chunk_cache_key(
    *,
    source_sha256: str,
    provider: str,
    model: str,
    chunk: AudioChunkInfo,
    chunk_sha256: str,
    language: str | None,
    word_timestamps: bool,
    prompt: str | None,
) -> str:
    """Deterministic cache identity, with prompt content reduced to a hash."""
    return "|".join(
        [
            "v1",
            source_sha256,
            provider,
            model,
            str(chunk.index),
            f"{float(chunk.offset):.3f}",
            f"{float(chunk.duration):.3f}",
            chunk_sha256,
            language or "",
            "1" if word_timestamps else "0",
            _sha256_text(prompt) if prompt else "",
        ]
    )


def stage_retryable(error: AIError) -> bool:
    """Provider/network/quota failures stay recoverable; config errors do not."""
    return error.retryable or error.category in _RETRYABLE_STAGE_CATEGORIES


def _finite(value: float) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _validate_span(start: float, end: float, *, offset: float, kind: str) -> None:
    if not _finite(start) or not _finite(end):
        raise AIError(
            ErrorCategory.INVALID_RESPONSE,
            f"Provider returned a non-finite {kind} timestamp.",
            retryable=True,
            details={"start": start, "end": end, "offset": offset, "kind": kind},
        )
    if start < 0 or end < 0:
        raise AIError(
            ErrorCategory.INVALID_RESPONSE,
            f"Provider returned a negative {kind} timestamp.",
            retryable=True,
            details={"start": start, "end": end, "offset": offset, "kind": kind},
        )
    if end < start:
        raise AIError(
            ErrorCategory.INVALID_RESPONSE,
            f"Provider returned {kind} end before start.",
            retryable=True,
            details={"start": start, "end": end, "offset": offset, "kind": kind},
        )
    if offset > 0 and start + _TIMESTAMP_TOLERANCE < offset:
        raise AIError(
            ErrorCategory.INVALID_RESPONSE,
            "Provider timestamps were not mapped to the source timeline.",
            retryable=True,
            details={"start": start, "offset": offset, "kind": kind},
        )


def validate_chunk_result(result: TranscriptResult, chunk: AudioChunkInfo) -> None:
    """Reject malformed timestamps before they enter the durable cache."""
    if result.duration is not None and not _finite(result.duration):
        raise AIError(
            ErrorCategory.INVALID_RESPONSE,
            "Provider returned a non-finite transcript duration.",
            retryable=True,
            details={"chunk_index": chunk.index},
        )
    for segment in result.segments:
        _validate_span(segment.start, segment.end, offset=chunk.offset, kind="segment")
    for word in result.words:
        _validate_span(word.start, word.end, offset=chunk.offset, kind="word")
        if word.probability is not None and not _finite(word.probability):
            raise AIError(
                ErrorCategory.INVALID_RESPONSE,
                "Provider returned a non-finite word probability.",
                retryable=True,
                details={"chunk_index": chunk.index},
            )


def merge_transcripts(
    plans: list[TranscriptChunkPlan],
    results: dict[int, TranscriptResult],
) -> TranscriptResult:
    """Assemble chunk results in deterministic order with global timestamps."""
    ordered = sorted(plans, key=lambda plan: plan.chunk.index)
    texts: list[str] = []
    segments: list[TranscriptSegment] = []
    words: list[TranscriptWord] = []
    seen_segments: set[tuple[float, float, str]] = set()
    seen_words: set[tuple[float, float, str]] = set()
    providers: list[str] = []
    models: list[str] = []
    language: str | None = None

    for plan in ordered:
        result = results[plan.chunk.index]
        if result.text.strip():
            texts.append(result.text.strip())
        if language is None and result.language:
            language = result.language
        if result.provider:
            providers.append(result.provider)
        if result.model:
            models.append(result.model)
        for segment in result.segments:
            key = (
                round(float(segment.start), 3),
                round(float(segment.end), 3),
                segment.text,
            )
            if key in seen_segments:
                continue
            seen_segments.add(key)
            segments.append(
                TranscriptSegment(
                    id=len(segments),
                    start=segment.start,
                    end=segment.end,
                    text=segment.text,
                    words=segment.words,
                )
            )
        for word in result.words:
            key = (
                round(float(word.start), 2),
                round(float(word.end), 2),
                word.word,
            )
            if key in seen_words:
                continue
            seen_words.add(key)
            words.append(word)

    duration: float | None
    if segments:
        duration = max(segment.end for segment in segments)
    elif words:
        duration = max(word.end for word in words)
    else:
        duration = sum(plan.chunk.duration for plan in ordered) or None

    provider = providers[0] if providers else None
    model = models[0] if models else None
    return TranscriptResult(
        text=" ".join(texts),
        language=language,
        duration=duration,
        segments=segments,
        words=words,
        provider=provider,
        model=model,
    )


@dataclass
class DurableTranscriptionRunner:
    manager: ProviderManager
    stage_store: StageStore
    cache: TranscriptionCache
    job_id: str
    owner: str = field(default_factory=lambda: f"analysis-transcribe-{uuid4().hex}")
    lease_seconds: float = 900.0
    heartbeat_seconds: float = 60.0

    def run(
        self,
        prepared: PreparedTranscriptionAudio,
        *,
        language: str | None,
        word_timestamps: bool,
        prompt: str | None,
        requested_provider: str | None = None,
        requested_model: str | None = None,
        force_retry: bool = False,
    ) -> dict[str, Any]:
        plans = self._plans(prepared)
        by_unit = {plan.unit_key: plan for plan in plans}
        outcomes: dict[int, _ChunkOutcome] = {}
        if not plans:
            merged = merge_transcripts(plans, {})
            return self._response(merged, attempts=[], used_fallback=False)

        for plan in plans:
            if plan.chunk.index in outcomes:
                continue
            fingerprint = self._fingerprint(
                prepared,
                plan,
                language=language,
                word_timestamps=word_timestamps,
                prompt=prompt,
                requested_provider=requested_provider,
                requested_model=requested_model,
            )
            record = self.stage_store.ensure(
                job_type=ANALYSIS_JOB_TYPE,
                job_id=self.job_id,
                stage=STAGE_TRANSCRIBE,
                unit_key=plan.unit_key,
                input_fingerprint=fingerprint,
            )
            if record.input_fingerprint != fingerprint:
                if record.status == StageStatus.RUNNING and not record.lease_expired:
                    raise self._busy_error(plan)
                try:
                    record = self.stage_store.reset(
                        job_type=ANALYSIS_JOB_TYPE,
                        job_id=self.job_id,
                        stage=STAGE_TRANSCRIBE,
                        unit_key=plan.unit_key,
                        input_fingerprint=fingerprint,
                        clear_checkpoint=True,
                    )
                except StageOwnershipError as exc:
                    raise self._busy_error(plan, exc) from exc

            if record.status == StageStatus.DONE:
                if not force_retry:
                    cached = self._cached_result(record, plan, fingerprint)
                    if cached is not None:
                        outcomes[plan.chunk.index] = _ChunkOutcome(cached)
                        continue
                record = self.stage_store.reset(
                    job_type=ANALYSIS_JOB_TYPE,
                    job_id=self.job_id,
                    stage=STAGE_TRANSCRIBE,
                    unit_key=plan.unit_key,
                    input_fingerprint=fingerprint,
                    clear_checkpoint=True,
                )

            if record.status == StageStatus.FAILED:
                if force_retry:
                    record = self.stage_store.reset(
                        job_type=ANALYSIS_JOB_TYPE,
                        job_id=self.job_id,
                        stage=STAGE_TRANSCRIBE,
                        unit_key=plan.unit_key,
                        input_fingerprint=fingerprint,
                        clear_checkpoint=True,
                    )
                else:
                    raise self._stored_error(record, plan)

            if record.status == StageStatus.RUNNING and not record.lease_expired:
                raise self._busy_error(plan)

            if record.status == StageStatus.PENDING and not force_retry:
                wait = self._retry_wait(record)
                if wait > 0:
                    raise self._retry_wait_error(record, plan, wait)

            try:
                claimed = self.stage_store.claim_next(
                    job_type=ANALYSIS_JOB_TYPE,
                    job_id=self.job_id,
                    stage=STAGE_TRANSCRIBE,
                    unit_key=plan.unit_key,
                    owner=self.owner,
                    lease_seconds=self.lease_seconds,
                )
            except StageError as exc:
                raise self._busy_error(plan, exc) from exc
            if claimed is None:
                raise AIError(
                    ErrorCategory.INTERNAL_ERROR,
                    "Could not claim the next transcription chunk.",
                    retryable=True,
                    details={"chunk_index": plan.chunk.index, "reason": "claim_lost"},
                )
            claimed_plan = by_unit.get(claimed.unit_key)
            if claimed_plan is None:
                raise AIError(
                    ErrorCategory.INTERNAL_ERROR,
                    f"Claimed unknown transcription chunk '{claimed.unit_key}'.",
                    retryable=True,
                )
            claimed_fingerprint = self._fingerprint(
                prepared,
                claimed_plan,
                language=language,
                word_timestamps=word_timestamps,
                prompt=prompt,
                requested_provider=requested_provider,
                requested_model=requested_model,
            )
            outcomes[claimed_plan.chunk.index] = self._process(
                claimed,
                claimed_plan,
                fingerprint=claimed_fingerprint,
                source_sha256=prepared.source_sha256,
                language=language,
                word_timestamps=word_timestamps,
                prompt=prompt,
                requested_provider=requested_provider,
                requested_model=requested_model,
            )

        missing = [plan.chunk.index for plan in plans if plan.chunk.index not in outcomes]
        if missing:
            raise AIError(
                ErrorCategory.INTERNAL_ERROR,
                "Transcription ended with incomplete chunks.",
                retryable=True,
                details={"missing_chunks": missing},
            )
        merged = merge_transcripts(
            plans, {index: outcome.result for index, outcome in outcomes.items()}
        )
        return self._response(
            merged,
            attempts=[attempt for outcome in outcomes.values() for attempt in outcome.attempts],
            used_fallback=any(outcome.used_fallback for outcome in outcomes.values()),
        )

    def _plans(self, prepared: PreparedTranscriptionAudio) -> list[TranscriptChunkPlan]:
        plans: list[TranscriptChunkPlan] = []
        for chunk in prepared.chunks:
            try:
                sha256 = hash_file(Path(chunk.path))
            except OSError as exc:
                raise AIError(
                    ErrorCategory.INVALID_REQUEST,
                    f"Audio chunk is missing or unreadable: {chunk.path}",
                    retryable=False,
                    cause=exc,
                    details={"chunk_index": chunk.index},
                ) from exc
            plans.append(TranscriptChunkPlan(chunk=chunk, sha256=sha256))
        return sorted(plans, key=lambda plan: plan.chunk.index)

    def _fingerprint(
        self,
        prepared: PreparedTranscriptionAudio,
        plan: TranscriptChunkPlan,
        *,
        language: str | None,
        word_timestamps: bool,
        prompt: str | None,
        requested_provider: str | None,
        requested_model: str | None,
    ) -> str:
        return transcription_config_fingerprint(
            source_sha256=prepared.source_sha256,
            manifest_fingerprint=prepared.manifest_fingerprint,
            chunk=plan.chunk,
            chunk_sha256=plan.sha256,
            language=language,
            word_timestamps=word_timestamps,
            prompt=prompt,
            requested_provider=requested_provider,
            requested_model=requested_model,
        )

    def _checkpoint(
        self,
        plan: TranscriptChunkPlan,
        *,
        fingerprint: str,
        source_sha256: str,
        provider: str,
        model: str,
        cache_key: str,
        result_sha256: str,
        attempt: int,
    ) -> dict[str, Any]:
        return {
            "version": _TRANSCRIPTION_VERSION,
            "job_id": self.job_id,
            "source_sha256": source_sha256,
            "config_fingerprint": fingerprint,
            "chunk": {
                "index": plan.chunk.index,
                "offset": plan.chunk.offset,
                "duration": plan.chunk.duration,
                "sha256": plan.sha256,
                "size_bytes": plan.chunk.size_bytes,
            },
            "provider": provider,
            "model": model,
            "cache_key": cache_key,
            "result_sha256": result_sha256,
            "attempt": attempt,
        }

    def _cached_result(
        self,
        record: StageRecord,
        plan: TranscriptChunkPlan,
        fingerprint: str,
    ) -> TranscriptResult | None:
        checkpoint = record.checkpoint
        if not isinstance(checkpoint, dict):
            return None
        if checkpoint.get("version") != _TRANSCRIPTION_VERSION:
            return None
        if checkpoint.get("job_id") != self.job_id:
            return None
        if checkpoint.get("config_fingerprint") != fingerprint:
            return None
        chunk = checkpoint.get("chunk")
        if not isinstance(chunk, dict) or chunk.get("sha256") != plan.sha256:
            return None
        cache_key = checkpoint.get("cache_key")
        if not isinstance(cache_key, str) or not cache_key:
            return None
        result = self.cache.get(cache_key)
        if result is None:
            return None
        expected = checkpoint.get("result_sha256")
        if not isinstance(expected, str) or _result_sha256(result) != expected:
            return None
        try:
            validate_chunk_result(result, plan.chunk)
        except AIError:
            return None
        return result

    def _process(
        self,
        record: StageRecord,
        plan: TranscriptChunkPlan,
        *,
        fingerprint: str,
        source_sha256: str,
        language: str | None,
        word_timestamps: bool,
        prompt: str | None,
        requested_provider: str | None,
        requested_model: str | None,
    ) -> _ChunkOutcome:
        cached = self._cached_result(record, plan, fingerprint)
        if cached is not None:
            checkpoint = record.checkpoint
            assert isinstance(checkpoint, dict)
            try:
                self.stage_store.complete(
                    job_type=ANALYSIS_JOB_TYPE,
                    job_id=self.job_id,
                    stage=STAGE_TRANSCRIBE,
                    unit_key=plan.unit_key,
                    owner=self.owner,
                    checkpoint=checkpoint,
                )
            except StageOwnershipError as exc:
                raise self._lease_lost_error(plan, exc) from exc
            return _ChunkOutcome(cached)

        stop = threading.Event()
        thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(plan, stop),
            name=f"clipping-transcribe-{plan.chunk.index}",
            daemon=True,
        )
        thread.start()
        try:
            chunk = AudioChunk(
                index=plan.chunk.index,
                path=Path(plan.chunk.path),
                offset=plan.chunk.offset,
                duration=plan.chunk.duration,
            )
            response = self.manager.transcribe_chunk(
                chunk,
                provider=requested_provider,
                model=requested_model,
                language=language,
                word_timestamps=word_timestamps,
                prompt=prompt,
            )
            raw = response.get("transcript")
            try:
                result = TranscriptResult.model_validate(raw)
            except ValidationError as exc:
                raise AIError(
                    ErrorCategory.INVALID_RESPONSE,
                    "Provider returned an invalid transcript payload.",
                    retryable=True,
                    cause=exc,
                    details={"chunk_index": plan.chunk.index},
                ) from exc
            provider = str(response.get("provider") or result.provider or "")
            model = str(response.get("model") or result.model or "")
            result = result.model_copy(
                update={
                    "provider": result.provider or provider or None,
                    "model": result.model or model or None,
                }
            )
            validate_chunk_result(result, plan.chunk)
            cache_key = chunk_cache_key(
                source_sha256=source_sha256,
                provider=provider,
                model=model,
                chunk=plan.chunk,
                chunk_sha256=plan.sha256,
                language=language,
                word_timestamps=word_timestamps,
                prompt=prompt,
            )
            self.cache.put(
                cache_key,
                provider=provider,
                model=model,
                source_hash=source_sha256,
                chunk_index=plan.chunk.index,
                offset=plan.chunk.offset,
                result=result,
            )
            checkpoint = self._checkpoint(
                plan,
                fingerprint=fingerprint,
                source_sha256=source_sha256,
                provider=provider,
                model=model,
                cache_key=cache_key,
                result_sha256=_result_sha256(result),
                attempt=record.attempt,
            )
            self.stage_store.save_checkpoint(
                job_type=ANALYSIS_JOB_TYPE,
                job_id=self.job_id,
                stage=STAGE_TRANSCRIBE,
                unit_key=plan.unit_key,
                owner=self.owner,
                checkpoint=checkpoint,
            )
            self.stage_store.complete(
                job_type=ANALYSIS_JOB_TYPE,
                job_id=self.job_id,
                stage=STAGE_TRANSCRIBE,
                unit_key=plan.unit_key,
                owner=self.owner,
                checkpoint=checkpoint,
            )
            raw_attempts = response.get("attempts")
            attempts = (
                tuple(item for item in raw_attempts if isinstance(item, dict))
                if isinstance(raw_attempts, list)
                else ()
            )
            return _ChunkOutcome(
                result,
                used_fallback=bool(response.get("used_fallback")),
                attempts=attempts,
            )
        except StageOwnershipError as exc:
            raise self._lease_lost_error(plan, exc) from exc
        except AIError as exc:
            self._record_failure(record, plan, exc)
            raise
        except Exception as exc:
            error = AIError(
                ErrorCategory.INTERNAL_ERROR,
                f"Unexpected transcription failure: {exc}",
                retryable=True,
                cause=exc,
                details={"chunk_index": plan.chunk.index},
            )
            self._record_failure(record, plan, error)
            raise error from exc
        finally:
            stop.set()
            thread.join(timeout=1.0)

    def _record_failure(
        self, record: StageRecord, plan: TranscriptChunkPlan, error: AIError
    ) -> None:
        retryable = stage_retryable(error)
        now = time.time()
        next_retry_at = now + error.retry_after if error.retry_after else None
        payload = {
            "category": error.category.value,
            "message": error.message,
            "retryable": retryable,
            "retry_after": error.retry_after,
            "details": {
                "chunk_index": plan.chunk.index,
                "chunk_offset": plan.chunk.offset,
                "chunk_sha256": plan.sha256,
                "provider": error.provider,
                "model": error.model,
                "attempt": record.attempt,
                "failed_at": now,
                "next_retry_at": next_retry_at,
            },
        }
        try:
            self.stage_store.fail(
                job_type=ANALYSIS_JOB_TYPE,
                job_id=self.job_id,
                stage=STAGE_TRANSCRIBE,
                unit_key=plan.unit_key,
                owner=self.owner,
                error=payload,
                retryable=retryable,
            )
        except StageOwnershipError as exc:
            raise self._lease_lost_error(plan, exc) from exc

    @staticmethod
    def _busy_error(plan: TranscriptChunkPlan, exc: Exception | None = None) -> AIError:
        return AIError(
            ErrorCategory.INTERNAL_ERROR,
            "This transcription chunk is already being processed by another worker.",
            retryable=True,
            details={"chunk_index": plan.chunk.index, "reason": "lease_held"},
            cause=exc,
        )

    @staticmethod
    def _lease_lost_error(plan: TranscriptChunkPlan, exc: StageOwnershipError) -> AIError:
        return AIError(
            ErrorCategory.INTERNAL_ERROR,
            "Transcription chunk ownership was lost before it could be committed.",
            retryable=True,
            details={"chunk_index": plan.chunk.index, "reason": "lease_lost"},
            cause=exc,
        )

    def _stored_error(self, record: StageRecord, plan: TranscriptChunkPlan) -> AIError:
        payload = record.error or {}
        try:
            category = ErrorCategory(str(payload.get("category") or "unknown_provider_error"))
        except ValueError:
            category = ErrorCategory.UNKNOWN_PROVIDER_ERROR
        details = payload.get("details")
        if not isinstance(details, dict):
            details = {}
        return AIError(
            category,
            str(payload.get("message") or "Transcription chunk failed permanently."),
            provider=str(details.get("provider") or "") or None,
            model=str(details.get("model") or "") or None,
            retryable=bool(payload.get("retryable")),
            retry_after=payload.get("retry_after")
            if isinstance(payload.get("retry_after"), (int, float))
            else None,
            details={**details, "chunk_index": plan.chunk.index},
        )

    def _retry_wait(self, record: StageRecord) -> float:
        payload = record.error or {}
        details = payload.get("details")
        if not isinstance(details, dict):
            return 0.0
        value = details.get("next_retry_at")
        if not isinstance(value, (int, float)):
            return 0.0
        return max(0.0, float(value) - time.time())

    def _retry_wait_error(
        self, record: StageRecord, plan: TranscriptChunkPlan, wait: float
    ) -> AIError:
        base = self._stored_error(record, plan)
        return AIError(
            base.category,
            f"Transcription chunk {plan.chunk.index} is waiting for its retry window "
            f"({math.ceil(wait)}s remaining).",
            provider=base.provider,
            model=base.model,
            retryable=True,
            retry_after=wait,
            details={**base.details, "reason": "retry_not_before"},
        )

    def _heartbeat_loop(self, plan: TranscriptChunkPlan, stop: threading.Event) -> None:
        while not stop.wait(self.heartbeat_seconds):
            try:
                if not self.stage_store.heartbeat(
                    job_type=ANALYSIS_JOB_TYPE,
                    job_id=self.job_id,
                    stage=STAGE_TRANSCRIBE,
                    unit_key=plan.unit_key,
                    owner=self.owner,
                    lease_seconds=self.lease_seconds,
                ):
                    return
            except Exception:
                return

    @staticmethod
    def _response(
        result: TranscriptResult,
        *,
        attempts: list[dict[str, Any]],
        used_fallback: bool,
    ) -> dict[str, Any]:
        return {
            "task": "speech_to_text",
            "provider": result.provider,
            "model": result.model,
            "used_fallback": used_fallback,
            "transcript": result.model_dump(mode="json"),
            "attempts": attempts,
        }
