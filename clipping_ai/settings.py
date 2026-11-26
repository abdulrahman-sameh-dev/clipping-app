"""Centralised settings. Secrets are only ever read from the environment/.env."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_KNOWN_SECRETS = {
    "GEMINI_API_KEY": "gemini_api_key",
    "GROQ_API_KEY": "groq_api_key",
    "OPENROUTER_API_KEY": "openrouter_api_key",
    "NVIDIA_API_KEY": "nvidia_api_key",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # Provider credentials. Optional: a missing key disables only that provider.
    gemini_api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    groq_api_key: SecretStr | None = Field(default=None, validation_alias="GROQ_API_KEY")
    openrouter_api_key: SecretStr | None = Field(default=None, validation_alias="OPENROUTER_API_KEY")
    nvidia_api_key: SecretStr | None = Field(default=None, validation_alias="NVIDIA_API_KEY")

    # Locations.
    config_dir: Path = Field(default=Path("config"), validation_alias="CLIPPING_AI_CONFIG_DIR")
    data_dir: Path = Field(default=Path("data"), validation_alias="CLIPPING_AI_DATA_DIR")

    # Local API. Loopback only by default.
    host: str = Field(default="127.0.0.1", validation_alias="CLIPPING_AI_HOST")
    port: int = Field(default=8765, validation_alias="CLIPPING_AI_PORT")

    # Defaults used when a provider config does not override them.
    timeout_seconds: float = Field(default=120.0, validation_alias="CLIPPING_AI_TIMEOUT_SECONDS")
    max_retries: int = Field(default=2, validation_alias="CLIPPING_AI_MAX_RETRIES")
    require_verified_free: bool | None = Field(
        default=None, validation_alias="CLIPPING_AI_REQUIRE_VERIFIED_FREE"
    )

    # Optional local whisper.cpp settings (overridden by config/providers.json).
    whisper_cpp_binary: str | None = Field(
        default=None, validation_alias="CLIPPING_AI_WHISPER_CPP_BINARY"
    )
    whisper_cpp_model_path: str | None = Field(
        default=None, validation_alias="CLIPPING_AI_WHISPER_CPP_MODEL_PATH"
    )

    log_level: str = Field(default="INFO", validation_alias="CLIPPING_AI_LOG_LEVEL")

    @field_validator("config_dir", "data_dir", mode="after")
    @classmethod
    def _resolve_storage_path(cls, value: Path) -> Path:
        """Resolve storage roots once so later cwd changes cannot move them."""
        return value.expanduser().resolve()

    def get_secret(self, env_var: str) -> str | None:
        """Return a secret value, or None. Never log the returned value."""
        attr = _KNOWN_SECRETS.get(env_var)
        if attr:
            value = getattr(self, attr, None)
            if isinstance(value, SecretStr):
                text = value.get_secret_value().strip()
                return text or None
        raw = os.environ.get(env_var, "").strip()
        return raw or None

    def has_secret(self, env_var: str) -> bool:
        return self.get_secret(env_var) is not None

    @property
    def usage_db_path(self) -> Path:
        return self.data_dir / "usage.db"

    @property
    def jobs_db_path(self) -> Path:
        return self.data_dir / "jobs.db"

    def ensure_dirs(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
