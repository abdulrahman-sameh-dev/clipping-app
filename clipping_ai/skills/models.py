"""Skill manifest and permission models."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


class SkillPermissions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    network: bool = False
    filesystem: bool = False
    subprocess: bool = False


class SkillManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(default="1.0.0", max_length=32)
    description: str = Field(default="", max_length=2000)
    instructions: str = Field(default="", max_length=20000)
    tools: list[str] = Field(default_factory=list, max_length=20)
    permissions: SkillPermissions = Field(default_factory=SkillPermissions)
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    max_concurrency: int = Field(default=1, ge=1, le=4)
    enabled_by_default: bool = False
    category: Literal["caption", "visual", "analysis", "metadata", "other"] = "other"

    @field_validator("id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        if not SKILL_ID_RE.fullmatch(value):
            raise ValueError("skill id must use lowercase letters, digits, '.', '_' or '-'")
        return value

    @field_validator("tools")
    @classmethod
    def _safe_tools(cls, value: list[str]) -> list[str]:
        for tool in value:
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,79}", tool):
                raise ValueError(f"invalid tool name: {tool}")
        return list(dict.fromkeys(value))


class SkillRecord(BaseModel):
    manifest: SkillManifest
    source: Literal["builtin", "external"]
    enabled: bool = False
    reviewed: bool = True
    reviewed_at: str | None = None

    def public_dict(self) -> dict[str, object]:
        return {
            **self.manifest.model_dump(mode="json"),
            "source": self.source,
            "enabled": self.enabled,
            "reviewed": self.reviewed,
            "reviewed_at": self.reviewed_at,
        }
