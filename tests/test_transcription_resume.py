from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from clipping_ai.analysis.transcription import (
    DurableTranscriptionRunner,
    PreparedTranscriptionAudio,
    chunk_unit_key,
    transcription_config_fingerprint,
)
from clipping_ai.audio import hash_file
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.jobs import TranscriptionCache
from clipping_ai.recovery.stages import (
    StageOwnershipError,
    StageStatus,
    StageStore,
)
from clipping_ai.registry import ProviderConfig, RouteCandidate
from clipping_ai.schemas import TranscriptResult, TranscriptSegment
from clipping_ai.video.models import AudioChunkInfo
from tests.helpers import (
    FakeSpeechProvider,
    make_manager,
    make_registry,
    speech_model,
)

JOB_ID = "job-1"


class FakeChunkManager:
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[int] = []

    def can_transcribe_prepared_chunks(self) -> bool:
        return True

    def transcribe_chunk(self, chunk: AudioChunkInfo, **_: Any) -> dict[str, Any]:
        self.calls.append(chunk.index)
        if not self.responses:
            raise AssertionError(f"unexpected chunk call for {chunk.index}")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        result = (
            item if isinstance(item, TranscriptResult) else TranscriptResult.model_validate(item)
        )
        return {
            "transcript": result,
            "provider": result.provider or "fake",
            "model": result.model or "fake-model",
            "used_fallback": False,
            "attempts": [],
        }


class PreparedSpeechProvider(FakeSpeechProvider):
    @property
    def supports_prepared_chunks(self) -> bool:
        return True

    def transcribe_chunk(self, entry, chunk, **kwargs):
        self.calls.append(
            {
                "entry": entry,
                "chunk": chunk,
                **kwargs,
            }
        )
        if not self.responses:
            raise AssertionError(f"unexpected prepared chunk call to {self.provider_id}")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def transcribe(self, entry, audio_path, **kwargs):
        raise AssertionError("whole-file transcription should not run for prepared chunks")


def make_chunk(
    tmp_path: Path,
    index: int,
    *,
    offset: float,
    duration: float,
    content: bytes | None = None,
) -> AudioChunkInfo:
    payload = content if content is not None else f"chunk-{index}".encode()
    path = tmp_path / f"chunk-{index:06d}.flac"
    path.write_bytes(payload)
    return AudioChunkInfo(
        index=index,
        path=path,
        offset=offset,
        duration=duration,
        size_bytes=len(payload),
    )


def chunk_result(
    chunk: AudioChunkInfo,
    *,
    text: str = "hello",
    start: float | None = None,
    provider: str = "fake",
) -> TranscriptResult:
    segment_start = chunk.offset if start is None else start
    return TranscriptResult(
        text=text,
        language="en",
        duration=chunk.duration,
        segments=[
            TranscriptSegment(
                start=segment_start,
                end=segment_start + max(chunk.duration, 0.1),
                text=text,
            )
        ],
        provider=provider,
        model="fake-model",
    )


def prepared(chunks: list[AudioChunkInfo]) -> PreparedTranscriptionAudio:
    return PreparedTranscriptionAudio(
        source_sha256="source-sha",
        manifest_fingerprint="manifest-fp",
        chunks=tuple(chunks),
    )


def runner_for(
    tmp_path: Path,
    manager: FakeChunkManager,
    *,
    store: StageStore | None = None,
    cache: TranscriptionCache | None = None,
) -> DurableTranscriptionRunner:
    return DurableTranscriptionRunner(
        manager=manager,  # type: ignore[arg-type]
        stage_store=store or StageStore(tmp_path / "jobs.db"),
        cache=cache or TranscriptionCache(tmp_path / "jobs.db"),
        job_id=JOB_ID,
    )


def run(
    runner: DurableTranscriptionRunner,
    chunks: list[AudioChunkInfo],
    *,
    language: str | None = None,
    word_timestamps: bool = True,
    prompt: str | None = None,
    force_retry: bool = False,
):
    return runner.run(
        prepared(chunks),
        language=language,
        word_timestamps=word_timestamps,
        prompt=prompt,
        force_retry=force_retry,
    )


def stage(store: StageStore, index: int):
    return store.get(
        job_type="analysis",
        job_id=JOB_ID,
        stage="transcribe",
        unit_key=chunk_unit_key(index),
    )


def chunk_fingerprint(chunk: AudioChunkInfo, *, prompt: str | None = None) -> str:
    return transcription_config_fingerprint(
        source_sha256="source-sha",
        manifest_fingerprint="manifest-fp",
        chunk=chunk,
        chunk_sha256=hash_file(chunk.path),
        language=None,
        word_timestamps=True,
        prompt=prompt,
        requested_provider=None,
        requested_model=None,
    )


