"""Local OAuth 2.0 (installed app, loopback + PKCE) for the YouTube upload scope.

The flow runs on the operator's own machine. Tokens are stored in a 0600 file under the
data directory (git-ignored). Tokens and secrets are never logged or printed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.youtube.config import UPLOAD_SCOPE, YouTubeSettings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALLBACK_TIMEOUT_SECONDS = 300.0
_EXPIRY_MARGIN_SECONDS = 60


def _auth_error(message: str, *, retryable: bool = False) -> AIError:
    return AIError(
        ErrorCategory.AUTHENTICATION_FAILED, message, provider="youtube", retryable=retryable
    )


def make_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_auth_url(*, client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": UPLOAD_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


class _CallbackServer:
    """One-shot loopback HTTP server that captures the OAuth redirect."""

    def __init__(self) -> None:
        self.result: dict[str, str] = {}
        self.done = threading.Event()
        result, done = self.result, self.done

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                query = parse_qs(urlparse(self.path).query)
                for key in ("code", "state", "error"):
                    if key in query:
                        result[key] = query[key][0]
                body = b"You can close this tab and return to the terminal."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                done.set()

            def log_message(self, *args: Any) -> None:  # silence request logging
                return

        # Loopback only, random free port.
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def wait(self, timeout: float) -> dict[str, str]:
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        try:
            self.done.wait(timeout)
        finally:
            self.server.shutdown()
            self.server.server_close()
        return self.result


def _token_record(payload: dict[str, Any], previous_refresh: str | None = None) -> dict[str, Any]:
    access = payload.get("access_token")
    if not access:
        raise _auth_error("Google did not return an access token.")
    refresh = payload.get("refresh_token") or previous_refresh
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": time.time() + float(payload.get("expires_in", 3600)),
        "scope": payload.get("scope", UPLOAD_SCOPE),
    }


def _post_token(http: httpx.Client, data: dict[str, str]) -> dict[str, Any]:
    try:
        response = http.post(TOKEN_URL, data=data, timeout=30.0)
    except httpx.HTTPError as exc:
        raise AIError(
            ErrorCategory.NETWORK_TIMEOUT,
            "Could not reach Google's token endpoint.",
            provider="youtube",
            retryable=True,
            cause=exc,
        ) from exc
    if response.status_code != 200:
        # Report Google's error code only; the body can echo request details.
        try:
            code = str(response.json().get("error", response.status_code))
        except ValueError:
            code = str(response.status_code)
        raise _auth_error(f"Google rejected the token request ({code}).")
    return response.json()


def save_token(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(record, handle)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def load_token(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def authorize(
    settings: YouTubeSettings,
    token_path: Path,
    *,
    http: httpx.Client | None = None,
    open_url: Callable[[str], Any] | None = None,
    announce: Callable[[str], None] = print,
    timeout: float = CALLBACK_TIMEOUT_SECONDS,
) -> None:
    """Run the browser consent flow on this machine and store the resulting token."""
    client_id, client_secret = settings.credentials()
    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(24)
    server = _CallbackServer()
    url = build_auth_url(
        client_id=client_id, redirect_uri=server.redirect_uri, state=state, challenge=challenge
    )
    announce("Open this URL to authorise upload access (the upload scope only):")
    announce(url)
    (open_url or webbrowser.open)(url)
    result = server.wait(timeout)
    if result.get("error"):
        raise _auth_error(f"Authorisation was not granted ({result['error']}).")
    if not result.get("code"):
        raise _auth_error("Timed out waiting for the browser authorisation.", retryable=True)
    if not secrets.compare_digest(result.get("state", ""), state):
        raise _auth_error("The authorisation response did not match this request.")
    owns = http is None
    client = http or httpx.Client()
    try:
        payload = _post_token(
            client,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "code": result["code"],
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": server.redirect_uri,
            },
        )
    finally:
        if owns:
            client.close()
    save_token(token_path, _token_record(payload))


def get_access_token(
    settings: YouTubeSettings,
    token_path: Path,
    *,
    http: httpx.Client | None = None,
    now: Callable[[], float] = time.time,
) -> str:
    """Return a valid access token, refreshing it when needed."""
    record = load_token(token_path)
    if record is None or not record.get("access_token"):
        raise _auth_error("YouTube is not authorised yet. Run: clipping-ai youtube auth")
    if float(record.get("expires_at", 0)) - now() > _EXPIRY_MARGIN_SECONDS:
        return str(record["access_token"])
    refresh = record.get("refresh_token")
    if not refresh:
        raise _auth_error("The saved token cannot be refreshed. Run: clipping-ai youtube auth")
    client_id, client_secret = settings.credentials()
    owns = http is None
    client = http or httpx.Client()
    try:
        payload = _post_token(
            client,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": str(refresh),
                "grant_type": "refresh_token",
            },
        )
    finally:
        if owns:
            client.close()
    updated = _token_record(payload, previous_refresh=str(refresh))
    if "scope" not in payload and record.get("scope"):
        updated["scope"] = record["scope"]  # keep the scopes that were granted
    save_token(token_path, updated)
    return str(updated["access_token"])
