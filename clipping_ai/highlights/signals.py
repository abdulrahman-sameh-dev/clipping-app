"""Lightweight audio/video activity signals using the existing ffmpeg toolchain."""

from __future__ import annotations

import array
import math
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from clipping_ai.analysis.config import HighlightSettings
from clipping_ai.highlights.models import AudioWindow, MediaSignals, MotionSample, SceneChange
from clipping_ai.video.models import VideoMetadata

_SCENE_TIME_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")
_SCENE_SCORE_RE = re.compile(r"lavfi\.scene_score=([0-9]+(?:\.[0-9]+)?)")
_MOTION_DIFF_RE = re.compile(r"lavfi\.signalstats\.YDIF=([0-9]+(?:\.[0-9]+)?)")
_FLOOR_DBFS = -90.0
_ENERGY_FLOOR_DBFS = -60.0


def _dbfs(amplitude: float) -> float:
    if amplitude <= 0:
        return _FLOOR_DBFS
    return max(_FLOOR_DBFS, 20.0 * math.log10(amplitude / 32768.0))


def _energy_from_dbfs(value: float) -> float:
    return min(1.0, max(0.0, (value - _ENERGY_FLOOR_DBFS) / -_ENERGY_FLOOR_DBFS))


def _window_from_samples(
    samples: array.array[int],
    *,
    start: float,
    sample_rate: int,
    duration: float,
    silence_dbfs: float,
) -> AudioWindow:
    peak = 0.0
    rms = 0.0
    if samples:
        peak = float(max(abs(sample) for sample in samples))
        rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    rms_dbfs = _dbfs(rms)
    peak_dbfs = _dbfs(peak)
    return AudioWindow(
        start=start,
        end=start + duration,
        rms_dbfs=round(rms_dbfs, 3),
        peak_dbfs=round(peak_dbfs, 3),
        energy=round(_energy_from_dbfs(rms_dbfs), 4),
        silence=rms_dbfs < silence_dbfs,
    )


def analyze_audio_signals(
    audio_path: Path,
    *,
    duration: float,
    config: HighlightSettings,
    ffmpeg_binary: str = "ffmpeg",
) -> tuple[list[AudioWindow], str | None]:
    if shutil.which(ffmpeg_binary) is None:
        return [], f"'{ffmpeg_binary}' is not installed; audio activity was not analysed."
    sample_rate = 8000
    window_samples = max(1, int(config.audio_window_seconds * sample_rate))
    window_bytes = window_samples * 2
    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(audio_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "-",
    ]
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        return [], f"Could not start ffmpeg for audio analysis: {exc}"

    windows: list[AudioWindow] = []
    pending = b""
    start = 0.0
    try:
        assert proc.stdout is not None
        while True:
            if time.monotonic() - started > config.audio_timeout_seconds:
                proc.kill()
                proc.wait(timeout=5)
                return windows, "Audio activity analysis timed out."
            block = proc.stdout.read(window_bytes * 8)
            if not block:
                break
            pending += block
            while len(pending) >= window_bytes:
                raw = pending[:window_bytes]
                pending = pending[window_bytes:]
                samples = array.array("h")
                samples.frombytes(raw)
                if sys.byteorder == "big":
                    samples.byteswap()
                windows.append(
                    _window_from_samples(
                        samples,
                        start=start,
                        sample_rate=sample_rate,
                        duration=config.audio_window_seconds,
                        silence_dbfs=config.silence_dbfs,
                    )
                )
                start += config.audio_window_seconds
        if pending:
            usable = len(pending) // 2
            samples = array.array("h")
            samples.frombytes(pending[: usable * 2])
            if sys.byteorder == "big":
                samples.byteswap()
            windows.append(
                _window_from_samples(
                    samples,
                    start=start,
                    sample_rate=sample_rate,
                    duration=min(config.audio_window_seconds, max(0.0, duration - start)),
                    silence_dbfs=config.silence_dbfs,
                )
            )
        returncode = proc.wait(timeout=max(1.0, config.audio_timeout_seconds))
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        return windows, "Audio activity analysis timed out."
    except Exception as exc:  # noqa: BLE001
        proc.kill()
        proc.wait(timeout=5)
        return windows, f"Audio activity analysis failed: {exc}"

    if returncode != 0:
        return windows, "ffmpeg could not decode audio for activity analysis."
    return windows, None