def test_completed_chunks_are_persisted_and_reused_without_provider_calls(tmp_path):
    chunks = [
        make_chunk(tmp_path, 0, offset=0.0, duration=10.0),
        make_chunk(tmp_path, 1, offset=10.0, duration=10.0),
    ]
    store = StageStore(tmp_path / "jobs.db")
    cache = TranscriptionCache(tmp_path / "jobs.db")
    first = FakeChunkManager([chunk_result(chunks[0]), chunk_result(chunks[1])])

    result = run(runner_for(tmp_path, first, store=store, cache=cache), chunks)

    assert first.calls == [0, 1]
    assert cache.count() == 2
    assert result["transcript"]["text"] == "hello hello"
    assert result["transcript"]["duration"] == 20.0
    assert [segment["start"] for segment in result["transcript"]["segments"]] == [0.0, 10.0]
    assert all(stage(store, index).status is StageStatus.DONE for index in (0, 1))

    second = FakeChunkManager([])
    resumed = run(runner_for(tmp_path, second, store=store, cache=cache), chunks)

    assert second.calls == []
    assert resumed == result


def test_resume_after_transient_failure_reuses_completed_chunk(tmp_path):
    chunks = [
        make_chunk(tmp_path, 0, offset=0.0, duration=10.0),
        make_chunk(tmp_path, 1, offset=10.0, duration=10.0),
    ]
    store = StageStore(tmp_path / "jobs.db")
    cache = TranscriptionCache(tmp_path / "jobs.db")
    first = FakeChunkManager(
        [
            chunk_result(chunks[0]),
            AIError(ErrorCategory.NETWORK_TIMEOUT, "timed out", retryable=True),
        ]
    )

    with pytest.raises(AIError) as exc:
        run(runner_for(tmp_path, first, store=store, cache=cache), chunks)

    assert exc.value.category is ErrorCategory.NETWORK_TIMEOUT
    assert cache.count() == 1
    assert stage(store, 0).status is StageStatus.DONE
    assert stage(store, 1).status is StageStatus.PENDING
    assert stage(store, 1).error is not None

    second = FakeChunkManager([chunk_result(chunks[1])])
    result = run(runner_for(tmp_path, second, store=store, cache=cache), chunks)

    assert second.calls == [1]
    assert result["transcript"]["text"] == "hello hello"


def test_non_retryable_failure_is_reported_without_repeating_provider_calls(tmp_path):
    chunks = [
        make_chunk(tmp_path, 0, offset=0.0, duration=10.0),
        make_chunk(tmp_path, 1, offset=10.0, duration=10.0),
    ]
    store = StageStore(tmp_path / "jobs.db")
    cache = TranscriptionCache(tmp_path / "jobs.db")
    first = FakeChunkManager(
        [
            chunk_result(chunks[0]),
            AIError(ErrorCategory.AUTHENTICATION_FAILED, "bad key", retryable=False),
        ]
    )

    with pytest.raises(AIError):
        run(runner_for(tmp_path, first, store=store, cache=cache), chunks)

    assert stage(store, 1).status is StageStatus.FAILED
    second = FakeChunkManager([])
    with pytest.raises(AIError) as exc:
        run(runner_for(tmp_path, second, store=store, cache=cache), chunks)

    assert exc.value.category is ErrorCategory.AUTHENTICATION_FAILED
    assert second.calls == []

    third = FakeChunkManager([chunk_result(chunks[0]), chunk_result(chunks[1])])
    result = run(
        runner_for(tmp_path, third, store=store, cache=cache),
        chunks,
        force_retry=True,
    )

    assert third.calls == [0, 1]
    assert result["transcript"]["text"] == "hello hello"


def test_corrupt_cached_result_is_rebuilt_from_provider(tmp_path):
    chunks = [
        make_chunk(tmp_path, 0, offset=0.0, duration=10.0),
        make_chunk(tmp_path, 1, offset=10.0, duration=10.0),
    ]
    store = StageStore(tmp_path / "jobs.db")
    cache = TranscriptionCache(tmp_path / "jobs.db")
    first = FakeChunkManager([chunk_result(chunks[0]), chunk_result(chunks[1])])
    run(runner_for(tmp_path, first, store=store, cache=cache), chunks)

    db = tmp_path / "jobs.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE transcription_chunks SET result_json = ? WHERE chunk_index = 0",
            (json.dumps({"text": "corrupt"}),),
        )

    second = FakeChunkManager([chunk_result(chunks[0], text="rebuilt")])
    result = run(runner_for(tmp_path, second, store=store, cache=cache), chunks)

    assert second.calls == [0]
    assert result["transcript"]["text"] == "rebuilt hello"
    assert cache.count() == 2


