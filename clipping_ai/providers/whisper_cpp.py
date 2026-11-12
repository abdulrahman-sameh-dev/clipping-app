"""Optional local whisper.cpp speech-to-text provider.

Nothing is downloaded automatically. The user must point the configuration at
an existing whisper.cpp binary and GGML model file.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from clipping_ai.errors import AIError, ErrorCategory, policy_violation
from clipping_ai.providers.base import BaseSpeechProvider, ConnectionResult, sanitize_message
from clipping_ai.registry import ModelEntry
from clipping_ai.schemas import TranscriptResult, TranscriptSegment, TranscriptWord

_TIMESTAMP_RE = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


def _parse_timestamp(value: str) -> float | None:
    match = _TIMESTAMP_RE.match(value.strip())
    if not match:
        return None
    hours, minutes, seconds, millis = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000.0


def _offset_seconds(raw: dict[str, Any]) -> tuple[float, float] | None:
    offsets = raw.get("offsets")
    if isinstance(offsets, dict):
        start = offsets.get("from")
        end = offsets.get("to")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            return start / 1000.0, end / 1000.0
    timestamps = raw.get("timestamps")
    if isinstance(timestamps, dict):
        start = _parse_timestamp(str(timestamps.get("from", "")))
        end = _parse_timestamp(str(timestamps.get("to", "")))
        if start is not None and end is not None:
            return start, end
    return None


class WhisperCppProvider(BaseSpeechProvider):
    def __init__(self, provider_id: str, config: Any) -> None:
        super().__init__(provider_id, config)

    def _resolve_binary(self) -> tuple[str | None, str]:
        raw = (self.config.binary or "whisper-cli").strip()
        if not raw:
            return None, "whisper.cpp binary is not configured"
        candidate = Path(raw).expanduser()
        if candidate.is_file():
            return str(candidate), ""
        found = shutil.which(raw)
        if found:
            return found, ""
        if candidate.is_absolute() or "/" in raw:
            return None, f"whisper.cpp binary not found: {raw} (configured path does not exist)"
        return None, f"whisper.cpp binary not found: {raw} (not on PATH)"

    def _binary(self) -> str | None:
        return self._resolve_binary()[0]

    def _model_path(self) -> Path | None:
        raw = self.config.model_path
        if not raw:
            return None
        path = Path(raw).expanduser()
        return path if path.is_file() else None

    def inspect(self) -> ConnectionResult:
        raw_binary = (self.config.binary or "whisper-cli").strip()
        raw_model = (self.config.model_path or "").strip()
        details: dict[str, Any] = {
            "binary": raw_binary,
            "model_path": raw_model or None,
        }
        if not self.config.enabled:
            return ConnectionResult(
                ok=False,
                provider=self.provider_id,
                model=None,
                message="provider disabled in configuration",
                details={
                    **details,
                    "status": "disabled",
                    "category": ErrorCategory.PROVIDER_UNAVAILABLE.value,
                },
            )
        binary, binary_error = self._resolve_binary()
        if binary is None:
            status = (
                "misconfigured"
                if Path(raw_binary).is_absolute() or "/" in raw_binary
                else "not_installed"
            )
            return ConnectionResult(
                ok=False,
                provider=self.provider_id,
                model=None,
                message=binary_error,
                details={
                    **details,
                    "status": status,
                    "category": ErrorCategory.PROVIDER_UNAVAILABLE.value,
                },
            )
        details["binary"] = binary
        if not raw_model:
            return ConnectionResult(
                ok=False,
                provider=self.provider_id,
                model=None,
                message=(
                    "whisper.cpp model_path is not set. Set it to an existing "
                    "GGML/GGUF model file."
                ),
                details={
                    **details,
                    "status": "model_missing",
                    "category": ErrorCategory.PROVIDER_UNAVAILABLE.value,
                },
            )
        model = Path(raw_model).expanduser()
        if not model.is_file():
            return ConnectionResult(
                ok=False,
                provider=self.provider_id,
                model=None,
                message=f"whisper.cpp model file not found: {model}",
                details={
                    **details,
                    "status": "model_missing",
                    "category": ErrorCategory.PROVIDER_UNAVAILABLE.value,
                },
            )
        details["model_path"] = str(model)
        return ConnectionResult(
            ok=True,
            provider=self.provider_id,
            model=None,
            message="whisper.cpp binary and model are available.",
            details={**details, "status": "ready"},
        )

    def available(self) -> tuple[bool, str]:
        result = self.inspect()
        return result.ok, result.message

    def health(self, entry: ModelEntry | None = None) -> ConnectionResult:
        started = time.perf_counter()
        result = self.inspect()
        if not result.ok:
            result.model = entry.id if entry else None
            result.latency_ms = int((time.perf_counter() - started) * 1000)
            return result
        binary = str(result.details.get("binary"))
        try:
            proc = subprocess.run(
                [binary, "--help"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ConnectionResult(
                ok=False,
                provider=self.provider_id,
                model=entry.id if entry else None,
                message=f"Could not run whisper.cpp: {sanitize_message(str(exc))}",
                latency_ms=int((time.perf_counter() - started) * 1000),
                details={
                    **result.details,
                    "status": "error",
                    "category": ErrorCategory.PROVIDER_UNAVAILABLE.value,
                },
            )
        if proc.returncode != 0:
            detail = sanitize_message((proc.stderr or proc.stdout).strip()[:500])
            message = "whisper.cpp --help failed."
            if detail:
                message = f"{message} {detail}"
            return ConnectionResult(
                ok=False,
                provider=self.provider_id,
                model=entry.id if entry else None,
                message=message,
                latency_ms=int((time.perf_counter() - started) * 1000),
                details={
                    **result.details,
                    "status": "error",
                    "category": ErrorCategory.PROVIDER_UNAVAILABLE.value,
                    "help_exit_code": proc.returncode,
                },
            )
        return ConnectionResult(
            ok=True,
            provider=self.provider_id,
            model=entry.id if entry else None,
            message="whisper.cpp binary and model are available.",
            latency_ms=int((time.perf_counter() - started) * 1000),
            details={
                **result.details,
                "status": "ready",
                "help_exit_code": proc.returncode,
            },
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
        binary = self._binary()
        model_path = self._model_path()
        if binary is None or model_path is None:
            ok, reason = self.available()
            raise AIError(
                ErrorCategory.PROVIDER_UNAVAILABLE,
                f"whisper.cpp is not usable: {reason}",
                provider=self.provider_id,
                model=entry.id,
                retryable=False,
            )

        audio_path = Path(audio_path)
        if not audio_path.exists():
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"Audio file does not exist: {audio_path}",
                provider=self.provider_id,
                model=entry.id,
                retryable=False,
            )

        with tempfile.TemporaryDirectory(prefix="clipping-ai-whispercpp-") as tmp:
            out_base = Path(tmp) / "transcript"
            args = [
                binary,
                "-m",
                str(model_path),
                "-f",
                str(audio_path),
                "-of",
                str(out_base),
                "-ojf" if word_timestamps else "-oj",
                "-np",
            ]
            if language:
                args += ["-l", language]
            if prompt:
                args += ["--prompt", prompt]
            args += list(self.config.extra_args)

            try:
                proc = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=timeout or self.config.timeout_seconds or 1800,
                )
            except subprocess.TimeoutExpired as exc:
                raise AIError(
                    ErrorCategory.NETWORK_TIMEOUT,
                    "whisper.cpp transcription timed out.",
                    provider=self.provider_id,
                    model=entry.id,
                    retryable=True,
                    cause=exc,
                ) from exc
            except OSError as exc:
                raise AIError(
                    ErrorCategory.PROVIDER_UNAVAILABLE,
                    f"Could not execute whisper.cpp: {sanitize_message(str(exc))}",
                    provider=self.provider_id,
                    model=entry.id,
                    retryable=False,
                    cause=exc,
                ) from exc

            if proc.returncode != 0:
                raise AIError(
                    ErrorCategory.PROVIDER_UNAVAILABLE,
                    "whisper.cpp failed: " + sanitize_message(proc.stderr.strip()[:500]),
                    provider=self.provider_id,
                    model=entry.id,
                    retryable=False,
                )

            json_path = out_base.with_suffix(".json")
            if not json_path.exists():
                raise AIError(
                    ErrorCategory.INVALID_RESPONSE,
                    "whisper.cpp did not produce a JSON transcript.",
                    provider=self.provider_id,
                    model=entry.id,
                    retryable=True,
                )
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise AIError(
                    ErrorCategory.INVALID_RESPONSE,
                    f"Could not parse whisper.cpp JSON: {exc}",
                    provider=self.provider_id,
                    model=entry.id,
                    retryable=True,
                    cause=exc,
                ) from exc

        return self._parse(payload, entry)

    def _parse(self, payload: dict[str, Any], entry: ModelEntry) -> TranscriptResult:
        segments: list[TranscriptSegment] = []
        words: list[TranscriptWord] = []
        for raw in payload.get("transcription") or []:
            if not isinstance(raw, dict):
                continue
            times = _offset_seconds(raw)
            text = str(raw.get("text") or "").strip()
            if not text:
                continue
            start, end = times if times else (0.0, 0.0)
            segments.append(
                TranscriptSegment(id=len(segments), start=start, end=max(end, start), text=text)
            )
            for token in raw.get("tokens") or []:
                if not isinstance(token, dict):
                    continue
                token_times = _offset_seconds(token)
                token_text = str(token.get("text") or "").strip()
                if (
                    not token_text
                    or not token_times
                    or (token_text.startswith("[") and token_text.endswith("]"))
                ):
                    continue
                probability = token.get("p")
                words.append(
                    TranscriptWord(
                        word=token_text,
                        start=token_times[0],
                        end=max(token_times[1], token_times[0]),
                        probability=float(probability)
                        if isinstance(probability, (int, float))
                        else None,
                    )
                )

        text = " ".join(segment.text for segment in segments).strip()
        language = None
        result_meta = payload.get("result")
        if isinstance(result_meta, dict):
            language = result_meta.get("language")
        duration = max((segment.end for segment in segments), default=None)
        return TranscriptResult(
            text=text,
            language=str(language) if language else None,
            duration=duration,
            segments=segments,
            words=words,
            provider=self.provider_id,
            model=entry.id,
        )

    def test_connection(self, entry: ModelEntry | None = None) -> ConnectionResult:
        return self.health(entry)
