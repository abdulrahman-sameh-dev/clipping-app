"""Upload request models and validation for the YouTube module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from clipping_ai.errors import AIError, ErrorCategory

MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_BYTES = 5000
MAX_TAGS_CHARS = 500
MIN_SCHEDULE_LEAD = timedelta(minutes=15)
MAX_SCHEDULE_LEAD = timedelta(days=365)


class Privacy(StrEnum):
    PRIVATE = "private"
    UNLISTED = "unlisted"
    PUBLIC = "public"


def _invalid(message: str) -> AIError:
    return AIError(ErrorCategory.INVALID_REQUEST, message, provider="youtube", retryable=False)


def parse_publish_at(value: str) -> datetime:
    """Parse an ISO 8601 timestamp. A UTC offset is required so the time is never guessed."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _invalid(
            "publish time must be ISO 8601 with a UTC offset, e.g. 2026-12-01T18:00:00+02:00"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _invalid("publish time needs a UTC offset (for example +02:00 or Z)")
    return parsed.astimezone(UTC)


class UploadRequest(BaseModel):
    """A fully explicit upload. Privacy has no implicit value at this layer."""

    model_config = ConfigDict(extra="forbid")

    clip_id: str
    title: str = Field(min_length=1)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    privacy: Privacy
    publish_at: datetime | None = None
    made_for_kids: bool = False
    confirm_public: bool = False

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be empty")
        if len(value) > MAX_TITLE_CHARS:
            raise ValueError(f"title is longer than {MAX_TITLE_CHARS} characters")
        if "<" in value or ">" in value:
            raise ValueError("title must not contain < or >")
        return value

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        if "<" in value or ">" in value:
            raise ValueError("description must not contain < or >")
        if len(value.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
            raise ValueError(f"description is longer than {MAX_DESCRIPTION_BYTES} bytes")
        return value

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for tag in value:
            tag = tag.strip().lstrip("#").strip()
            if tag and "<" not in tag and ">" not in tag and tag not in cleaned:
                cleaned.append(tag)
        if sum(len(tag) + 1 for tag in cleaned) > MAX_TAGS_CHARS:
            raise ValueError(f"tags are longer than {MAX_TAGS_CHARS} characters in total")
        return cleaned

    @field_validator("publish_at")
    @classmethod
    def _publish_at_tz(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("publish_at must include a UTC offset")
        return value.astimezone(UTC)

    def validate_policy(self, *, now: datetime | None = None) -> None:
        """Enforce the safety rules. Raises AIError before anything touches the network."""
        current = now or datetime.now(UTC)
        if self.publish_at is not None:
            if self.privacy != Privacy.PRIVATE:
                raise _invalid(
                    "Scheduling needs privacy=private. YouTube makes the video public at the "
                    "scheduled time; an unlisted or public video cannot be scheduled."
                )
            if self.publish_at < current + MIN_SCHEDULE_LEAD:
                raise _invalid(
                    "publish time must be at least "
                    f"{int(MIN_SCHEDULE_LEAD.total_seconds() // 60)} minutes in the future"
                )
            if self.publish_at > current + MAX_SCHEDULE_LEAD:
                raise _invalid("publish time is more than a year away; check the date")
        if self.privacy == Privacy.PUBLIC and not self.confirm_public:
            raise _invalid(
                "Public uploads need explicit confirmation (--confirm-public). "
                "Use private, or schedule a private upload, if unsure."
            )

    def api_body(self) -> dict[str, Any]:
        """The videos.insert body. privacyStatus is always set explicitly."""
        status: dict[str, Any] = {
            "privacyStatus": self.privacy.value,
            "selfDeclaredMadeForKids": self.made_for_kids,
        }
        if self.publish_at is not None:
            status["publishAt"] = self.publish_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        snippet: dict[str, Any] = {"title": self.title, "description": self.description}
        if self.tags:
            snippet["tags"] = self.tags
        return {"snippet": snippet, "status": status}