def test_live_lease_blocks_resume_but_expired_lease_is_reclaimed(tmp_path):
    chunks = [make_chunk(tmp_path, 0, offset=0.0, duration=10.0)]
    store = StageStore(tmp_path / "jobs.db")
    cache = TranscriptionCache(tmp_path / "jobs.db")
    fingerprint = chunk_fingerprint(chunks[0])
    store.ensure(
        job_type="analysis",
        job_id=JOB_ID,
        stage="transcribe",
        unit_key=chunk_unit_key(0),
        input_fingerprint=fingerprint,
    )
    store.claim_next(
        owner="other",
        lease_seconds=300,
        job_type="analysis",
        job_id=JOB_ID,
        stage="transcribe",
    )

    blocked = FakeChunkManager([])
    with pytest.raises(AIError) as exc:
        run(runner_for(tmp_path, blocked, store=store, cache=cache), chunks)

    assert exc.value.category is ErrorCategory.INTERNAL_ERROR
    assert exc.value.details["reason"] == "lease_held"
    assert blocked.calls == []

    expired = StageStore(tmp_path / "expired-jobs.db")
    expired.ensure(
        job_type="analysis",
        job_id=JOB_ID,
        stage="transcribe",
        unit_key=chunk_unit_key(0),
        input_fingerprint=fingerprint,
    )
    expired.claim_next(
        owner="dead",
        lease_seconds=-1,
        job_type="analysis",
        job_id=JOB_ID,
        stage="transcribe",
    )
    manager = FakeChunkManager([chunk_result(chunks[0])])
    result = run(
        runner_for(tmp_path, manager, store=expired, cache=cache),
        chunks,
    )

    assert manager.calls == [0]
    assert result["transcript"]["text"] == "hello"
    assert stage(expired, 0).status is StageStatus.DONE


def test_lost_ownership_cannot_mark_stage_done(tmp_path, monkeypatch):
    chunks = [make_chunk(tmp_path, 0, offset=0.0, duration=10.0)]
    store = StageStore(tmp_path / "jobs.db")
    cache = TranscriptionCache(tmp_path / "jobs.db")
    manager = FakeChunkManager([chunk_result(chunks[0])])
    runner = runner_for(tmp_path, manager, store=store, cache=cache)

    def lost(**_: Any):
        raise StageOwnershipError("lease lost")

    monkeypatch.setattr(store, "complete", lost)

    with pytest.raises(AIError) as exc:
        run(runner, chunks)

    assert exc.value.category is ErrorCategory.INTERNAL_ERROR
    assert stage(store, 0).status is not StageStatus.DONE


def test_malformed_timestamps_are_rejected_before_caching(tmp_path):
    chunks = [make_chunk(tmp_path, 0, offset=10.0, duration=10.0)]
    store = StageStore(tmp_path / "jobs.db")
    cache = TranscriptionCache(tmp_path / "jobs.db")
    manager = FakeChunkManager([chunk_result(chunks[0], start=0.0)])

    with pytest.raises(AIError) as exc:
        run(runner_for(tmp_path, manager, store=store, cache=cache), chunks)

    assert exc.value.category is ErrorCategory.INVALID_RESPONSE
    assert cache.count() == 0
    assert stage(store, 0).status is StageStatus.PENDING


def test_fingerprint_is_stable_and_scoped_to_each_chunk(tmp_path):
    chunks = [
        make_chunk(tmp_path, 0, offset=0.0, duration=10.0),
        make_chunk(tmp_path, 1, offset=10.0, duration=10.0),
    ]

    assert chunk_fingerprint(chunks[0]) == chunk_fingerprint(chunks[0])
    original_zero = chunk_fingerprint(chunks[0])
    original_one = chunk_fingerprint(chunks[1])
    changed_one = chunks[1].model_copy(update={"duration": 11.0})

    assert chunk_fingerprint(changed_one) != original_one
    assert chunk_fingerprint(chunks[0]) == original_zero
    assert chunk_fingerprint(chunks[0], prompt="different") != original_zero


