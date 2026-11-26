"""Command-line interface for local administration and direct AI calls."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from clipping_ai import __version__
from clipping_ai.analysis.service import AnalysisService
from clipping_ai.clips.models import ClipFit, ClipFormat, ClipJob, ClipQuality, ClipStatus
from clipping_ai.clips.service import ClipService
from clipping_ai.errors import AIError
from clipping_ai.highlights.models import ContentType
from clipping_ai.manager import ProviderManager
from clipping_ai.recovery import DataDirectoryLock, DataDirectoryLockedError
from clipping_ai.schemas import ALL_TASKS, TEXT_TASKS
from clipping_ai.settings import get_settings
from clipping_ai.video.models import JobStatus, VideoJob
from clipping_ai.video.service import VideoService


def _dump(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _read_json_arg(value: str | None, path: str | None) -> dict[str, Any]:
    if value and path:
        raise SystemExit("Use either --payload or --payload-file, not both.")
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    if value:
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise SystemExit("--payload must be a JSON object.")
        return parsed
    return {}


def _read_text(text: str | None, file_path: str | None) -> str:
    if text and file_path:
        raise SystemExit("Use either --text or --file, not both.")
    if file_path:
        return Path(file_path).read_text(encoding="utf-8")
    if text:
        return text
    raise SystemExit("Provide --text or --file.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clipping-ai", description=__doc__)
    parser.add_argument("--version", action="version", version=f"clipping-ai {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the local FastAPI service.")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true")

    status = sub.add_parser("status", help="Show provider and policy readiness.")
    status.add_argument("--health", action="store_true", help="Also call each provider.")
    status.add_argument("--json", action="store_true", help="Emit raw JSON.")

    providers = sub.add_parser("providers", help="List configured providers.")
    providers.add_argument("--json", action="store_true")

    models = sub.add_parser("models", help="List registered models and eligibility.")
    models.add_argument("--task", choices=ALL_TASKS, default=None)
    models.add_argument("--json", action="store_true")

    usage = sub.add_parser("usage", help="Show local usage statistics.")
    usage.add_argument("--days", type=int, default=7)
    usage.add_argument("--recent", type=int, default=20)
    usage.add_argument("--json", action="store_true")

    analyze = sub.add_parser("analyze", help="Run a structured text task.")
    analyze.add_argument("task", choices=TEXT_TASKS)
    analyze.add_argument("--text")
    analyze.add_argument("--file", dest="file_path")
    analyze.add_argument("--instructions")
    analyze.add_argument("--context")
    analyze.add_argument("--target-language")
    analyze.add_argument("--provider")
    analyze.add_argument("--model")
    analyze.add_argument("--payload", help="JSON object merged into the task payload.")
    analyze.add_argument("--payload-file")
    analyze.add_argument("--json", action="store_true")

    transcribe = sub.add_parser("transcribe", help="Transcribe a local audio file.")
    transcribe.add_argument("audio_path")
    transcribe.add_argument("--provider")
    transcribe.add_argument("--model")
    transcribe.add_argument("--language")
    transcribe.add_argument("--no-word-timestamps", action="store_true")
    transcribe.add_argument("--prompt")
    transcribe.add_argument("--json", action="store_true")

    test = sub.add_parser("test-connection", help="Test one provider connection.")
    test.add_argument("provider")
    test.add_argument("--model")
    test.add_argument("--json", action="store_true")

    verify = sub.add_parser("verify-free", help="Verify free pricing metadata when supported.")
    verify.add_argument("model_id")
    verify.add_argument("--json", action="store_true")

    inspect = sub.add_parser("inspect", help="Inspect a local video without creating a job.")
    inspect.add_argument("path")
    inspect.add_argument("--json", action="store_true")

    prepare = sub.add_parser("prepare", help="Ingest a local file or URL and prepare audio.")
    prepare.add_argument("source")
    prepare.add_argument("--json", action="store_true")

    job = sub.add_parser("job", help="Show one video preparation job.")
    job.add_argument("job_id")
    job.add_argument("--json", action="store_true")

    jobs = sub.add_parser("jobs", help="List video preparation jobs.")
    jobs.add_argument("--limit", type=int, default=20)
    jobs.add_argument("--status", choices=[status.value for status in JobStatus], default=None)
    jobs.add_argument("--json", action="store_true")

    retry = sub.add_parser("retry", help="Requeue a failed or cancelled video job.")
    retry.add_argument("job_id")
    retry.add_argument("--json", action="store_true")

    cancel = sub.add_parser("cancel", help="Cancel a queued video job.")
    cancel.add_argument("job_id")
    cancel.add_argument("--json", action="store_true")

    transcribe_job = sub.add_parser(
        "transcribe-job", help="Transcribe a prepared video job using the free provider policy."
    )
    transcribe_job.add_argument("job_id")
    transcribe_job.add_argument("--provider")
    transcribe_job.add_argument("--model")
    transcribe_job.add_argument("--language")
    transcribe_job.add_argument("--no-word-timestamps", action="store_true")
    transcribe_job.add_argument("--prompt")
    transcribe_job.add_argument("--refresh", action="store_true")
    transcribe_job.add_argument("--json", action="store_true")

    transcript = sub.add_parser("transcript", help="Show a stored job transcript.")
    transcript.add_argument("job_id")
    transcript.add_argument("--json", action="store_true")

    highlights = sub.add_parser("highlights", help="Detect highlights for a prepared video job.")
    highlights.add_argument("job_id")
    highlights.add_argument(
        "--content-type",
        choices=[item.value for item in ContentType],
        default=ContentType.AUTO.value,
    )
    highlights.add_argument("--provider")
    highlights.add_argument("--model")
    highlights.add_argument("--no-llm", action="store_true")
    highlights.add_argument("--refresh", action="store_true")
    highlights.add_argument("--multi-pass", action="store_true")
    highlights.add_argument("--window-seconds", type=float)
    highlights.add_argument("--overlap-seconds", type=float)
    highlights.add_argument("--max-highlights", type=int)
    highlights.add_argument("--json", action="store_true")

    analyze_job = sub.add_parser(
        "analyze-job", help="Transcribe and detect highlights for a prepared video job."
    )
    analyze_job.add_argument("job_id")
    analyze_job.add_argument("--provider")
    analyze_job.add_argument("--model")
    analyze_job.add_argument("--language")
    analyze_job.add_argument(
        "--content-type",
        choices=[item.value for item in ContentType],
        default=ContentType.AUTO.value,
    )
    analyze_job.add_argument("--no-llm", action="store_true")
    analyze_job.add_argument("--refresh", action="store_true")
    analyze_job.add_argument("--multi-pass", action="store_true")
    analyze_job.add_argument("--window-seconds", type=float)
    analyze_job.add_argument("--overlap-seconds", type=float)
    analyze_job.add_argument("--max-highlights", type=int)
    analyze_job.add_argument("--json", action="store_true")

    analysis_config = sub.add_parser(
        "analysis-config", help="Show transcription and highlight configuration."
    )
    analysis_config.add_argument("--json", action="store_true")

    render_clips = sub.add_parser(
        "render-clips", help="Render highlight clips from the original source video."
    )
    render_clips.add_argument("job_id")
    render_clips.add_argument(
        "--format",
        dest="formats",
        action="append",
        choices=[item.value for item in ClipFormat],
        default=None,
        help="Output format; repeat for multiple formats.",
    )
    render_clips.add_argument(
        "--quality",
        choices=[item.value for item in ClipQuality],
        default=None,
    )
    render_clips.add_argument(
        "--fit",
        choices=[item.value for item in ClipFit],
        default=None,
    )
    render_clips.add_argument("--captions", action="store_true")
    render_clips.add_argument("--caption-language")
    render_clips.add_argument(
        "--highlight-index",
        dest="highlight_indices",
        action="append",
        type=int,
        default=None,
        help="Render only this highlight index; repeatable.",
    )
    render_clips.add_argument("--limit", type=int, default=None)
    render_clips.add_argument(
        "--queue",
        action="store_true",
        help="Only create jobs; do not render them synchronously.",
    )
    render_clips.add_argument("--json", action="store_true")

    clip_job = sub.add_parser("clip-job", help="Show one clip rendering job.")
    clip_job.add_argument("clip_id")
    clip_job.add_argument("--json", action="store_true")

    clip_jobs = sub.add_parser("clip-jobs", help="List clip rendering jobs.")
    clip_jobs.add_argument("job_id", nargs="?", default=None)
    clip_jobs.add_argument("--limit", type=int, default=20)
    clip_jobs.add_argument(
        "--status", choices=[status.value for status in ClipStatus], default=None
    )
    clip_jobs.add_argument("--json", action="store_true")

    render_clip = sub.add_parser("render-clip", help="Render one queued clip job.")
    render_clip.add_argument("clip_id")
    render_clip.add_argument("--json", action="store_true")

    retry_clip = sub.add_parser("retry-clip", help="Requeue a failed or cancelled clip job.")
    retry_clip.add_argument("clip_id")
    retry_clip.add_argument("--json", action="store_true")

    cancel_clip = sub.add_parser("cancel-clip", help="Cancel a queued clip job.")
    cancel_clip.add_argument("clip_id")
    cancel_clip.add_argument("--json", action="store_true")

    clips_config = sub.add_parser("clips-config", help="Show clip rendering configuration.")
    clips_config.add_argument("--json", action="store_true")

    youtube = sub.add_parser(
        "youtube",
        help="Optional YouTube upload and scheduling (disabled by default).",
    )
    from clipping_ai.youtube.cli import add_arguments as add_youtube_arguments

    add_youtube_arguments(youtube)

    pipeline = sub.add_parser(
        "pipeline",
        help="Read-only Google sign-ins for the Pipeline page (private Sheet, Analytics).",
    )
    from clipping_ai.pipeline.cli import add_arguments as add_pipeline_arguments

    add_pipeline_arguments(pipeline)

    return parser


def _manager() -> ProviderManager:
    return ProviderManager.from_config()


def _video_service() -> VideoService:
    return VideoService(get_settings())


def _analysis_service(*, with_manager: bool) -> AnalysisService:
    return AnalysisService(
        get_settings(),
        manager=_manager() if with_manager else None,
    )


def _clip_service() -> ClipService:
    return ClipService(get_settings(), manager=_manager())


def _print_status(data: dict[str, Any]) -> None:
    print(f"Clipping AI {data['version']} — ready={data['ready']} local_only={data['local_only']}")
    print(
        "Models: "
        f"{data['models']['free_approved']} free-approved / "
        f"{data['models']['enabled']} enabled / {data['models']['total']} total"
    )
    for provider in data["providers"]:
        state = "available" if provider["configured"] else "unavailable"
        print(
            f"- {provider['id']}: {state}; key={'yes' if provider['key_present'] else 'no'}; "
            f"models={provider['models']}; {provider['reason']}"
        )
    print("Policy: " + json.dumps(data["policy"], sort_keys=True))
    print("Usage: " + json.dumps(data["usage"], sort_keys=True))


def _print_providers(data: list[dict[str, Any]]) -> None:
    for provider in data:
        print(
            f"{provider['id']}: configured={provider['configured']} "
            f"enabled={provider['enabled']} models={provider['models']} "
            f"key_present={provider['key_present']} ({provider['reason']})"
        )


def _print_models(data: list[dict[str, Any]]) -> None:
    for model in data:
        tasks = ", ".join(model.get("tasks", []))
        print(
            f"{model['id']} [{model['provider']}] free={model['free']} "
            f"status={model['status']} verified={model['free_verified']} tasks={tasks}"
        )


def _print_video_job(job: VideoJob) -> None:
    print(
        f"{job.id} status={job.status.value} stage={job.stage.value} "
        f"progress={job.progress:.0%} source={job.source_type.value}"
    )
    print(f"  source: {job.source_value}")
    if job.input_path:
        print(f"  input: {job.input_path}")
    if job.metadata:
        metadata = job.metadata
        print(
            f"  media: {metadata.duration_seconds:.2f}s, {metadata.size_bytes} bytes, "
            f"video={metadata.has_video}, audio={metadata.has_audio}"
        )
        for video_stream in metadata.video_streams:
            print(
                f"  video stream {video_stream.index}: {video_stream.codec_name or '?'} "
                f"{video_stream.width or '?'}x{video_stream.height or '?'} "
                f"fps={video_stream.fps or '?'}"
            )
        for audio_stream in metadata.audio_streams:
            print(
                f"  audio stream {audio_stream.index}: {audio_stream.codec_name or '?'} "
                f"{audio_stream.sample_rate or '?'} Hz channels={audio_stream.channels or '?'}"
            )
    if job.audio_path:
        print(f"  audio: {job.audio_path}")
    if job.audio_chunks:
        print(f"  chunks: {len(job.audio_chunks)}")
    if job.transcript:
        print(
            f"  transcript: {len(job.transcript.segments)} segments, "
            f"language={job.transcript.language or '?'}, "
            f"provider={job.transcript_provider or '?'}/{job.transcript_model or '?'}"
        )
    if job.transcript_error:
        print(f"  transcript error: {job.transcript_error}")
    if job.highlight_result:
        print(
            f"  highlights: {len(job.highlight_result.highlights)} "
            f"({job.highlight_result.method}, {job.highlight_result.content_type.value})"
        )
        for highlight in job.highlight_result.highlights:
            print(
                f"    {highlight.start_time:.2f}-{highlight.end_time:.2f}s "
                f"score={highlight.confidence:.2f} {highlight.title}"
            )
    if job.highlight_error:
        print(f"  highlight error: {job.highlight_error}")
    if job.error:
        print(f"  error: {job.error.get('message') or job.error}")


def _print_video_jobs(jobs: list[VideoJob]) -> None:
    if not jobs:
        print("No video jobs.")
        return
    for job in jobs:
        print(
            f"{job.id} {job.status.value:10s} {job.stage.value:10s} "
            f"{job.progress:>4.0%} {job.source_type.value:6s} {job.source_value}"
        )


def _print_clip_job(job: ClipJob) -> None:
    print(
        f"{job.id} status={job.status.value} stage={job.stage.value} "
        f"progress={job.progress:.0%} source_job={job.source_job_id}"
    )
    print(
        f"  range: {job.start_time:.2f}-{job.end_time:.2f}s "
        f"({job.duration:.2f}s) format={job.format.value} "
        f"{job.width}x{job.height} fps={job.fps:g} quality={job.quality.value} "
        f"fit={job.fit.value}"
    )
    print(
        f"  captions: {job.captions} language={job.caption_language or '?'} "
        f"file={job.caption_path or '?'}"
    )
    if job.output_path:
        print(f"  output: {job.output_path} ({job.size_bytes or 0} bytes)")
    if job.error:
        print(f"  error: {job.error.get('message') or job.error}")


def _print_clip_jobs(jobs: list[ClipJob]) -> None:
    if not jobs:
        print("No clip jobs.")
        return
    for job in jobs:
        print(
            f"{job.id} {job.status.value:10s} {job.stage.value:10s} "
            f"{job.progress:>4.0%} {job.format.value:9s} "
            f"{job.start_time:.2f}-{job.end_time:.2f}s source={job.source_job_id}"
        )


def _print_usage(data: dict[str, Any]) -> None:
    print("Totals: " + json.dumps(data["totals"], sort_keys=True))
    if not data["summary"]:
        print("No usage recorded yet.")
        return
    print("By provider/model/task:")
    for row in data["summary"]:
        print(
            f"- {row['provider']}/{row['model'] or '*'} task={row['task'] or '*'} "
            f"requests={row['requests']} failures={row['failures']} "
            f"audio_seconds={row['audio_seconds']}"
        )


_MUTATING_COMMANDS = {
    "prepare",
    "retry",
    "cancel",
    "render-clips",
    "render-clip",
    "retry-clip",
    "cancel-clip",
    "transcribe-job",
    "highlights",
    "analyze-job",
    "analyze",
    "transcribe",
    "test-connection",
    "verify-free",
}


def run(args: argparse.Namespace) -> int:
    if args.command in _MUTATING_COMMANDS:
        with DataDirectoryLock(get_settings().data_dir):
            return _run(args)
    return _run(args)


def _run(args: argparse.Namespace) -> int:
    settings = get_settings()

    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "clipping_ai.api.app:app",
            host=args.host or settings.host,
            port=args.port or settings.port,
            reload=args.reload,
            log_level=settings.log_level.lower(),
        )
        return 0

    if args.command == "pipeline":
        from clipping_ai.pipeline.cli import run_pipeline

        return run_pipeline(args, settings=settings)

    if args.command == "youtube":
        from clipping_ai.youtube.cli import run_youtube

        return run_youtube(args, settings=settings, clip_service_factory=_clip_service)

    if args.command in {"inspect", "prepare", "job", "jobs", "retry", "cancel"}:
        service = _video_service()
        if args.command == "inspect":
            metadata = service.inspect(args.path)
            if args.json:
                _dump(metadata.model_dump(mode="json"))
            else:
                print(json.dumps(metadata.model_dump(mode="json"), indent=2, ensure_ascii=False))
            return 0
        if args.command == "prepare":
            if args.source.lower().startswith(("http://", "https://")):
                job = service.prepare_url(args.source)
            else:
                job = service.prepare_local(args.source)
            if args.json:
                _dump(job.to_dict())
            else:
                _print_video_job(job)
            return 0
        if args.command == "job":
            job = service.get(args.job_id)
            if args.json:
                _dump(job.to_dict())
            else:
                _print_video_job(job)
            return 0
        if args.command == "jobs":
            status = JobStatus(args.status) if args.status else None
            jobs = service.list(limit=args.limit, status=status)
            if args.json:
                _dump({"jobs": [job.to_dict() for job in jobs]})
            else:
                _print_video_jobs(jobs)
            return 0
        if args.command == "retry":
            job = service.retry(args.job_id)
            if args.json:
                _dump(job.to_dict())
            else:
                _print_video_job(job)
            return 0
        if args.command == "cancel":
            job = service.cancel(args.job_id)
            if args.json:
                _dump(job.to_dict())
            else:
                _print_video_job(job)
            return 0

    if args.command in {
        "render-clips",
        "clip-job",
        "clip-jobs",
        "render-clip",
        "retry-clip",
        "cancel-clip",
        "clips-config",
    }:
        clip_service = _clip_service()
        if args.command == "clips-config":
            _dump(clip_service.config_dict())
            return 0
        if args.command == "render-clips":
            clip_jobs = clip_service.create_for_job(
                args.job_id,
                formats=args.formats,
                quality=args.quality,
                fit=args.fit,
                captions=args.captions,
                caption_language=args.caption_language,
                highlight_indices=args.highlight_indices,
                limit=args.limit,
            )
            if not args.queue:
                clip_jobs = [clip_service.process(job.id) for job in clip_jobs]
            if args.json:
                _dump({"jobs": [job.to_dict() for job in clip_jobs], "count": len(clip_jobs)})
            else:
                _print_clip_jobs(clip_jobs)
            return 0
        if args.command == "clip-job":
            clip_job = clip_service.get(args.clip_id)
            if args.json:
                _dump(clip_job.to_dict())
            else:
                _print_clip_job(clip_job)
            return 0
        if args.command == "clip-jobs":
            clip_status = ClipStatus(args.status) if args.status else None
            clip_jobs = clip_service.list(
                source_job_id=args.job_id, status=clip_status, limit=args.limit
            )
            if args.json:
                _dump({"jobs": [job.to_dict() for job in clip_jobs]})
            else:
                _print_clip_jobs(clip_jobs)
            return 0
        if args.command == "render-clip":
            clip_job = clip_service.process(args.clip_id)
            if args.json:
                _dump(clip_job.to_dict())
            else:
                _print_clip_job(clip_job)
            return 0
        if args.command == "retry-clip":
            clip_job = clip_service.retry(args.clip_id)
            if args.json:
                _dump(clip_job.to_dict())
            else:
                _print_clip_job(clip_job)
            return 0
        if args.command == "cancel-clip":
            clip_job = clip_service.cancel(args.clip_id)
            if args.json:
                _dump(clip_job.to_dict())
            else:
                _print_clip_job(clip_job)
            return 0

    if args.command in {
        "transcribe-job",
        "transcript",
        "highlights",
        "analyze-job",
        "analysis-config",
    }:
        with_manager = args.command in {"transcribe-job", "highlights", "analyze-job"}
        analysis_service = _analysis_service(with_manager=with_manager)
        if args.command == "analysis-config":
            _dump(analysis_service.config_dict())
            return 0
        if args.command == "transcript":
            transcript_result = analysis_service.get_transcript(args.job_id)
            if args.json:
                _dump(
                    {
                        "transcript": (
                            transcript_result.model_dump(mode="json")
                            if transcript_result
                            else None
                        ),
                    }
                )
            elif transcript_result is None:
                print("No transcript stored for this job.")
            else:
                print(transcript_result.text)
            return 0
        if args.command == "transcribe-job":
            job = analysis_service.transcribe(
                args.job_id,
                provider=args.provider,
                model=args.model,
                language=args.language,
                word_timestamps=False if args.no_word_timestamps else None,
                prompt=args.prompt,
                refresh=args.refresh,
            )
        elif args.command == "highlights":
            job = analysis_service.highlights(
                args.job_id,
                content_type=ContentType(args.content_type),
                provider=args.provider,
                model=args.model,
                use_llm=False if args.no_llm else None,
                refresh=args.refresh,
                multi_pass=True if args.multi_pass else None,
                window_seconds=args.window_seconds,
                overlap_seconds=args.overlap_seconds,
                max_highlights=args.max_highlights,
            )
        else:
            job = analysis_service.analyze(
                args.job_id,
                provider=args.provider,
                model=args.model,
                language=args.language,
                content_type=ContentType(args.content_type),
                use_llm=False if args.no_llm else None,
                refresh=args.refresh,
                multi_pass=True if args.multi_pass else None,
                window_seconds=args.window_seconds,
                overlap_seconds=args.overlap_seconds,
                max_highlights=args.max_highlights,
            )
        if args.json:
            _dump(job.to_dict())
        else:
            _print_video_job(job)
        return 0

    manager = _manager()

    if args.command == "status":
        status_data = manager.status(include_health=args.health)
        if args.json:
            _dump(status_data)
        else:
            _print_status(status_data)
        return 0

    if args.command == "providers":
        provider_rows = manager.list_providers()
        if args.json:
            _dump({"providers": provider_rows})
        else:
            _print_providers(provider_rows)
        return 0

    if args.command == "models":
        model_rows = manager.list_models()
        if args.task:
            model_rows = [item for item in model_rows if args.task in item.get("tasks", [])]
        if args.json:
            _dump({"models": model_rows})
        else:
            _print_models(model_rows)
        return 0

    if args.command == "usage":
        tracker = manager.usage
        data = {
            "totals": tracker.totals(),
            "summary": tracker.summary(args.days),
            "daily": tracker.daily_counts(args.days),
            "recent": tracker.recent(args.recent),
        }
        if args.json:
            _dump(data)
        else:
            _print_usage(data)
        return 0

    if args.command == "analyze":
        text = _read_text(args.text, args.file_path)
        payload = _read_json_arg(args.payload, args.payload_file)
        payload.setdefault("text", text)
        result = manager.analyze(
            args.task,
            text=text,
            instructions=args.instructions,
            context=args.context,
            target_language=args.target_language,
            provider=args.provider,
            model=args.model,
            payload=payload,
        )
        _dump(result)
        return 0

    if args.command == "transcribe":
        result = manager.transcribe(
            args.audio_path,
            provider=args.provider,
            model=args.model,
            language=args.language,
            word_timestamps=not args.no_word_timestamps,
            prompt=args.prompt,
        )
        _dump(result)
        return 0

    if args.command == "test-connection":
        _dump(manager.test_connection(args.provider, args.model))
        return 0

    if args.command == "verify-free":
        _dump(manager.verify_free(args.model_id))
        return 0

    raise SystemExit(f"Unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except AIError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        if exc.details:
            print(json.dumps(exc.to_dict(), indent=2, default=str), file=sys.stderr)
        return 1
    except DataDirectoryLockedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
