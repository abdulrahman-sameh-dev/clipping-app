#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import tempfile
from array import array
from pathlib import Path
from typing import Any

ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
WHITESPACE_RE = re.compile(r"\s+")


def run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, capture_output=True, check=False)


def probe(path: Path) -> dict[str, Any]:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    return json.loads(result.stdout)


def stream_of(media: dict[str, Any], kind: str) -> dict[str, Any] | None:
    for stream in media.get("streams", []):
        if stream.get("codec_type") == kind:
            return stream
    return None


def pcm_mono(path: Path, sample_rate: int, start: float | None, duration: float | None) -> array:
    command = ["ffmpeg", "-v", "error"]
    if start is not None:
        command += ["-ss", f"{start:.6f}"]
    if duration is not None:
        command += ["-t", f"{duration:.6f}"]
    command += [
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "-",
    ]
    result = run(command)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    samples = array("h")
    samples.frombytes(result.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def correlation(a: list[float], b: list[float]) -> float | None:
    count = min(len(a), len(b))
    if count < 100:
        return None
    left = a[:count]
    right = b[:count]
    mean_left = sum(left) / count
    mean_right = sum(right) / count
    var_left = sum((value - mean_left) ** 2 for value in left)
    var_right = sum((value - mean_right) ** 2 for value in right)
    if var_left <= 0 or var_right <= 0:
        return None
    covariance = sum(
        (left[index] - mean_left) * (right[index] - mean_right) for index in range(count)
    )
    return covariance / math.sqrt(var_left * var_right)


def audio_correlation(
    source: Path,
    output: Path,
    start: float,
    end: float,
    *,
    sample_rate: int = 1000,
    stride: int = 1,
    max_offset_seconds: float = 0.5,
) -> dict[str, Any]:
    duration = max(0.0, end - start)
    source_pcm = pcm_mono(source, sample_rate, start, duration)
    output_pcm = pcm_mono(output, sample_rate, None, None)
    source_samples = [float(value) for value in source_pcm[::stride]]
    output_samples = [float(value) for value in output_pcm[::stride]]
    if not source_samples or not output_samples:
        return {"applicable": False, "reason": "empty audio samples"}
    source_rms = math.sqrt(sum(value * value for value in source_samples) / len(source_samples))
    if source_rms < 10:
        return {"applicable": False, "reason": "source segment is silent"}

    effective_rate = sample_rate / stride
    max_offset = max(1, int(max_offset_seconds * effective_rate))
    best_offset = 0
    best_value = -1.0
    for offset in range(-max_offset, max_offset + 1):
        if offset >= 0:
            left = source_samples
            right = output_samples[offset:]
        else:
            left = source_samples[-offset:]
            right = output_samples
        value = correlation(left, right)
        if value is not None and value > best_value:
            best_value = value
            best_offset = offset
    if best_value < 0:
        return {"applicable": False, "reason": "correlation could not be computed"}
    return {
        "applicable": True,
        "correlation": round(best_value, 6),
        "offset_samples": best_offset,
        "offset_seconds": round(best_offset / effective_rate, 6),
        "source_rms": round(source_rms, 3),
    }


def frame_rgb(path: Path, time_seconds: float) -> tuple[int, int, bytes]:
    result = run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{time_seconds:.6f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    media = probe(path)
    video = stream_of(media, "video")
    if video is None:
        raise RuntimeError("output has no video stream")
    width = int(video["width"])
    height = int(video["height"])
    expected = width * height * 3
    if len(result.stdout) < expected:
        raise RuntimeError(f"expected {expected} RGB bytes, received {len(result.stdout)}")
    return width, height, result.stdout[:expected]


def caption_pixel_fraction(path: Path, time_seconds: float) -> float:
    width, height, pixels = frame_rgb(path, time_seconds)
    top = int(height * 0.68)
    total = 0
    bright = 0
    for y in range(top, height):
        row = y * width * 3
        for x in range(width):
            index = row + x * 3
            red, green, blue = pixels[index], pixels[index + 1], pixels[index + 2]
            total += 1
            if red > 200 and green > 200 and blue > 200:
                bright += 1
    return bright / total if total else 0.0


def caption_reference_diff(
    output: Path,
    reference: Path,
    time_seconds: float,
) -> dict[str, Any]:
    width, height, pixels = frame_rgb(output, time_seconds)
    ref_width, ref_height, ref_pixels = frame_rgb(reference, time_seconds)
    if width != ref_width or height != ref_height:
        return {"comparable": False, "reason": "frame dimensions differ"}
    top = int(height * 0.68)
    total = 0
    changed = 0
    total_abs = 0
    for y in range(top, height):
        row = y * width * 3
        for x in range(width):
            index = row + x * 3
            for channel in range(3):
                delta = abs(pixels[index + channel] - ref_pixels[index + channel])
                total_abs += delta
                if delta > 25:
                    changed += 1
            total += 1
    changed_fraction = changed / (total * 3) if total else 0.0
    mean_abs = total_abs / (total * 3) if total else 0.0
    return {
        "comparable": True,
        "changed_fraction": round(changed_fraction, 6),
        "mean_abs_diff": round(mean_abs, 6),
    }


def caption_ocr(path: Path, time_seconds: float) -> str:
    width, height, _ = frame_rgb(path, time_seconds)
    with tempfile.TemporaryDirectory(prefix="clipping-ocr-") as temp_dir:
        image_path = Path(temp_dir) / "caption.png"
        result = run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-ss",
                f"{time_seconds:.6f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                f"crop={width}:{int(height * 0.30)}:0:{int(height * 0.68)},scale={width * 2}:-1",
                str(image_path),
            ]
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
        ocr = run(["tesseract", str(image_path), "stdout", "-l", "eng", "--psm", "6"])
        if ocr.returncode != 0:
            return ""
        return ocr.stdout.decode("utf-8", errors="replace").strip()


def normalize_text(value: str) -> str:
    return WHITESPACE_RE.sub(" ", value).strip().lower()


def check_ass(
    ass_path: Path,
    *,
    expected_language: str | None,
    expected_text: str | None,
) -> dict[str, Any]:
    text = ass_path.read_text(encoding="utf-8")
    dialogue = [line for line in text.splitlines() if line.startswith("Dialogue:")]
    style_lines = [line for line in text.splitlines() if line.startswith("Style: Default")]
    font = ""
    if style_lines:
        parts = style_lines[0].split(",")
        font = parts[1] if len(parts) > 1 else ""
    has_arabic = ARABIC_RE.search(text) is not None
    checks: list[dict[str, Any]] = [
        {"name": "ass_exists", "pass": ass_path.is_file()},
        {"name": "ass_dialogue_events", "pass": len(dialogue) > 0, "value": len(dialogue)},
        {"name": "ass_font", "pass": bool(font), "value": font},
    ]
    if expected_language and expected_language.lower().startswith("ar"):
        checks.append(
            {"name": "ass_arabic_text", "pass": has_arabic, "value": has_arabic}
        )
        checks.append(
            {
                "name": "ass_arabic_font",
                "pass": font == "Noto Sans Arabic",
                "value": font,
            }
        )
    if expected_language and expected_language.lower().startswith("en"):
        checks.append(
            {
                "name": "ass_latin_font",
                "pass": font == "Noto Sans",
                "value": font,
            }
        )
    if expected_text:
        checks.append(
            {
                "name": "ass_expected_text",
                "pass": normalize_text(expected_text) in normalize_text(text),
                "value": expected_text,
            }
        )
    return {"checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a rendered clipping output.")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", type=float)
    parser.add_argument("--end", type=float)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--fps", type=float)
    parser.add_argument("--audio", action="store_true")
    parser.add_argument("--ass", type=Path)
    parser.add_argument("--caption-language")
    parser.add_argument("--caption-time", type=float, default=1.0)
    parser.add_argument("--caption-text")
    parser.add_argument("--ocr-english", action="store_true")
    parser.add_argument("--caption-reference", type=Path)
    parser.add_argument("--contact-sheet", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, value: Any = None, skipped: bool = False) -> None:
        item: dict[str, Any] = {"name": name, "pass": bool(passed)}
        if skipped:
            item["skipped"] = True
        if value is not None:
            item["value"] = value
        checks.append(item)

    add("output_exists", args.output.is_file())
    add("output_nonempty", args.output.is_file() and args.output.stat().st_size > 0)
    if not args.output.is_file():
        print(json.dumps({"checks": checks, "pass": False}, indent=2))
        return 1

    media = probe(args.output)
    video = stream_of(media, "video")
    audio = stream_of(media, "audio")
    add("video_stream", video is not None)
    add("audio_stream", audio is not None, bool(audio))
    if video is not None:
        add("width", int(video.get("width", 0)) == args.width, video.get("width"))
        add("height", int(video.get("height", 0)) == args.height, video.get("height"))
        add("pixel_format", video.get("pix_fmt") == "yuv420p", video.get("pix_fmt"))
        if args.fps:
            add(
                "fps",
                abs(float(video.get("avg_frame_rate", "0/1").split("/")[0]) / max(1, int(video.get("avg_frame_rate", "0/1").split("/")[1])) - args.fps) < 0.01,
                video.get("avg_frame_rate"),
            )
    if audio is not None:
        add("audio_codec", audio.get("codec_name") == "aac", audio.get("codec_name"))
    if args.audio:
        add("audio_requested", audio is not None)

    actual_duration = float(media.get("format", {}).get("duration", 0.0))
    if args.start is not None and args.end is not None:
        expected_duration = args.end - args.start
        tolerance = max(0.75, expected_duration * 0.02)
        add(
            "duration",
            abs(actual_duration - expected_duration) <= tolerance,
            {
                "expected": round(expected_duration, 6),
                "actual": round(actual_duration, 6),
                "tolerance": round(tolerance, 6),
            },
        )
    if video is not None and audio is not None:
        video_duration = float(video.get("duration", 0.0))
        audio_duration = float(audio.get("duration", 0.0))
        add(
            "av_duration_delta",
            abs(video_duration - audio_duration) <= 0.5,
            round(abs(video_duration - audio_duration), 6),
        )

    decode = run(["ffmpeg", "-v", "error", "-i", str(args.output), "-f", "null", "-"])
    add("decode", decode.returncode == 0, decode.stderr.decode("utf-8", errors="replace")[-1000:])

    if args.source is not None and args.start is not None and args.end is not None and args.audio:
        correlation_result = audio_correlation(args.source, args.output, args.start, args.end)
        if correlation_result.get("applicable"):
            add(
                "audio_correlation",
                float(correlation_result["correlation"]) >= 0.8,
                correlation_result,
            )
        else:
            add("audio_correlation", False, correlation_result)

    if args.ass is not None:
        ass_result = check_ass(
            args.ass,
            expected_language=args.caption_language,
            expected_text=args.caption_text,
        )
        checks.extend(ass_result["checks"])
        pixel_fraction = caption_pixel_fraction(args.output, args.caption_time)
        add("caption_pixels", pixel_fraction >= 0.002, round(pixel_fraction, 6))
        if args.caption_reference is not None:
            diff = caption_reference_diff(args.output, args.caption_reference, args.caption_time)
            add(
                "caption_reference_diff",
                bool(diff.get("comparable"))
                and (
                    float(diff.get("changed_fraction", 0.0)) >= 0.002
                    or float(diff.get("mean_abs_diff", 0.0)) >= 0.5
                ),
                diff,
            )
        if args.ocr_english:
            languages = run(["tesseract", "--list-langs"])
            available = set(
                languages.stdout.decode("utf-8", errors="replace").splitlines()[1:]
                if languages.returncode == 0
                else []
            )
            if "eng" not in available:
                add(
                    "caption_ocr",
                    True,
                    "skipped: tesseract eng language data is not installed",
                    skipped=True,
                )
            else:
                ocr_text = caption_ocr(args.output, args.caption_time)
                add("caption_ocr_nonempty", bool(ocr_text), ocr_text)
                if args.caption_text:
                    add(
                        "caption_ocr_text",
                        normalize_text(args.caption_text) in normalize_text(ocr_text),
                        ocr_text,
                    )

    if args.contact_sheet is not None:
        args.contact_sheet.parent.mkdir(parents=True, exist_ok=True)
        sheet = run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(args.output),
                "-vf",
                "fps=1/2,scale=270:-1,tile=4x2",
                "-frames:v",
                "1",
                str(args.contact_sheet),
            ]
        )
        add("contact_sheet", sheet.returncode == 0, str(args.contact_sheet))

    passed = all(item["pass"] for item in checks)
    report = {"checks": checks, "pass": passed}
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        for item in checks:
            status = "PASS" if item["pass"] else "FAIL"
            print(f"{status} {item['name']}: {item.get('value', '')}")
        print(f"RESULT {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
