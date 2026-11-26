"""Facebook settings. Secrets are only ever read from the environment/.env."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from clipping_ai.errors import AIError, ErrorCategory

# Permissions asked at sign-in. Meta's docs list these for publishing a Reel to a Page.
# They are only approved for the app's own admins while the Meta app is in Development mode.
SCOPES = ("pages_show_list", "pages_manage_posts", "pages_read_engagement", "publish_video")
DAILY_LIMIT = 30  # API-published Reels per Page per rolling 24 hours (Meta's stated limit)
MIN_SCHEDULE_MINUTES = 10  # Meta: scheduled Reels must be at least 10 minutes ahead
MAX_SCHEDULE_DAYS = 29  # and at most 29 days ahead (third-party source; Meta may differ)


class FacebookSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    app_id: str | None = Field(default=None, validation_alias="FACEBOOK_APP_ID")
    app_secret: SecretStr | None = Field(default=None, validation_alias="FACEBOOK_APP_SECRET")
    api_version: str = Field(default="v21.0", validation_alias="FACEBOOK_API_VERSION")

    def credentials(self) -> tuple[str, str]:
        app_id = (self.app_id or "").strip()
        secret = self.app_secret.get_secret_value().strip() if self.app_secret else ""
        if not app_id or not secret:
            raise AIError(
                ErrorCategory.MISSING_API_KEY,
                "FACEBOOK_APP_ID and FACEBOOK_APP_SECRET must be set in .env. "
                "See FACEBOOK_SETUP.md.",
                provider="facebook",
                retryable=False,
            )
        return app_id, secret

    @property
    def configured(self) -> bool:
        try:
            self.credentials()
        except AIError:
            return False
        return True

    def graph_url(self) -> str:
        return f"https://graph.facebook.com/{self.api_version}"

    def token_path(self, data_dir: Path) -> Path:
        return (data_dir / "facebook" / "token.json").resolve()

    def posts_path(self, data_dir: Path) -> Path:
        return (data_dir / "facebook" / "posts.json").resolve()
