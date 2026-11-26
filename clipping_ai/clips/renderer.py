"""FFmpeg renderer that cuts clips from the original source media."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clipping_ai.clips.config import ClipConfig
from clipping_ai.clips.editing import EditPlan, Effect
from clipping_ai.clips.models import ClipFit, ClipJob
from clipping_ai.errors import AIError, ErrorCategory

_CancelCheck = Callable[[], bool]


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    has_video: bool
    has_audio: bool
    video_codec: str | None
    audio_codec: str | None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class RenderResult:
    path: Path
    size_bytes: int
    duration: float
    filter_graph: str = ""
    effect_filters: tuple[str, ...] = ()
    overlay_path: Path | None = None


def _run_probe(binary: str, path: Path, timeout: float) -> dict[str, Any]:
    command = [
        binary,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise AIError(
            ErrorCategory.INTERNAL_ERROR,
            f"Media probe binary '{binary}' was not found.",
            retryable=False,
            cause=exc,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AIError(
            ErrorCategory.NETWORK_TIMEOUT,
            f"Media probe timed out after {timeout:.0f}s.",
            retryable=True,
            cause=exc,
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        message = detail[-1] if detail else f"ffprobe exited with {completed.returncode}"
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            f"Could not probe media '{path.name}': {message}",
            retryable=False,
        )
    try:
        data = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise AIError(
            ErrorCategory.INVALID_RESPONSE,
            "ffprobe returned invalid JSON.",
            retryable=True,
            cause=exc,
        ) from exc
    return data if isinstance(data, dict) else {}


def probe_media(path: Path, config: ClipConfig) -> MediaInfo:
    data = _run_probe(config.ffprobe_binary, path, config.ffprobe_timeout_seconds)
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = 0.0
    for candidate in ((data.get("format") or {}).get("duration"), (video or {}).get("duration")):
        if candidate is None:
            continue
        try:
            duration = float(candidate)
        except (TypeError, ValueError):
            continue
        if duration > 0:
            break
    return MediaInfo(
        duration=duration,
        has_video=video is not None,
        has_audio=audio is not None,
        video_codec=(video or {}).get("codec_name"),
        audio_codec=(audio or {}).get("codec_name"),
        width=(video or {}).get("width"),
        height=(video or {}).get("height"),
    )


def _base_filter(job: ClipJob, config: ClipConfig) -> str:
    width, height, fps = job.width, job.height, job.fps
    if job.fit == ClipFit.PAD:
        return (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps={fps},format={config.pixel_format}[vbase]"
        )
    if job.fit == ClipFit.BLUR:
        return (
            f"[0:v]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur={config.blur_sigma}:{config.blur_steps}[bgb];"
            f"[fgsrc]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,"
            f"setsar=1,fps={fps},format={config.pixel_format}[vbase]"
        )
    return (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps={fps},format={config.pixel_format}[vbase]"
    )


def _effect_filters(job: ClipJob, effects: tuple[Effect, ...], config: ClipConfig) -> list[str]:
    zoom_effects = [effect for effect in effects if effect.kind == "zoom"]
    punch_effects = [effect for effect in effects if effect.kind == "punch"]
    emphasis_effects = [effect for effect in effects if effect.kind == "emphasis"]
    fade_in = any(effect.kind == "fade_in" for effect in effects)
    fade_out = any(effect.kind == "fade_out" for effect in effects)
    filters: list[str] = []
    if zoom_effects or punch_effects:
        terms = ["1"]
        for effect in zoom_effects:
            duration = max(0.25, effect.duration)
            progress = (
                f"min(1,max(0,(on/{job.fps:g}-{effect.start:.3f})/{duration:.3f}))"
            )
            eased = f"({progress}*{progress}*(3-2*{progress}))"
            terms.append(f"{effect.intensity:.4f}*{eased}")
        for effect in punch_effects:
            half = max(0.2, effect.duration / 2.0)
            center = effect.start + effect.duration / 2.0
            progress = f"min(1,max(0,1-abs(on/{job.fps:g}-{center:.3f})/{half:.3f}))"
            eased = f"({progress}*{progress}*(3-2*{progress}))"
            terms.append(f"{effect.intensity:.4f}*{eased}")
        expression = "+".join(terms)
        filters.append(
            f"zoompan=z='{expression}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:s={job.width}x{job.height}:fps={job.fps:g}"
        )
    if emphasis_effects:
        brightness_terms: list[str] = []
        saturation_terms: list[str] = []
        for effect in emphasis_effects:
            half = max(0.2, effect.duration / 2.0)
            center = effect.start + effect.duration / 2.0
            progress = f"min(1,max(0,1-abs(t-{center:.3f})/{half:.3f}))"
            pulse = f"({progress}*{progress}*(3-2*{progress}))"
            brightness_terms.append(f"{effect.intensity * 0.6:.4f}*{pulse}")
            saturation_terms.append(f"{effect.intensity * 1.2:.4f}*{pulse}")
        filters.append(
            f"eq=brightness='{'+'.join(brightness_terms)}'"
            f":saturation='1+{'+'.join(saturation_terms)}'"
        )
    transition = config.effects_transition_duration
    if fade_in:
        filters.append(f"fade=t=in:st=0:d={transition:g}")
    if fade_out:
        filters.append(
            f"fade=t=out:st={max(0.0, job.duration - transition):.3f}:d={transition:g}"
        )
    return filters


def build_filter_graph(
    job: ClipJob,
    config: ClipConfig,
    edit_plan: EditPlan | None = None,
    overlay_path: Path | None = None,
) -> str:
    graph = _base_filter(job, config)
    label = "vbase"
    effects = edit_plan.effects if edit_plan is not None else ()
    effect_filters = _effect_filters(job, effects, config)
    if effect_filters:
        graph += f";[{label}]" + ",".join(effect_filters) + "[veff]"
        label = "veff"
    if job.captions:
        if job.caption_path is None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Caption rendering was requested but no ASS file was generated.",
                retryable=False,
            )
        graph += (
            f";[{label}]ass=filename={job.caption_path.name}:"
            f"shaping={config.caption_shaping}[vcap]"
        )
        label = "vcap"
    if overlay_path is not None:
        graph += (
            f";[{label}]ass=filename={overlay_path.name}:"
            f"shaping={config.caption_shaping}[vover]"
        )
        label = "vover"
    graph += f";[{label}]null[vout]"
    return graph


def _build_command(
    job: ClipJob, config: ClipConfig, source: Path, part_path: Path, graph: str
) -> list[str]:
    quality = config.quality_profile(job.quality)
    command = [
        config.ffmpeg_binary,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss",
        f"{job.start_time:.3f}",
        "-i",
        str(source),
        "-t",
        f"{job.duration:.3f}",
        "-filter_complex",
        graph,
        "-map",
        "[vout]",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        quality.preset,
        "-crf",
        str(quality.crf),
        "-threads",
        str(config.x264_threads),
        "-pix_fmt",
        config.pixel_format,
        "-r",
        f"{job.fps:g}",
        "-c:a",
        config.audio_codec,
        "-b:a",
        quality.audio_bitrate,
        "-avoid_negative_ts",
        "make_zero",
        "-fps_mode",
        "cfr",
        "-max_muxing_queue_size",
        "2048",
    ]
    if config.faststart:
        command.extend(["-movflags", "+faststart"])
    if config.audio_sample_rate:
        command.extend(["-ar", str(config.audio_sample_rate)])
    if config.audio_channels:
        command.extend(["-ac", str(config.audio_channels)])
    command.append(str(part_path))
    return command


def _cleanup(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _read_log_tail(path: Path, limit: int = 4000) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return data[-limit:].strip()


def render_clip(
    job: ClipJob,
    *,
    source_path: Path,
    output_path: Path,
    config: ClipConfig,
    cancel_check: _CancelCheck | None = None,
    source_info: MediaInfo | None = None,
    edit_plan: EditPlan | None = None,
    overlay_path: Path | None = None,
) -> RenderResult:
    source_path = source_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if not config.enabled:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "Clip rendering is disabled in configuration.",
            retryable=False,
        )
    if not source_path.is_file():
        raise AIError(
            ErrorCategory.NOT_FOUND,
            f"Source media '{source_path}' does not exist.",
            retryable=False,
        )
    if job.captions and job.caption_path is not None:
        caption_path = job.caption_path.expanduser().resolve()
        if caption_path.parent != output_path.parent:
            raise AIError(
                ErrorCategory.INTERNAL_ERROR,
                "Caption file must be in the same directory as the rendered clip.",
                retryable=False,
            )
        if not caption_path.is_file():
            raise AIError(
                ErrorCategory.NOT_FOUND,
                f"Caption file '{caption_path}' does not exist.",
                retryable=False,
            )
        job = job.model_copy(update={"caption_path": caption_path})

    if overlay_path is not None:
        overlay_path = overlay_path.expanduser().resolve()
        if overlay_path.parent != output_path.parent:
            raise AIError(
                ErrorCategory.INTERNAL_ERROR,
                "Overlay file must be in the same directory as the rendered clip.",
                retryable=False,
            )
        if not overlay_path.is_file():
            raise AIError(
                ErrorCategory.NOT_FOUND,
                f"Overlay file '{overlay_path}' does not exist.",
                retryable=False,
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    info = source_info or probe_media(source_path, config)
    if not info.has_video:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "Source media has no video stream to render.",
            retryable=False,
        )
    if info.duration > 0 and job.end_time > info.duration + 0.05:
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            f"Clip end ({job.end_time:.2f}s) exceeds source duration "
            f"({info.duration:.2f}s).",
            retryable=False,
        )

    free = shutil.disk_usage(output_path.parent).free
    if free < config.min_free_disk_bytes:
        raise AIError(
            ErrorCategory.INTERNAL_ERROR,
            f"Not enough free disk space to render a clip "
            f"({free} bytes available, {config.min_free_disk_bytes} required).",
            retryable=False,
        )

    part_path = output_path.with_name(f"{output_path.stem}.part{output_path.suffix}")
    log_path = output_path.with_name(f"{output_path.stem}.ffmpeg.log")
    _cleanup(part_path)
    graph = build_filter_graph(job, config, edit_plan, overlay_path)
    effect_filters = tuple(
        _effect_filters(job, edit_plan.effects if edit_plan is not None else (), config)
    )
    command = _build_command(job, config, source_path, part_path, graph)
    started = time.monotonic()
    cancelled = False

    try:
        with log_path.open("w", encoding="utf-8") as log_handle:
            process = subprocess.Popen(
                command,
                cwd=str(output_path.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log_handle,
                text=True,
            )
            while process.poll() is None:
                if cancel_check is not None and cancel_check():
                    cancelled = True
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                    break
                if time.monotonic() - started > config.render_timeout_seconds:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                    raise AIError(
                        ErrorCategory.NETWORK_TIMEOUT,
                        f"Clip rendering timed out after {config.render_timeout_seconds:.0f}s.",
                        retryable=True,
                    )
                time.sleep(0.2)
            returncode = process.returncode
    except FileNotFoundError as exc:
        raise AIError(
            ErrorCategory.INTERNAL_ERROR,
            f"FFmpeg binary '{config.ffmpeg_binary}' was not found.",
            retryable=False,
            cause=exc,
        ) from exc
    if cancelled:
        if not config.keep_failed_artifacts:
            _cleanup(part_path)
            _cleanup(log_path)
        raise AIError(
            ErrorCategory.INVALID_REQUEST,
            "Clip rendering was cancelled.",
            retryable=False,
        )
    if returncode != 0:
        detail = _read_log_tail(log_path) if log_path.exists() else ""
        _cleanup(part_path)
        if not config.keep_failed_artifacts:
            _cleanup(log_path)
        message = "FFmpeg failed to render the clip."
        if detail:
            message = f"{message} {detail.splitlines()[-1]}"
        raise AIError(
            ErrorCategory.INTERNAL_ERROR,
            message,
            retryable=True,
            details={"ffmpeg_log": detail[-2000:]},
        )

    if not part_path.exists() or part_path.stat().st_size == 0:
        _cleanup(part_path)
        _cleanup(log_path)
        raise AIError(
            ErrorCategory.INTERNAL_ERROR,
            "FFmpeg reported success but produced no output file.",
            retryable=True,
        )

    try:
        output_info = probe_media(part_path, config)
        if not output_info.has_video:
            raise AIError(
                ErrorCategory.INTERNAL_ERROR,
                "Rendered clip has no video stream.",
                retryable=True,
            )
        if info.has_audio and not output_info.has_audio:
            raise AIError(
                ErrorCategory.INTERNAL_ERROR,
                "Rendered clip lost the source audio stream.",
                retryable=True,
            )
        if output_info.duration + 0.75 < job.duration:
            raise AIError(
                ErrorCategory.INTERNAL_ERROR,
                f"Rendered clip is too short ({output_info.duration:.2f}s of "
                f"{job.duration:.2f}s).",
                retryable=True,
            )
    except Exception:
        _cleanup(part_path)
        if not config.keep_failed_artifacts:
            _cleanup(log_path)
        raise

    os.replace(part_path, output_path)
    if not config.keep_failed_artifacts:
        _cleanup(log_path)
    return RenderResult(
        path=output_path,
        size_bytes=output_path.stat().st_size,
        duration=output_info.duration,
        filter_graph=graph,
        effect_filters=effect_filters,
        overlay_path=overlay_path,
    )
