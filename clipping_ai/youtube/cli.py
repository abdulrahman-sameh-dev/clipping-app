"""`clipping-ai youtube ...` commands."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.settings import Settings
from clipping_ai.youtube import auth
from clipping_ai.youtube.client import YouTubeClient
from clipping_ai.youtube.config import YouTubeSettings
from clipping_ai.youtube.models import Privacy, parse_publish_at
from clipping_ai.youtube.service import UploadStore, YouTubeUploadService


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="youtube_command", required=True)

    sub.add_parser("status", help="Show whether the YouTube module is configured (no network).")
    sub.add_parser("auth", help="Authorise upload access in your browser (runs locally).")

    upload = sub.add_parser("upload", help="Upload one rendered clip to YouTube.")
    upload.add_argument("clip_id")
    upload.add_argument(
        "--privacy",
        choices=[p.value for p in Privacy],
        help="private (default from settings), unlisted or public.",
    )
    upload.add_argument(
        "--publish-at",
        help="Schedule a private upload. ISO 8601 with a UTC offset, e.g. "
        "2026-12-01T18:00:00+02:00.",
    )
    upload.add_argument("--title")
    upload.add_argument("--description")
    upload.add_argument(
        "--confirm-public", action="store_true", help="Required for --privacy public."
    )
    upload.add_argument("--force", action="store_true", help="Upload even if already uploaded.")
    upload.add_argument(
        "--dry-run", action="store_true", help="Validate and print the request. No network."
    )
    upload.add_argument("--json", action="store_true")


def _status(settings: Settings, yt: YouTubeSettings) -> dict[str, Any]:
    token = auth.load_token(yt.resolved_token_path(settings.data_dir))
    return {
        "enabled": yt.enabled,
        "client_configured": bool(yt.client_id and yt.client_secret),
        "authorised": bool(token and token.get("refresh_token")),
        "default_privacy": yt.default_privacy.value,
        "made_for_kids": yt.made_for_kids,
    }


def run_youtube(
    args: argparse.Namespace,
    *,
    settings: Settings,
    clip_service_factory: Callable[[], Any],
    youtube_settings: YouTubeSettings | None = None,
    uploader: Any | None = None,
) -> int:
    yt = youtube_settings or YouTubeSettings()
    command = args.youtube_command

    if command == "status":
        for key, value in _status(settings, yt).items():
            print(f"{key}: {value}")
        return 0

    yt.require_enabled()
    token_path = yt.resolved_token_path(settings.data_dir)

    if command == "auth":
        auth.authorize(yt, token_path)
        print("Authorised. The token is stored locally and is not part of the repository.")
        return 0

    if command == "upload":
        publish_at = parse_publish_at(args.publish_at) if args.publish_at else None
        service = YouTubeUploadService(
            yt,
            clip_service_factory(),
            uploader,
            UploadStore(yt.uploads_path(settings.data_dir)),
        )
        request = service.build_request(
            args.clip_id,
            privacy=Privacy(args.privacy) if args.privacy else None,
            publish_at=publish_at,
            title=args.title,
            description=args.description,
            confirm_public=args.confirm_public,
        )
        if args.dry_run:
            print(json.dumps(request.api_body(), indent=2, ensure_ascii=False))
            return 0
        if uploader is None:
            yt.credentials()  # fail early with a clear message
            service.uploader = YouTubeClient(lambda: auth.get_access_token(yt, token_path))
        record = service.upload(request, force=args.force)
        if args.json:
            print(json.dumps(record, indent=2))
        else:
            print(f"Uploaded as {record['privacy']}: {record['studio_url']}")
            if record["publish_at"]:
                print(f"Scheduled to go public at {record['publish_at']} (UTC).")
            print("The clip's manual 'published' flag was not changed.")
        return 0

    raise AIError(
        ErrorCategory.INVALID_REQUEST, f"Unknown youtube command: {command}", retryable=False
    )
