"""`clipping-ai pipeline ...` commands: read-only Google sign-ins for the Pipeline page."""

from __future__ import annotations

import argparse

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.pipeline import google
from clipping_ai.settings import Settings
from clipping_ai.youtube.config import YouTubeSettings


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="pipeline_command", required=True)
    sub.add_parser("status", help="Show which read-only Google sign-ins are done (no network).")
    auth = sub.add_parser(
        "auth",
        help="Sign in with Google, read-only, in your browser (runs on this computer).",
    )
    auth.add_argument(
        "kind",
        choices=sorted(google.SCOPES),
        help="sheets: read your private Google Sheet. analytics: read YouTube Analytics and the "
        "channel's scheduled videos. upload: upload and schedule clips. The web UI (Pipeline "
        "page, Connections) does the same without the terminal.",
    )
    auth.add_argument("--channel", default=google.DEFAULT_CHANNEL, help="Channel id.")


def run_pipeline(
    args: argparse.Namespace,
    *,
    settings: Settings,
    youtube_settings: YouTubeSettings | None = None,
) -> int:
    yt = youtube_settings or YouTubeSettings()
    command = args.pipeline_command
    if command == "status":
        for kind in sorted(google.SCOPES):
            done = google.is_authorised(settings.data_dir, kind, google.DEFAULT_CHANNEL, yt)
            print(f"{kind}: {'signed in' if done else 'not signed in'}")
        return 0
    if command == "auth":
        google.authorize(args.kind, yt, settings.data_dir, channel_id=args.channel)
        print(
            f"Signed in ({args.kind}, read-only). The token is stored locally and is not part "
            "of the repository."
        )
        return 0
    raise AIError(
        ErrorCategory.INVALID_REQUEST, f"Unknown pipeline command: {command}", retryable=False
    )
