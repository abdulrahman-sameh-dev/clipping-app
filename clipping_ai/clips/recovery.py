from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from clipping_ai.audio import hash_file
from clipping_ai.clips.captions import build_ass_file
from clipping_ai.clips.config import ClipConfig
from clipping_ai.clips.editing import EditPlan
from clipping_ai.clips.models import ClipJob
from clipping_ai.clips.renderer import MediaInfo, RenderResult, probe_media, render_clip
from clipping_ai.composition.models import CompositionPlan, VisualSettings
from clipping_ai.composition.overlays import write_overlay_ass
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.highlights.models import Highlight
from clipping_ai.recovery.idempotency import stable_fingerprint
from clipping_ai.recovery.stages import (
    StageOwnershipError,
    StageRecord,
    StageStatus,
    StageStore,
)
from clipping_ai.video.models import VideoJob

CLIP_JOB_TYPE = "clip"
STAGE_PREPARE = "render.prepare"
STAGE_CAPTIONS = "render.captions"
STAGE_OUTPUT = "render.output"
STAGE_VALIDATE = "render.validate"
STAGE_FINALIZE = "render.finalize"
CLIP_STAGES = (
    STAGE_PREPARE,
    STAGE_CAPTIONS,
    STAGE_OUTPUT,
    STAGE_VALIDATE,
    STAGE_FINALIZE,
)

_ARTIFACT_VERSION = 2
_LEASE_SECONDS = 1800.0
_HEARTBEAT_SECONDS = 60.0
_DURATION_TOLERANCE = 0.75

M = TypeVar("M", bound=BaseModel)


class PrepareArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: int
    job_id: str
    source_job_id: str
    source_sha256: str
    source_size: int
    start_time: float
    end_time: float
    duration: float
    format: str
    width: int
    height: int
    fps: float
    quality: str
    fit: str
    source_info: dict[str, Any]
    edit_plan: dict[str, Any]
    composition: dict[str, Any]
    visual_settings: dict[str, Any]
    render_options: dict[str, Any]
    effects_options: dict[str, Any]
    captions_enabled: bool


class CaptionsArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: int
    job_id: str
    captions_enabled: bool
    language: str | None
    caption_path: Path | None = None
    caption_sha256: str | None = None
    callout_path: Path | None = None
    callout_sha256: str | None = None
    caption_options: dict[str, Any] = Field(default_factory=dict)


class OutputArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: int
    job_id: str
    path: Path
    sha256: str
    size_bytes: int
    rendered_duration: float
    staging_path: Path | None = None


class ValidateArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: int
    job_id: str
    path: Path
    sha256: str
    size_bytes: int
    duration: float
    has_video: bool
    has_audio: bool
    video_codec: str | None = None
    audio_codec: str | None = None


class FinalizeArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: int
    job_id: str
    output_path: Path
    sha256: str
    size_bytes: int
    caption_path: Path | None = None
    status: str = "ready"


@dataclass(frozen=True)
class ClipStageRequest:
    job: ClipJob
    source_job: VideoJob
    source_path: Path
    source_info: MediaInfo
    config: ClipConfig
    visual: VisualSettings
    edit_plan: EditPlan
    composition: CompositionPlan
    highlight: Highlight | None
    signals: dict[str, Any]


@dataclass(frozen=True)
class ClipStageResult:
    output_path: Path
    size_bytes: int
    output_sha256: str
    caption_path: Path | None
    caption_language: str | None = None
    render: RenderResult | None = None


