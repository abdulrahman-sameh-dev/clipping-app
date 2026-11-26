"""Upload rendered clips to YouTube. Never changes a clip's manual `published` flag."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from clipping_ai.clips.models import ClipJob
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.youtube.config import YouTubeSettings
from clipping_ai.youtube.models import Privacy, UploadRequest


class ClipSource(Protocol):
    def get(self, clip_id: str) -> ClipJob: ...
    def display_title(self, clip: ClipJob) -> str: ...
    def output_path(self, clip_id: str) -> Path: ...


class VideoUploader(Protocol):
    def upload_video(self, path: Path, request: UploadRequest) -> dict[str, Any]: ...


class UploadStore:
    """Small JSON record of what was uploaded. Holds no credentials."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, clip_id: str) -> dict[str, Any] | None:
        return self.load().get(clip_id)

    def put(self, clip_id: str, record: dict[str, Any]) -> None:
        data = self.load()
        data[clip_id] = record
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)


class YouTubeUploadService:
    def __init__(
        self,
        settings: YouTubeSettings,
        clips: ClipSource,
        uploader: VideoUploader | None,
        store: UploadStore,
    ) -> None:
        self.settings = settings
        self.clips = clips
        self.uploader = uploader
        self.store = store

    def build_request(
        self,
        clip_id: str,
        *,
        privacy: Privacy | None = None,
        publish_at: datetime | None = None,
        title: str | None = None,
        description: str | None = None,
        confirm_public: bool = False,
        now: datetime | None = None,
    ) -> UploadRequest:
        clip = self.clips.get(clip_id)
        if clip.deleted_at is not None:
            raise AIError(
                ErrorCategory.NOT_FOUND,
                "This clip is in the trash. Restore it before uploading.",
                provider="youtube",
                retryable=False,
            )
        hashtags = list(clip.hashtags)
        body = description if description is not None else (clip.description or "")
        if description is None and hashtags:
            tag_line = " ".join(f"#{tag.lstrip('#')}" for tag in hashtags)
            body = f"{body}\n\n{tag_line}".strip()
        request = UploadRequest(
            clip_id=clip_id,
            title=title or self.clips.display_title(clip),
            description=body,
            tags=hashtags,
            privacy=privacy or self.settings.default_privacy,
            publish_at=publish_at,
            made_for_kids=self.settings.made_for_kids,
            confirm_public=confirm_public,
        )
        request.validate_policy(now=now)
        return request

    def upload(
        self, request: UploadRequest, *, force: bool = False, channel_id: str | None = None
    ) -> dict[str, Any]:
        """Upload the rendered file. Refuses repeats unless forced."""
        self.settings.require_enabled()
        if self.uploader is None:
            raise AIError(
                ErrorCategory.INTERNAL_ERROR,
                "No uploader configured.",
                provider="youtube",
                retryable=False,
            )
        existing = self.store.get(request.clip_id)
        if existing and not force:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                f"This clip was already uploaded (video {existing.get('video_id')}). "
                "Use --force to upload it again.",
                provider="youtube",
                retryable=False,
            )
        path = self.clips.output_path(request.clip_id)
        resource = self.uploader.upload_video(path, request)
        video_id = resource.get("id")
        if not video_id:
            raise AIError(
                ErrorCategory.INVALID_RESPONSE,
                "YouTube did not return a video id.",
                provider="youtube",
                retryable=False,
            )
        record = {
            "video_id": video_id,
            "privacy": request.privacy.value,
            "publish_at": request.api_body()["status"].get("publishAt"),
            "uploaded_at": datetime.now(UTC).isoformat(),
            "studio_url": f"https://studio.youtube.com/video/{video_id}/edit",
        }
        if channel_id:
            record["channel_id"] = channel_id
        self.store.put(request.clip_id, record)
        return record