def test_manager_routes_prepared_chunks_and_falls_back_on_rate_limit(tmp_path):
    provider_config = ProviderConfig(
        type="groq_speech",
        api_key_env=None,
        max_retries=0,
        requests_per_minute=0,
        min_interval_seconds=0,
    )
    registry = make_registry(
        providers={"p1": provider_config, "p2": provider_config},
        models=[
            speech_model("whisper-1", provider="p1"),
            speech_model("whisper-2", provider="p2"),
        ],
        routing={
            "speech_to_text": [
                RouteCandidate(provider="p1", model="whisper-1"),
                RouteCandidate(provider="p2", model="whisper-2"),
            ]
        },
    )
    first = PreparedSpeechProvider(
        "p1",
        [AIError(ErrorCategory.RATE_LIMIT_EXCEEDED, "rate limited", retryable=True)],
    )
    second = PreparedSpeechProvider(
        "p2",
        [chunk_result(make_chunk(tmp_path, 0, offset=0.0, duration=1.0))],
    )
    manager = make_manager(registry, {"p1": first, "p2": second}, tmp_path)
    chunk = make_chunk(tmp_path, 0, offset=0.0, duration=1.0)

    assert manager.can_transcribe_prepared_chunks() is True
    result = manager.transcribe_chunk(
        chunk,
        language="en",
        word_timestamps=True,
        prompt=None,
    )

    assert result["provider"] == "p2"
    assert result["used_fallback"] is True
    assert result["transcript"]["text"] == "hello"
    assert len(first.calls) == 1
    assert len(second.calls) == 1


def test_manager_rejects_provider_without_prepared_chunk_support(tmp_path):
    provider_config = ProviderConfig(
        type="groq_speech",
        api_key_env=None,
        max_retries=0,
        requests_per_minute=0,
        min_interval_seconds=0,
    )
    registry = make_registry(
        providers={"p1": provider_config},
        models=[speech_model("whisper-1", provider="p1")],
        routing={"speech_to_text": [RouteCandidate(provider="p1", model="whisper-1")]},
    )
    provider = FakeSpeechProvider("p1", [])
    manager = make_manager(registry, {"p1": provider}, tmp_path)

    assert manager.can_transcribe_prepared_chunks() is False
    with pytest.raises(AIError) as exc:
        manager.transcribe_chunk(
            make_chunk(tmp_path, 0, offset=0.0, duration=1.0),
            provider="p1",
            model="whisper-1",
        )

    assert exc.value.category is ErrorCategory.UNSUPPORTED_CAPABILITY
    assert provider.calls == []


def test_stale_chunk_unit_is_not_claimed_by_current_manifest(tmp_path):
    chunks = [make_chunk(tmp_path, 0, offset=0.0, duration=10.0)]
    store = StageStore(tmp_path / "jobs.db")
    cache = TranscriptionCache(tmp_path / "jobs.db")
    store.ensure(
        job_type="analysis",
        job_id=JOB_ID,
        stage="transcribe",
        unit_key=chunk_unit_key(5),
        input_fingerprint="stale-fingerprint",
    )
    manager = FakeChunkManager([chunk_result(chunks[0])])

    result = run(runner_for(tmp_path, manager, store=store, cache=cache), chunks)

    assert manager.calls == [0]
    assert result["transcript"]["text"] == "hello"
    assert stage(store, 5).status is StageStatus.PENDING


def test_changed_chunk_hash_invalidates_completed_stage(tmp_path):
    chunk = make_chunk(tmp_path, 0, offset=0.0, duration=10.0, content=b"one")
    store = StageStore(tmp_path / "jobs.db")
    cache = TranscriptionCache(tmp_path / "jobs.db")
    first = FakeChunkManager([chunk_result(chunk, text="one")])
    run(runner_for(tmp_path, first, store=store, cache=cache), [chunk])

    chunk.path.write_bytes(b"two")
    second = FakeChunkManager([chunk_result(chunk, text="two")])
    result = run(runner_for(tmp_path, second, store=store, cache=cache), [chunk])

    assert first.calls == [0]
    assert second.calls == [0]
    assert result["transcript"]["text"] == "two"
    assert cache.count() == 2


def test_explicit_whole_file_provider_disables_prepared_chunk_path(tmp_path):
    provider_config = ProviderConfig(
        type="groq_speech",
        api_key_env=None,
        max_retries=0,
        requests_per_minute=0,
        min_interval_seconds=0,
    )
    registry = make_registry(
        providers={"p1": provider_config, "p2": provider_config},
        models=[
            speech_model("whole-file", provider="p1"),
            speech_model("prepared", provider="p2"),
        ],
        routing={
            "speech_to_text": [
                RouteCandidate(provider="p1", model="whole-file"),
                RouteCandidate(provider="p2", model="prepared"),
            ]
        },
    )
    whole_file = FakeSpeechProvider("p1", [])
    prepared_provider = PreparedSpeechProvider("p2", [])
    manager = make_manager(registry, {"p1": whole_file, "p2": prepared_provider}, tmp_path)

    assert manager.can_transcribe_prepared_chunks() is True
    assert manager.can_transcribe_prepared_chunks(provider="p1", model="whole-file") is False
    assert manager.can_transcribe_prepared_chunks(provider="p2", model="prepared") is True