@dataclass
class DurableClipRunner:
    stage_store: StageStore
    request: ClipStageRequest
    artifact_dir: Path
    owner: str = field(default_factory=lambda: f"clip-renderer-{uuid4().hex}")
    lease_seconds: float = _LEASE_SECONDS
    heartbeat_seconds: float = _HEARTBEAT_SECONDS
    cancel_check: Callable[[], bool] = field(default=lambda: False)
    on_stage: Callable[[str], None] | None = None

    def __post_init__(self) -> None:
        self.artifact_dir = Path(self.artifact_dir).resolve()
        self.artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def run(self) -> ClipStageResult:
        fingerprints = clip_fingerprints(self.request)
        prepare = self._run_prepare(fingerprints)
        captions = self._run_captions(fingerprints, prepare)
        output = self._run_output(fingerprints, captions)
        self._run_validate(fingerprints, output)
        return self._run_finalize(fingerprints, output, captions)

    def reset_failed_stages(self) -> None:
        for stage in CLIP_STAGES:
            record = self._get(stage)
            if record is None or record.status is not StageStatus.FAILED:
                continue
            self._reset(stage, record.input_fingerprint or "pending", clear_checkpoint=True)

    def _run_prepare(self, fingerprints: dict[str, str]) -> PrepareArtifact:
        self._notify(STAGE_PREPARE)
        request = self.request
        return self._run_json_stage(
            STAGE_PREPARE,
            fingerprints,
            PrepareArtifact,
            input_hashes={
                "source_sha256": _file_hash(request.source_path),
                "job_sha256": _hash_value(request.job.model_dump(mode="json")),
                "source_job_sha256": _hash_value(request.source_job.model_dump(mode="json")),
                "source_info_sha256": _hash_value(request.source_info),
                "edit_plan_sha256": _hash_value(asdict(request.edit_plan)),
                "composition_sha256": _hash_value(request.composition.to_dict()),
                "config_sha256": _hash_value(_render_options(request)),
            },
            payload=lambda: _prepare_payload(request),
            heartbeat=False,
        )

    def _run_captions(
        self, fingerprints: dict[str, str], prepare: PrepareArtifact
    ) -> CaptionsArtifact:
        self._notify(STAGE_CAPTIONS)
        request = self.request
        enabled = bool(request.job.captions and request.visual.captions)
        caption_path = self.artifact_dir / "captions.ass" if enabled else None
        callout_path = self.artifact_dir / "callouts.ass" if request.composition.callouts else None
        cached = self._cached_model(
            STAGE_CAPTIONS,
            fingerprints[STAGE_CAPTIONS],
            CaptionsArtifact,
        )
        if cached is not None and self._captions_files_valid(cached):
            return cached
        self._invalidate_from(STAGE_CAPTIONS, fingerprints)
        claimed = self._prepare_and_claim(STAGE_CAPTIONS, fingerprints[STAGE_CAPTIONS])
        input_hashes = {
            "prepare": fingerprints[STAGE_PREPARE],
            "prepare_version": prepare.version,
            "transcript_sha256": _hash_value(request.source_job.transcript),
            "highlight_sha256": _hash_value(request.highlight),
            "signals_sha256": _hash_value(request.signals),
            "caption_options_sha256": _hash_value(_caption_options(request)),
            "callouts_sha256": _hash_value(
                [callout.model_dump(mode="json") for callout in request.composition.callouts]
            ),
        }
        try:
            if enabled:
                if request.source_job.transcript is None:
                    raise AIError(
                        ErrorCategory.INVALID_REQUEST,
                        "Captions were requested but the source transcript is missing.",
                        retryable=False,
                    )
                _, language = build_ass_file(
                    request.source_job.transcript,
                    start_time=request.job.start_time,
                    end_time=request.job.end_time,
                    output_path=caption_path or self.artifact_dir / "captions.ass",
                    width=request.job.width,
                    height=request.job.height,
                    config=request.config,
                    language=request.job.caption_language,
                    highlight=request.highlight,
                    signals=request.signals,
                )
            else:
                language = None
            overlay_path = write_overlay_ass(
                request.composition.callouts,
                output_path=callout_path or self.artifact_dir / "callouts.ass",
                width=request.job.width,
                height=request.job.height,
                config=request.config,
            )
            artifact = CaptionsArtifact(
                version=_ARTIFACT_VERSION,
                job_id=request.job.id,
                captions_enabled=enabled,
                language=language,
                caption_path=caption_path if caption_path and caption_path.is_file() else None,
                caption_sha256=_optional_hash(caption_path),
                callout_path=overlay_path,
                callout_sha256=_optional_hash(overlay_path),
                caption_options=_caption_options(request),
            )
            self._complete_json_stage(
                STAGE_CAPTIONS,
                claimed,
                fingerprints[STAGE_CAPTIONS],
                artifact,
                input_hashes,
            )
            return artifact
        except StageOwnershipError as exc:
            raise self._lease_lost_error(STAGE_CAPTIONS, exc) from exc
        except AIError as exc:
            self._record_failure(STAGE_CAPTIONS, claimed, fingerprints[STAGE_CAPTIONS], exc, input_hashes)
            raise
        except Exception as exc:
            error = AIError(
                ErrorCategory.INTERNAL_ERROR,
                f"Unexpected clip failure in '{STAGE_CAPTIONS}': {exc}",
                retryable=True,
                cause=exc,
            )
            self._record_failure(STAGE_CAPTIONS, claimed, fingerprints[STAGE_CAPTIONS], error, input_hashes)
            raise error from exc

    def _captions_files_valid(self, artifact: CaptionsArtifact) -> bool:
        for path, expected in (
            (artifact.caption_path, artifact.caption_sha256),
            (artifact.callout_path, artifact.callout_sha256),
        ):
            if path is None or expected is None:
                continue
            if not path.is_file() or _file_hash(path) != expected:
                return False
        return True

    def _run_output(
        self, fingerprints: dict[str, str], captions: CaptionsArtifact
    ) -> OutputArtifact:
        self._notify(STAGE_OUTPUT)
        request = self.request
        fingerprint = fingerprints[STAGE_OUTPUT]
        final_path = final_output_path(self.artifact_dir, request.job)
        staging_path = output_staging_path(self.artifact_dir, request.job)
        cached = self._cached_model(STAGE_OUTPUT, fingerprint, OutputArtifact)
        if cached is not None and self._output_file_valid(cached):
            return cached
        record = self._get(STAGE_OUTPUT)
        checkpoint = record.checkpoint if record is not None and isinstance(record.checkpoint, dict) else {}
        if record is not None and record.status == StageStatus.DONE:
            self._invalidate_from(STAGE_OUTPUT, fingerprints)
        claimed = self._prepare_and_claim(STAGE_OUTPUT, fingerprint)
        input_hashes = {
            "prepare": fingerprints[STAGE_PREPARE],
            "captions": fingerprints[STAGE_CAPTIONS],
            "render_options_sha256": _hash_value(_render_options(request)),
            "final_path": final_path.name,
        }
        try:
            expected_sha = str(checkpoint.get("output_sha256") or "")
            if expected_sha and final_path.is_file() and _file_hash(final_path) == expected_sha:
                artifact = OutputArtifact(
                    version=_ARTIFACT_VERSION,
                    job_id=request.job.id,
                    path=final_path,
                    sha256=expected_sha,
                    size_bytes=final_path.stat().st_size,
                    rendered_duration=request.job.duration,
                )
                self._complete_json_stage(
                    STAGE_OUTPUT,
                    claimed,
                    fingerprint,
                    artifact,
                    {**input_hashes, "recovered_from": "final"},
                    artifact_path=final_path,
                )
                return artifact
            staged_sha = str(checkpoint.get("staged_sha256") or "")
            if staged_sha and staging_path.is_file() and _file_hash(staging_path) == staged_sha:
                self._ensure_stage_alive(STAGE_OUTPUT)
                os.replace(staging_path, final_path)
                artifact = OutputArtifact(
                    version=_ARTIFACT_VERSION,
                    job_id=request.job.id,
                    path=final_path,
                    sha256=staged_sha,
                    size_bytes=final_path.stat().st_size,
                    rendered_duration=request.job.duration,
                )
                self._complete_json_stage(
                    STAGE_OUTPUT,
                    claimed,
                    fingerprint,
                    artifact,
                    {**input_hashes, "recovered_from": "staged"},
                    artifact_path=final_path,
                )
                return artifact

            with contextlib.suppress(OSError):
                staging_path.unlink(missing_ok=True)
            render_job = request.job.model_copy(
                update={
                    "captions": bool(request.job.captions and request.visual.captions),
                    "caption_path": (
                        captions.caption_path if request.visual.captions else None
                    ),
                }
            )
            with self._heartbeat(STAGE_OUTPUT):
                result = render_clip(
                    render_job,
                    source_path=request.source_path,
                    output_path=staging_path,
                    config=request.config,
                    cancel_check=self._render_cancel_check,
                    source_info=request.source_info,
                    edit_plan=request.edit_plan,
                    overlay_path=self.artifact_dir / "callouts.ass"
                    if (self.artifact_dir / "callouts.ass").is_file()
                    else None,
                )
                staged_path = Path(result.path)
                if not staged_path.is_file():
                    raise AIError(
                        ErrorCategory.INTERNAL_ERROR,
                        "Clip renderer did not produce a staged output file.",
                        retryable=True,
                    )
                staged_sha = _file_hash(staged_path)
                try:
                    self.stage_store.save_checkpoint(
                        job_type=CLIP_JOB_TYPE,
                        job_id=request.job.id,
                        stage=STAGE_OUTPUT,
                        owner=self.owner,
                        checkpoint={
                            "version": _ARTIFACT_VERSION,
                            "job_id": request.job.id,
                            "stage": STAGE_OUTPUT,
                            "fingerprint": fingerprint,
                            "staged_sha256": staged_sha,
                            "output_sha256": staged_sha,
                            "staging_path": str(staged_path),
                            "output_path": str(final_path),
                            "size_bytes": staged_path.stat().st_size,
                        },
                    )
                except StageOwnershipError as exc:
                    raise self._lease_lost_error(STAGE_OUTPUT, exc) from exc
                self._ensure_stage_alive(STAGE_OUTPUT)
                os.replace(staged_path, final_path)
                artifact = OutputArtifact(
                    version=_ARTIFACT_VERSION,
                    job_id=request.job.id,
                    path=final_path,
                    sha256=staged_sha,
                    size_bytes=final_path.stat().st_size,
                    rendered_duration=result.duration,
                    staging_path=staging_path,
                )
                self._complete_json_stage(
                    STAGE_OUTPUT,
                    claimed,
                    fingerprint,
                    artifact,
                    input_hashes,
                    artifact_path=final_path,
                )
                self.last_render = result
                return artifact
        except StageOwnershipError as exc:
            raise self._lease_lost_error(STAGE_OUTPUT, exc) from exc
        except AIError as exc:
            self.stage_store.release(
                job_type=CLIP_JOB_TYPE,
                job_id=request.job.id,
                stage=STAGE_OUTPUT,
                owner=self.owner,
            ) if exc.details.get("reason") == "cancelled" else None
            self._record_failure(STAGE_OUTPUT, claimed, fingerprint, exc, input_hashes)
            raise
        except Exception as exc:
            error = AIError(
                ErrorCategory.INTERNAL_ERROR,
                f"Unexpected clip failure in '{STAGE_OUTPUT}': {exc}",
                retryable=True,
                cause=exc,
            )
            self._record_failure(STAGE_OUTPUT, claimed, fingerprint, error, input_hashes)
            raise error from exc

    def _output_file_valid(self, artifact: OutputArtifact) -> bool:
        path = Path(artifact.path)
        if not path.is_file() or _file_hash(path) != artifact.sha256:
            return False
        return path.stat().st_size == artifact.size_bytes

    def _run_validate(self, fingerprints: dict[str, str], output: OutputArtifact) -> None:
        self._notify(STAGE_VALIDATE)
        request = self.request
        fingerprint = fingerprints[STAGE_VALIDATE]
        cached = self._cached_model(STAGE_VALIDATE, fingerprint, ValidateArtifact)
        if cached is not None and self._validate_file_valid(cached):
            return
        self._invalidate_from(STAGE_VALIDATE, fingerprints)
        claimed = self._prepare_and_claim(STAGE_VALIDATE, fingerprint)
        input_hashes = {
            "output": fingerprints[STAGE_OUTPUT],
            "output_sha256": output.sha256,
        }
        try:
            info = probe_media(Path(output.path), request.config)
            if not info.has_video:
                raise AIError(
                    ErrorCategory.INTERNAL_ERROR,
                    "Rendered clip has no video stream.",
                    retryable=True,
                )
            if request.source_info.has_audio and not info.has_audio:
                raise AIError(
                    ErrorCategory.INTERNAL_ERROR,
                    "Rendered clip lost the source audio stream.",
                    retryable=True,
                )
            if info.duration + _DURATION_TOLERANCE < request.job.duration:
                raise AIError(
                    ErrorCategory.INTERNAL_ERROR,
                    f"Rendered clip is too short ({info.duration:.2f}s of "
                    f"{request.job.duration:.2f}s).",
                    retryable=True,
                )
            path = Path(output.path)
            artifact = ValidateArtifact(
                version=_ARTIFACT_VERSION,
                job_id=request.job.id,
                path=path,
                sha256=_file_hash(path),
                size_bytes=path.stat().st_size,
                duration=info.duration,
                has_video=info.has_video,
                has_audio=info.has_audio,
                video_codec=info.video_codec,
                audio_codec=info.audio_codec,
            )
            self._complete_json_stage(
                STAGE_VALIDATE,
                claimed,
                fingerprint,
                artifact,
                input_hashes,
                artifact_path=path,
            )
        except StageOwnershipError as exc:
            raise self._lease_lost_error(STAGE_VALIDATE, exc) from exc
        except AIError as exc:
            self._record_failure(STAGE_VALIDATE, claimed, fingerprint, exc, input_hashes)
            raise
        except Exception as exc:
            error = AIError(
                ErrorCategory.INTERNAL_ERROR,
                f"Unexpected clip failure in '{STAGE_VALIDATE}': {exc}",
                retryable=True,
                cause=exc,
            )
            self._record_failure(STAGE_VALIDATE, claimed, fingerprint, error, input_hashes)
            raise error from exc

    def _validate_file_valid(self, artifact: ValidateArtifact) -> bool:
        path = Path(artifact.path)
        return (
            path.is_file()
            and _file_hash(path) == artifact.sha256
            and path.stat().st_size == artifact.size_bytes
        )

    def _run_finalize(
        self,
        fingerprints: dict[str, str],
        output: OutputArtifact,
        captions: CaptionsArtifact,
    ) -> ClipStageResult:
        self._notify(STAGE_FINALIZE)
        request = self.request
        fingerprint = fingerprints[STAGE_FINALIZE]
        cached = self._cached_model(STAGE_FINALIZE, fingerprint, FinalizeArtifact)
        render = getattr(self, "last_render", None)
        if cached is not None:
            return ClipStageResult(
                output_path=Path(cached.output_path),
                size_bytes=cached.size_bytes,
                output_sha256=cached.sha256,
                caption_path=cached.caption_path or captions.caption_path,
                caption_language=captions.language,
                render=render,
            )
        self._invalidate_from(STAGE_FINALIZE, fingerprints)
        claimed = self._prepare_and_claim(STAGE_FINALIZE, fingerprint)
        input_hashes = {
            "validate": fingerprints[STAGE_VALIDATE],
            "output_sha256": output.sha256,
        }
        artifact = FinalizeArtifact(
            version=_ARTIFACT_VERSION,
            job_id=request.job.id,
            output_path=Path(output.path),
            sha256=output.sha256,
            size_bytes=output.size_bytes,
            caption_path=captions.caption_path,
        )
        try:
            self._complete_json_stage(
                STAGE_FINALIZE,
                claimed,
                fingerprint,
                artifact,
                input_hashes,
            )
        except StageOwnershipError as exc:
            raise self._lease_lost_error(STAGE_FINALIZE, exc) from exc
        except AIError as exc:
            self._record_failure(STAGE_FINALIZE, claimed, fingerprint, exc, input_hashes)
            raise
        except Exception as exc:
            error = AIError(
                ErrorCategory.INTERNAL_ERROR,
                f"Unexpected clip failure in '{STAGE_FINALIZE}': {exc}",
                retryable=True,
                cause=exc,
            )
            self._record_failure(STAGE_FINALIZE, claimed, fingerprint, error, input_hashes)
            raise error from exc
        render = getattr(self, "last_render", None)
        return ClipStageResult(
            output_path=Path(output.path),
            size_bytes=output.size_bytes,
            output_sha256=output.sha256,
            caption_path=captions.caption_path,
            caption_language=captions.language,
            render=render,
        )

    def _run_json_stage(
        self,
        stage: str,
        fingerprints: dict[str, str],
        model_type: type[M],
        *,
        payload: Callable[[], Any],
        input_hashes: dict[str, Any],
        heartbeat: bool,
    ) -> M:
        fingerprint = fingerprints[stage]
        cached = self._cached_model(stage, fingerprint, model_type)
        if cached is not None:
            return cached
        self._invalidate_from(stage, fingerprints)
        claimed = self._prepare_and_claim(stage, fingerprint)
        try:
            with self._heartbeat(stage) if heartbeat else _null_context():
                value = payload()
                artifact = model_type.model_validate(value)
                self._complete_json_stage(stage, claimed, fingerprint, artifact, input_hashes)
                return artifact
        except StageOwnershipError as exc:
            raise self._lease_lost_error(stage, exc) from exc
        except AIError as exc:
            self._record_failure(stage, claimed, fingerprint, exc, input_hashes)
            raise
        except Exception as exc:
            error = AIError(
                ErrorCategory.INTERNAL_ERROR,
                f"Unexpected clip failure in '{stage}': {exc}",
                retryable=True,
                cause=exc,
            )
            self._record_failure(stage, claimed, fingerprint, error, input_hashes)
            raise error from exc

    def _cached_model(
        self, stage: str, fingerprint: str, model_type: type[M]
    ) -> M | None:
        record = self._get(stage)
        if record is None or record.status is not StageStatus.DONE:
            return None
        if record.input_fingerprint != fingerprint:
            return None
        envelope_path = self._artifact_path(stage)
        checkpoint = record.checkpoint
        if not isinstance(checkpoint, dict) or checkpoint.get("fingerprint") != fingerprint:
            return None
        if not envelope_path.is_file():
            return None
        envelope_sha256 = checkpoint.get("envelope_sha256")
        if not isinstance(envelope_sha256, str):
            return None
        if _file_hash(envelope_path) != envelope_sha256:
            return None
        if record.artifact_path is not None and record.artifact_sha256 is not None:
            artifact_path = Path(record.artifact_path)
            if artifact_path.is_file() and _file_hash(artifact_path) != record.artifact_sha256:
                return None
        envelope = self._read_envelope(stage, envelope_path, fingerprint)
        if envelope is None:
            return None
        payload = envelope["payload"]
        if checkpoint.get("payload_sha256") != envelope["payload_sha256"]:
            return None
        if checkpoint.get("input_hashes") != envelope["input_hashes"]:
            return None
        try:
            return model_type.model_validate(payload)
        except Exception:
            return None

    def _read_envelope(self, stage: str, path: Path, fingerprint: str) -> dict[str, Any] | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        if raw.get("version") != _ARTIFACT_VERSION:
            return None
        if raw.get("job_id") != self.request.job.id or raw.get("stage") != stage:
            return None
        if raw.get("fingerprint") != fingerprint:
            return None
        payload = raw.get("payload")
        expected = raw.get("payload_sha256")
        input_hashes = raw.get("input_hashes")
        if not isinstance(expected, str) or expected != _hash_value(payload):
            return None
        if not isinstance(input_hashes, dict):
            return None
        return {
            "payload": payload,
            "payload_sha256": expected,
            "input_hashes": input_hashes,
        }

    def _complete_json_stage(
        self,
        stage: str,
        claimed: StageRecord,
        fingerprint: str,
        artifact: BaseModel,
        input_hashes: dict[str, Any],
        *,
        artifact_path: Path | None = None,
    ) -> None:
        path = artifact_path or self._artifact_path(stage)
        payload = json.loads(artifact.model_dump_json())
        envelope = {
            "version": _ARTIFACT_VERSION,
            "job_id": self.request.job.id,
            "stage": stage,
            "fingerprint": fingerprint,
            "payload": payload,
            "payload_sha256": _hash_value(payload),
            "input_hashes": input_hashes,
            "created_at": datetime.now(UTC).isoformat(),
        }
        envelope_path = self._artifact_path(stage)
        self._write_json_atomic(envelope_path, envelope)
        checkpoint = {
            "version": _ARTIFACT_VERSION,
            "job_id": self.request.job.id,
            "stage": stage,
            "fingerprint": fingerprint,
            "payload_sha256": envelope["payload_sha256"],
            "envelope_sha256": _file_hash(envelope_path),
            "input_hashes": input_hashes,
            "attempt": claimed.attempt,
        }
        self.stage_store.complete(
            job_type=CLIP_JOB_TYPE,
            job_id=self.request.job.id,
            stage=stage,
            owner=self.owner,
            checkpoint=checkpoint,
            artifact_path=path,
            require_artifact=True,
        )

    def _write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = (_canonical_json(payload) + "\n").encode("utf-8")
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.part")
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _artifact_path(self, stage: str) -> Path:
        return self.artifact_dir / f"{stage}.json"

    def _get(self, stage: str) -> StageRecord | None:
        return self.stage_store.get(
            job_type=CLIP_JOB_TYPE,
            job_id=self.request.job.id,
            stage=stage,
        )

    def _ensure(self, stage: str, fingerprint: str) -> StageRecord:
        return self.stage_store.ensure(
            job_type=CLIP_JOB_TYPE,
            job_id=self.request.job.id,
            stage=stage,
            input_fingerprint=fingerprint,
        )

    def _reset(self, stage: str, fingerprint: str, *, clear_checkpoint: bool) -> StageRecord:
        try:
            return self.stage_store.reset(
                job_type=CLIP_JOB_TYPE,
                job_id=self.request.job.id,
                stage=stage,
                input_fingerprint=fingerprint,
                clear_checkpoint=clear_checkpoint,
            )
        except StageOwnershipError as exc:
            raise self._busy_error(stage, "Stage is actively owned by another worker.", exc) from exc

    def _invalidate_from(self, stage: str, fingerprints: dict[str, str]) -> None:
        start = CLIP_STAGES.index(stage)
        for name in CLIP_STAGES[start:]:
            record = self._get(name)
            if record is None:
                self._ensure(name, fingerprints[name])
                continue
            self._reset(name, fingerprints[name], clear_checkpoint=True)

    def _prepare_stage(self, stage: str, fingerprint: str) -> StageRecord:
        record = self._ensure(stage, fingerprint)
        if record.input_fingerprint != fingerprint:
            record = self._reset(stage, fingerprint, clear_checkpoint=True)
        if record.status is StageStatus.RUNNING and not record.lease_expired:
            raise self._busy_error(stage, "Stage is actively owned by another worker.")
        if record.status is StageStatus.FAILED:
            raise self._stored_error(stage, record)
        return record

    def _prepare_and_claim(self, stage: str, fingerprint: str) -> StageRecord:
        self._prepare_stage(stage, fingerprint)
        try:
            claimed = self.stage_store.claim_next(
                job_type=CLIP_JOB_TYPE,
                job_id=self.request.job.id,
                stage=stage,
                owner=self.owner,
                lease_seconds=self.lease_seconds,
            )
        except StageOwnershipError as exc:
            raise self._busy_error(stage, "Stage claim was lost.", exc) from exc
        if claimed is None:
            raise self._busy_error(stage, "Stage could not be claimed.")
        return claimed

    def _record_failure(
        self,
        stage: str,
        claimed: StageRecord,
        fingerprint: str,
        error: AIError,
        input_hashes: dict[str, Any],
    ) -> None:
        details = {
            **error.details,
            "stage": stage,
            "attempt": claimed.attempt,
            "failed_at": time.time(),
        }
        safe_error = {
            "category": error.category.value,
            "message": error.message,
            "retryable": error.retryable,
            "retry_after": error.retry_after,
            "details": details,
        }
        record = self._get(stage) or claimed
        existing = record.checkpoint if isinstance(record.checkpoint, dict) else {}
        checkpoint = {
            **existing,
            "version": _ARTIFACT_VERSION,
            "job_id": self.request.job.id,
            "stage": stage,
            "fingerprint": fingerprint,
            "payload_sha256": existing.get("payload_sha256", ""),
            "input_hashes": input_hashes,
            "status": "failed",
            "error": safe_error,
        }
        try:
            self.stage_store.save_checkpoint(
                job_type=CLIP_JOB_TYPE,
                job_id=self.request.job.id,
                stage=stage,
                owner=self.owner,
                checkpoint=checkpoint,
            )
            self.stage_store.fail(
                job_type=CLIP_JOB_TYPE,
                job_id=self.request.job.id,
                stage=stage,
                owner=self.owner,
                error=safe_error,
                retryable=error.retryable,
            )
        except StageOwnershipError as exc:
            raise self._lease_lost_error(stage, exc) from exc

    def _stored_error(self, stage: str, record: StageRecord) -> AIError:
        payload = record.error or {}
        details = payload.get("details")
        if not isinstance(details, dict):
            details = {}
        return AIError(
            str(payload.get("category") or ErrorCategory.INTERNAL_ERROR),
            str(payload.get("message") or f"Clip stage '{stage}' failed."),
            retryable=bool(payload.get("retryable", True)),
            retry_after=payload.get("retry_after")
            if isinstance(payload.get("retry_after"), (int, float))
            else None,
            details={**details, "stage": stage},
        )

    def _busy_error(self, stage: str, message: str, exc: Exception | None = None) -> AIError:
        return AIError(
            ErrorCategory.INTERNAL_ERROR,
            message,
            retryable=True,
            details={"stage": stage, "reason": "lease_held"},
            cause=exc,
        )

    def _lease_lost_error(self, stage: str, exc: Exception) -> AIError:
        return AIError(
            ErrorCategory.INTERNAL_ERROR,
            f"Clip stage '{stage}' ownership was lost before it could be committed.",
            retryable=True,
            details={"stage": stage, "reason": "lease_lost"},
            cause=exc,
        )

    def _ensure_stage_alive(self, stage: str) -> None:
        record = self._get(stage)
        alive = bool(
            record is not None
            and record.status is StageStatus.RUNNING
            and record.lease_owner == self.owner
            and record.lease_expires_at is not None
            and record.lease_expires_at > time.time()
        )
        if not alive:
            raise self._lease_lost_error(stage, RuntimeError("stage lease expired"))

    @property
    def _render_cancel_check(self) -> Callable[[], bool]:
        def check() -> bool:
            if self.cancel_check():
                return True
            return not self._stage_is_alive(STAGE_OUTPUT)

        return check

    def _stage_is_alive(self, stage: str) -> bool:
        record = self._get(stage)
        return bool(
            record is not None
            and record.status is StageStatus.RUNNING
            and record.lease_owner == self.owner
            and record.lease_expires_at is not None
            and record.lease_expires_at > time.time()
        )

    @contextmanager
    def _heartbeat(self, stage: str) -> Iterator[None]:
        stop = threading.Event()
        thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(stage, stop),
            name=f"clipping-clip-{stage}",
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=1.0)

    def _heartbeat_loop(self, stage: str, stop: threading.Event) -> None:
        while not stop.wait(self.heartbeat_seconds):
            try:
                if not self.stage_store.heartbeat(
                    job_type=CLIP_JOB_TYPE,
                    job_id=self.request.job.id,
                    stage=stage,
                    owner=self.owner,
                    lease_seconds=self.lease_seconds,
                ):
                    return
            except Exception:
                return

    def _notify(self, stage: str) -> None:
        if self.on_stage is not None:
            self.on_stage(stage)