def analyze_scene_changes(
    video_path: Path,
    *,
    config: HighlightSettings,
    ffmpeg_binary: str = "ffmpeg",
) -> tuple[list[SceneChange], str | None]:
    if shutil.which(ffmpeg_binary) is None:
        return [], f"'{ffmpeg_binary}' is not installed; video activity was not analysed."
    filter_value = f"select=gt(scene\\,{config.scene_threshold:.3f}),metadata=print"
    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "info",
        "-i",
        str(video_path),
        "-filter:v",
        filter_value,
        "-an",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=config.scene_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return [], "Scene-change analysis timed out."
    except OSError as exc:
        return [], f"Could not run ffmpeg for scene analysis: {exc}"
    if proc.returncode != 0:
        return [], "ffmpeg could not analyse video scene changes."

    changes: list[SceneChange] = []
    pending_time: float | None = None
    for line in proc.stderr.splitlines():
        time_match = _SCENE_TIME_RE.search(line)
        if time_match:
            pending_time = float(time_match.group(1))
        score_match = _SCENE_SCORE_RE.search(line)
        if score_match and pending_time is not None:
            score = min(1.0, max(0.0, float(score_match.group(1))))
            changes.append(SceneChange(time=max(0.0, pending_time), score=score))
            pending_time = None
    return changes, None


def analyze_motion(
    video_path: Path,
    *,
    config: HighlightSettings,
    ffmpeg_binary: str = "ffmpeg",
) -> tuple[list[MotionSample], str | None]:
    if not config.analyze_motion:
        return [], None
    if shutil.which(ffmpeg_binary) is None:
        return [], f"'{ffmpeg_binary}' is not installed; motion activity was not analysed."
    filter_value = (
        f"fps={config.motion_fps:g},"
        f"scale={config.motion_scale_width}:-2,"
        "signalstats,metadata=print"
    )
    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "info",
        "-i",
        str(video_path),
        "-filter:v",
        filter_value,
        "-an",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=config.motion_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return [], "Motion analysis timed out."
    except OSError as exc:
        return [], f"Could not run ffmpeg for motion analysis: {exc}"
    if proc.returncode != 0:
        return [], "ffmpeg could not analyse video motion."

    samples: list[MotionSample] = []
    pending_time: float | None = None
    for line in proc.stderr.splitlines():
        time_match = _SCENE_TIME_RE.search(line)
        if time_match:
            pending_time = float(time_match.group(1))
        diff_match = _MOTION_DIFF_RE.search(line)
        if diff_match and pending_time is not None:
            diff = max(0.0, float(diff_match.group(1)))
            score = min(1.0, diff / config.motion_score_scale)
            samples.append(MotionSample(time=max(0.0, pending_time), score=score))
            pending_time = None
    return samples, None


def analyze_media_signals(
    media_path: Path,
    metadata: VideoMetadata,
    *,
    config: HighlightSettings,
    ffmpeg_binary: str = "ffmpeg",
) -> MediaSignals:
    notes: list[str] = []
    audio_windows: list[AudioWindow] = []
    scene_changes: list[SceneChange] = []
    motion_samples: list[MotionSample] = []
    audio_analyzed = False
    video_analyzed = False
    motion_analyzed = False

    if metadata.has_audio:
        audio_windows, note = analyze_audio_signals(
            media_path,
            duration=metadata.duration_seconds,
            config=config,
            ffmpeg_binary=ffmpeg_binary,
        )
        audio_analyzed = note is None
        if note:
            notes.append(note)
    else:
        notes.append("No audio stream was available for activity analysis.")

    if metadata.has_video:
        scene_changes, note = analyze_scene_changes(
            media_path,
            config=config,
            ffmpeg_binary=ffmpeg_binary,
        )
        video_analyzed = note is None
        if note:
            notes.append(note)
        motion_samples, note = analyze_motion(
            media_path,
            config=config,
            ffmpeg_binary=ffmpeg_binary,
        )
        motion_analyzed = note is None and config.analyze_motion
        if note:
            notes.append(note)
    else:
        notes.append("No video stream was available for scene or motion analysis.")

    return MediaSignals(
        duration=metadata.duration_seconds,
        audio_windows=audio_windows,
        scene_changes=scene_changes,
        motion_samples=motion_samples,
        audio_analyzed=audio_analyzed,
        video_analyzed=video_analyzed,
        motion_analyzed=motion_analyzed,
        notes=notes,
    )