def test_force_retry_retranscribes_completed_chunks(tmp_path):
    chunks = [
        make_chunk(tmp_path, 0, offset=0.0, duration=10.0),
        make_chunk(tmp_path, 1, offset=10.0, duration=10.0),
    ]
    store = StageStore(tmp_path / "jobs.db")
    cache = TranscriptionCache(tmp_path / "jobs.db")
    first = FakeChunkManager([chunk_result(chunks[0]), chunk_result(chunks[1])])
    run(runner_for(tmp_path, first, store=store, cache=cache), chunks)

    second = FakeChunkManager(
        [
            chunk_result(chunks[0], text="fresh zero"),
            chunk_result(chunks[1], text="fresh one"),
        ]
    )
    result = run(
        runner_for(tmp_path, second, store=store, cache=cache),
        chunks,
        force_retry=True,
    )

    assert second.calls == [0, 1]
    assert result["transcript"]["text"] == "fresh zero fresh one"
    assert cache.count() == 2


def test_changed_fingerprint_cannot_steal_live_lease(tmp_path):
    chunks = [make_chunk(tmp_path, 0, offset=0.0, duration=10.0)]
    store = StageStore(tmp_path / "jobs.db")
    cache = TranscriptionCache(tmp_path / "jobs.db")
    store.ensure(
        job_type="analysis",
        job_id=JOB_ID,
        stage="transcribe",
        unit_key=chunk_unit_key(0),
        input_fingerprint="stale",
    )
    store.claim_next(
        owner="other",
        lease_seconds=300,
        job_type="analysis",
        job_id=JOB_ID,
        stage="transcribe",
    )
    manager = FakeChunkManager([])

    with pytest.raises(AIError) as exc:
        run(runner_for(tmp_path, manager, store=store, cache=cache), chunks)

    assert exc.value.details["reason"] == "lease_held"
    assert manager.calls == []
    assert stage(store, 0).input_fingerprint == "stale"


def test_prepared_capability_requires_available_provider(tmp_path):
    provider_config = ProviderConfig(
        type="groq_speech",
        api_key_env=None,
        max_retries=0,
        requests_per_minute=0,
        min_interval_seconds=0,
    )
    registry = make_registry(
        providers={"p1": provider_config},
        models=[speech_model("whisper-1", provider="p1")],
        routing={"speech_to_text": [RouteCandidate(provider="p1", model="whisper-1")]},
    )
    provider = PreparedSpeechProvider("p1", [], available=False, reason="missing key")
    manager = make_manager(registry, {"p1": provider}, tmp_path)

    assert manager.can_transcribe_prepared_chunks() is False


def test_invalid_transcript_payload_is_retryable_and_not_cached(tmp_path):
    chunks = [make_chunk(tmp_path, 0, offset=0.0, duration=10.0)]
    store = StageStore(tmp_path / "jobs.db")
    cache = TranscriptionCache(tmp_path / "jobs.db")

    class RawChunkManager(FakeChunkManager):
        def transcribe_chunk(self, chunk: AudioChunkInfo, **_: Any) -> dict[str, Any]:
            self.calls.append(chunk.index)
            return {
                "transcript": {"segments": [{"start": "bad"}]},
                "provider": "fake",
                "model": "fake-model",
            }

    with pytest.raises(AIError) as exc:
        run(
            runner_for(tmp_path, RawChunkManager(), store=store, cache=cache),
            chunks,
        )

    assert exc.value.category is ErrorCategory.INVALID_RESPONSE
    assert exc.value.retryable is True
    assert cache.count() == 0
    assert stage(store, 0).status is StageStatus.PENDING


def test_prepared_capability_ignores_cooling_down_provider(tmp_path):
    provider_config = ProviderConfig(
        type="groq_speech",
        api_key_env=None,
        max_retries=0,
        requests_per_minute=0,
        min_interval_seconds=0,
    )
    registry = make_registry(
        providers={"p1": provider_config},
        models=[speech_model("whisper-1", provider="p1")],
        routing={"speech_to_text": [RouteCandidate(provider="p1", model="whisper-1")]},
    )
    provider = PreparedSpeechProvider("p1", [])
    manager = make_manager(registry, {"p1": provider}, tmp_path)
    manager._block_provider_quota(
        "p1",
        AIError(ErrorCategory.FREE_QUOTA_EXHAUSTED, "quota", retryable=False),
    )

    assert manager.can_transcribe_prepared_chunks() is False