@contextmanager
def _null_context() -> Iterator[None]:
    yield None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash_value(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    try:
        return hash_file(path)
    except OSError as exc:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            f"Clip input file is missing or unreadable: {path}",
            retryable=False,
            cause=exc,
        ) from exc


def _optional_hash(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    return _file_hash(path)


def final_output_path(directory: Path, job: ClipJob) -> Path:
    return Path(directory) / f"clip_{job.format.value}_{job.quality.value}.mp4"


def output_staging_path(directory: Path, job: ClipJob) -> Path:
    final = final_output_path(directory, job)
    return final.with_name(f".{final.stem}.rendering.part{final.suffix}")


def _source_values(request: ClipStageRequest) -> dict[str, Any]:
    source = request.source_job
    return {
        "source_job_id": source.id,
        "source_type": source.source_type.value,
        "source_value": source.source_value,
        "source_hash": source.source_hash or "",
        "source_sha256": _file_hash(request.source_path),
        "source_size": request.source_path.stat().st_size,
        "metadata_sha256": _hash_value(source.metadata),
        "source_info_sha256": _hash_value(request.source_info),
    }


def _prepare_payload(request: ClipStageRequest) -> dict[str, Any]:
    values = _source_values(request)
    job = request.job
    return {
        "version": _ARTIFACT_VERSION,
        "job_id": job.id,
        "source_job_id": request.source_job.id,
        "source_sha256": values["source_sha256"],
        "source_size": values["source_size"],
        "start_time": job.start_time,
        "end_time": job.end_time,
        "duration": job.duration,
        "format": job.format.value,
        "width": job.width,
        "height": job.height,
        "fps": job.fps,
        "quality": job.quality.value,
        "fit": job.fit.value,
        "source_info": asdict(request.source_info),
        "edit_plan": asdict(request.edit_plan),
        "composition": request.composition.to_dict(),
        "visual_settings": request.visual.model_dump(mode="json"),
        "render_options": _render_options(request),
        "effects_options": _effects_options(request),
        "captions_enabled": bool(job.captions and request.visual.captions),
    }


def _render_options(request: ClipStageRequest) -> dict[str, Any]:
    job = request.job
    config = request.config
    return {
        "format": job.format.value,
        "quality": job.quality.value,
        "fit": job.fit.value,
        "width": job.width,
        "height": job.height,
        "fps": job.fps,
        "duration": job.duration,
        "start_time": job.start_time,
        "end_time": job.end_time,
        "captions_enabled": bool(job.captions and request.visual.captions),
        "ffmpeg_binary": config.ffmpeg_binary,
        "ffprobe_binary": config.ffprobe_binary,
        "render_timeout_seconds": config.render_timeout_seconds,
        "ffprobe_timeout_seconds": config.ffprobe_timeout_seconds,
        "min_free_disk_bytes": config.min_free_disk_bytes,
        "x264_threads": config.x264_threads,
        "pixel_format": config.pixel_format,
        "audio_codec": config.audio_codec,
        "audio_channels": config.audio_channels,
        "audio_sample_rate": config.audio_sample_rate,
        "faststart": config.faststart,
        "quality_profile": config.quality_profile(job.quality).model_dump(mode="json"),
        "format_profile": config.format_profile(job.format).model_dump(mode="json"),
    }


def _caption_options(request: ClipStageRequest) -> dict[str, Any]:
    config = request.config
    return {
        "language": request.job.caption_language or "",
        "transcript_sha256": _hash_value(request.source_job.transcript),
        "font_arabic": config.caption_font_arabic,
        "font_latin": config.caption_font_latin,
        "font_size": config.caption_font_size,
        "font_size_ratio": config.caption_font_size_ratio,
        "margin_v_ratio": config.caption_margin_v_ratio,
        "outline": config.caption_outline,
        "shadow": config.caption_shadow,
        "border_style": config.caption_border_style,
        "box_padding_ratio": config.caption_box_padding_ratio,
        "encoding": config.caption_encoding,
        "max_chars_per_line": config.caption_max_chars_per_line,
        "max_lines": config.caption_max_lines,
        "min_duration": config.caption_min_duration,
        "max_duration": config.caption_max_duration,
        "primary_colour": config.caption_primary_colour,
        "outline_colour": config.caption_outline_colour,
        "back_colour": config.caption_back_colour,
        "accent_colour": config.caption_accent_colour,
        "emphasis_colour": config.caption_emphasis_colour,
        "emphasis_scale": config.caption_emphasis_scale,
        "safe_area_ratio": config.caption_safe_area_ratio,
        "shaping": config.caption_shaping,
        "dynamic": config.dynamic_captions,
        "phrase_max_words": config.caption_phrase_max_words,
        "phrase_max_chars": config.caption_phrase_max_chars,
        "phrase_max_seconds": config.caption_phrase_max_seconds,
        "break_gap_seconds": config.caption_break_gap_seconds,
        "importance_threshold": config.caption_importance_threshold,
    }


def _effects_options(request: ClipStageRequest) -> dict[str, Any]:
    config = request.config
    return {
        "enabled": config.effects_enabled,
        "min_importance": config.effects_min_importance,
        "max_visual": config.effects_max_visual,
        "zoom_min_duration": config.effects_zoom_min_duration,
        "zoom_duration": config.effects_zoom_duration,
        "zoom_intensity": config.effects_zoom_intensity,
        "punch_intensity": config.effects_punch_intensity,
        "punch_duration": config.effects_punch_duration,
        "emphasis_intensity": config.effects_emphasis_intensity,
        "emphasis_duration": config.effects_emphasis_duration,
        "min_gap_seconds": config.effects_min_gap_seconds,
        "transitions": config.effects_transitions,
        "transition_duration": config.effects_transition_duration,
        "blur_sigma": config.blur_sigma,
        "blur_steps": config.blur_steps,
    }


def clip_fingerprints(request: ClipStageRequest) -> dict[str, str]:
    source = _source_values(request)
    visual = request.visual.model_dump(mode="json")
    common = {
        "artifact_version": _ARTIFACT_VERSION,
        "clip_job_id": request.job.id,
        **source,
    }
    prepare = stable_fingerprint(
        scope="clip.render.prepare",
        **common,
        range={
            "start_time": request.job.start_time,
            "end_time": request.job.end_time,
            "duration": request.job.duration,
        },
        output={
            "format": request.job.format.value,
            "width": request.job.width,
            "height": request.job.height,
            "fps": request.job.fps,
            "quality": request.job.quality.value,
            "fit": request.job.fit.value,
            "captions_enabled": bool(request.job.captions and request.visual.captions),
        },
        visual_settings=visual,
        edit_plan_sha256=_hash_value(asdict(request.edit_plan)),
        composition_sha256=_hash_value(request.composition.to_dict()),
        effects_options_sha256=_hash_value(_effects_options(request)),
    )
    captions = stable_fingerprint(
        scope="clip.render.captions",
        **common,
        prepare=prepare,
        requested_language=request.job.caption_language or "",
        captions_enabled=bool(request.job.captions and request.visual.captions),
        transcript_sha256=_hash_value(request.source_job.transcript),
        highlight_sha256=_hash_value(request.highlight),
        signals_sha256=_hash_value(request.signals),
        caption_options_sha256=_hash_value(_caption_options(request)),
        callouts_sha256=_hash_value(
            [callout.model_dump(mode="json") for callout in request.composition.callouts]
        ),
    )
    output = stable_fingerprint(
        scope="clip.render.output",
        **common,
        prepare=prepare,
        captions=captions,
        render_options_sha256=_hash_value(_render_options(request)),
    )
    validate = stable_fingerprint(
        scope="clip.render.validate",
        **common,
        output=output,
        duration_tolerance=_DURATION_TOLERANCE,
        ffprobe_binary=request.config.ffprobe_binary,
        ffprobe_timeout_seconds=request.config.ffprobe_timeout_seconds,
    )
    finalize = stable_fingerprint(
        scope="clip.render.finalize",
        **common,
        validate=validate,
        output_filename=f"clip_{request.job.format.value}_{request.job.quality.value}.mp4",
    )
    return {
        STAGE_PREPARE: prepare,
        STAGE_CAPTIONS: captions,
        STAGE_OUTPUT: output,
        STAGE_VALIDATE: validate,
        STAGE_FINALIZE: finalize,
    }


def reset_failed_clip_stages(stage_store: StageStore, clip_id: str) -> None:
    for stage in CLIP_STAGES:
        record = stage_store.get(job_type=CLIP_JOB_TYPE, job_id=clip_id, stage=stage)
        if record is None or record.status is not StageStatus.FAILED:
            continue
        with contextlib.suppress(Exception):
            stage_store.reset(
                job_type=CLIP_JOB_TYPE,
                job_id=clip_id,
                stage=stage,
                input_fingerprint=record.input_fingerprint or "pending",
                clear_checkpoint=True,
            )


def release_clip_stages(stage_store: StageStore, clip_id: str | None = None) -> int:
    try:
        return stage_store.release_running(job_type=CLIP_JOB_TYPE, job_id=clip_id)
    except Exception:
        return 0


def preserved_clip_temporaries(
    stage_store: StageStore, clip_id: str, directory: Path
) -> set[Path]:
    preserved: set[Path] = set()
    record = stage_store.get(job_type=CLIP_JOB_TYPE, job_id=clip_id, stage=STAGE_OUTPUT)
    if record is None or not isinstance(record.checkpoint, dict):
        return preserved
    expected = record.checkpoint.get("staged_sha256")
    staged = record.checkpoint.get("staging_path")
    if not isinstance(staged, str):
        return preserved
    staging = Path(staged)
    if not isinstance(expected, str) or not staging.is_file():
        return preserved
    if _file_hash(staging) == expected:
        preserved.add(staging.resolve())
    return preserved


def cleanup_clip_temporaries(directory: Path, *, preserve: set[Path] | None = None) -> None:
    keep = {path.resolve() for path in preserve or ()}
    for path in Path(directory).glob("*"):
        if not path.is_file():
            continue
        name = path.name
        if ".part" not in name and not name.endswith(".tmp"):
            continue
        if path.resolve() in keep:
            continue
        with contextlib.suppress(OSError):
            path.unlink()
