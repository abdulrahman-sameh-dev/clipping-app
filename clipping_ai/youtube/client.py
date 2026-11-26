"""Minimal YouTube Data API v3 client: resumable videos.insert upload over httpx."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.youtube.models import UploadRequest

UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CHUNK_SIZE = 8 * 1024 * 1024  # must be a multiple of 256 KiB
_MAX_CHUNK_ATTEMPTS = 3


def _api_error(response: httpx.Response) -> AIError:
    reason = ""
    try:
        errors = response.json().get("error", {}).get("errors", [])
        reason = str(errors[0].get("reason", "")) if errors else ""
    except (ValueError, AttributeError, IndexError):
        pass
    status = response.status_code
    if status == 401:
        category = ErrorCategory.AUTHENTICATION_FAILED
        message = "YouTube rejected the access token. Run: clipping-ai youtube auth"
    elif status == 403 and reason in {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}:
        category = ErrorCategory.RATE_LIMIT_EXCEEDED
        message = "The YouTube API quota for this project is used up. Try again after it resets."
    elif status == 403:
        category = ErrorCategory.AUTHENTICATION_FAILED
        message = f"YouTube refused the request ({reason or 'forbidden'})."
    elif status == 429:
        category = ErrorCategory.RATE_LIMIT_EXCEEDED
        message = "YouTube is rate limiting this project."
    elif 400 <= status < 500:
        category = ErrorCategory.INVALID_REQUEST
        message = f"YouTube rejected the upload ({reason or status})."
    else:
        category = ErrorCategory.PROVIDER_UNAVAILABLE
        message = f"YouTube returned an error ({status})."
    return AIError(
        category,
        message,
        provider="youtube",
        retryable=category in {ErrorCategory.PROVIDER_UNAVAILABLE},
        details={"http_status": status, "reason": reason},
    )


class YouTubeClient:
    def __init__(
        self,
        token_provider: Callable[[], str],
        *,
        http: httpx.Client | None = None,
        chunk_size: int = CHUNK_SIZE,
    ) -> None:
        self._token_provider = token_provider
        self._http = http or httpx.Client(timeout=httpx.Timeout(60.0, read=120.0))
        self._chunk_size = chunk_size

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token_provider()}"}

    def upload_video(self, path: Path, request: UploadRequest) -> dict[str, Any]:
        """Upload one file with an explicit privacy status. Returns the API video resource."""
        total = path.stat().st_size
        if total <= 0:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "The clip file is empty.",
                provider="youtube",
                retryable=False,
            )
        session_url = self._start_session(total, request)
        offset = 0
        attempts = 0
        with path.open("rb") as handle:
            while True:
                handle.seek(offset)
                chunk = handle.read(self._chunk_size)
                end = offset + len(chunk) - 1
                headers = {
                    **self._headers(),
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{total}",
                    "Content-Type": "video/mp4",
                }
                try:
                    response = self._http.put(session_url, content=chunk, headers=headers)
                except httpx.HTTPError as exc:
                    attempts += 1
                    if attempts >= _MAX_CHUNK_ATTEMPTS:
                        raise AIError(
                            ErrorCategory.NETWORK_TIMEOUT,
                            "The upload to YouTube was interrupted.",
                            provider="youtube",
                            retryable=True,
                            cause=exc,
                        ) from exc
                    resumed = self._resume_offset(session_url, total)
                    if isinstance(resumed, dict):
                        return resumed
                    offset = resumed
                    continue
                if response.status_code in (200, 201):
                    return response.json()
                if response.status_code == 308:
                    attempts = 0
                    offset = self._offset_from_range(response, default=end + 1)
                    continue
                if response.status_code >= 500:
                    attempts += 1
                    if attempts >= _MAX_CHUNK_ATTEMPTS:
                        raise _api_error(response)
                    resumed = self._resume_offset(session_url, total)
                    if isinstance(resumed, dict):
                        return resumed
                    offset = resumed
                    continue
                raise _api_error(response)

    def _start_session(self, total: int, request: UploadRequest) -> str:
        response = self._http.post(
            UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            json=request.api_body(),
            headers={
                **self._headers(),
                "X-Upload-Content-Length": str(total),
                "X-Upload-Content-Type": "video/mp4",
            },
        )
        if response.status_code != 200:
            raise _api_error(response)
        location = response.headers.get("Location")
        if not location:
            raise AIError(
                ErrorCategory.INVALID_RESPONSE,
                "YouTube did not return an upload session URL.",
                provider="youtube",
                retryable=False,
            )
        return location

    def _resume_offset(self, session_url: str, total: int) -> int | dict[str, Any]:
        """Ask YouTube how much it has. A finished upload returns the video resource."""
        response = self._http.put(
            session_url,
            headers={
                **self._headers(),
                "Content-Length": "0",
                "Content-Range": f"bytes */{total}",
            },
        )
        if response.status_code == 308:
            return self._offset_from_range(response, default=0)
        if response.status_code in (200, 201):
            return response.json()
        raise _api_error(response)

    @staticmethod
    def _offset_from_range(response: httpx.Response, *, default: int) -> int:
        value = response.headers.get("Range")
        if not value or "-" not in value:
            return default
        try:
            return int(value.rsplit("-", 1)[1]) + 1
        except ValueError:
            return default
