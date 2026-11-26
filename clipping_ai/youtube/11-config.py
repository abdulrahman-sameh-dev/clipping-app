"""YouTube module settings. Secrets are only ever read from the environment/.env."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.youtube.models import Privacy

UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


class YouTubeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    enabled: bool = Field(default=False, validation_alias="CLIPPING_AI_YOUTUBE_ENABLED")
    client_id: str | None = Field(default=None, validation_alias="YOUTUBE_CLIENT_ID")
    client_secret: SecretStr | None = Field(default=None, validation_alias="YOUTUBE_CLIENT_SECRET")
    default_privacy: Privacy = Field(
        default=Privacy.PRIVATE, validation_alias="CLIPPING_AI_YOUTUBE_DEFAULT_PRIVACY"
    )
    made_for_kids: bool = Field(default=False, validation_alias="CLIPPING_AI_YOUTUBE_MADE_FOR_KIDS")
    token_path: Path | None = Field(default=None, validation_alias="CLIPPING_AI_YOUTUBE_TOKEN_PATH")

    def credentials(self) -> tuple[str, str]:
        """Return (client_id, client_secret) or raise a typed error. Never log the secret."""
        client_id = (self.client_id or "").strip()
        secret = self.client_secret.get_secret_value().strip() if self.client_secret else ""
        if not client_id or not secret:
            raise AIError(
                ErrorCategory.MISSING_API_KEY,
                "YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET must be set in .env. "
                "See YOUTUBE_SETUP.md.",
                provider="youtube",
                retryable=False,
            )
        return client_id, secret

    def require_enabled(self) -> None:
        if not self.enabled:
            raise AIError(
                ErrorCategory.POLICY_VIOLATION,
                "The YouTube module is disabled. Set CLIPPING_AI_YOUTUBE_ENABLED=true in .env "
                "to use it. Publishing is manual by default.",
                provider="youtube",
                retryable=False,
            )

    def resolved_token_path(self, data_dir: Path) -> Path:
        return (self.token_path or data_dir / "youtube" / "token.json").expanduser().resolve()

    def uploads_path(self, data_dir: Path) -> Path:
        return (data_dir / "youtube" / "uploads.json").resolve()
