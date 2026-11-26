"""Skill discovery with explicit review/enable state for external manifests."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.skills.models import SkillManifest, SkillRecord
from clipping_ai.skills.tools import ToolRegistry

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SkillRegistry:
    def __init__(
        self,
        config_dir: Path | str,
        *,
        tools: ToolRegistry | None = None,
        builtin_dir: Path | None = None,
    ) -> None:
        self.config_dir = Path(config_dir)
        self.builtin_dir = builtin_dir or Path(__file__).parent / "manifests"
        self.external_dir = self.config_dir / "skills"
        self.tools = tools or ToolRegistry()
        self._records: dict[str, SkillRecord] = {}
        self._state_path = self.config_dir / "local" / "skills.json"
        self.reload()

    def _state(self) -> dict[str, dict[str, Any]]:
        if not self._state_path.exists():
            return {}
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        skills = payload.get("skills") if isinstance(payload, dict) else None
        return skills if isinstance(skills, dict) else {}

    def _persist(self) -> None:
        state = {
            skill_id: {
                "enabled": record.enabled,
                "reviewed": record.reviewed,
                "reviewed_at": record.reviewed_at,
            }
            for skill_id, record in self._records.items()
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps({"skills": state}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _load_dir(
        self,
        directory: Path,
        *,
        source: Literal["builtin", "external"],
        state: dict[str, dict[str, Any]],
    ) -> None:
        if not directory.exists():
            return
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                manifest = SkillManifest.model_validate(payload)
            except (OSError, json.JSONDecodeError, ValidationError) as exc:
                logger.warning("Skipping invalid skill manifest %s: %s", path.name, exc)
                continue
            overrides = state.get(manifest.id, {})
            enabled = bool(overrides.get("enabled", manifest.enabled_by_default and source == "builtin"))
            reviewed = bool(overrides.get("reviewed", source == "builtin"))
            if source == "external":
                enabled = enabled and reviewed
            self._records[manifest.id] = SkillRecord(
                manifest=manifest,
                source=source,
                enabled=enabled,
                reviewed=reviewed,
                reviewed_at=overrides.get("reviewed_at"),
            )

    def reload(self) -> None:
        state = self._state()
        self._records.clear()
        self._load_dir(self.builtin_dir, source="builtin", state=state)
        self._load_dir(self.external_dir, source="external", state=state)

    def list(self) -> list[dict[str, Any]]:
        return [record.public_dict() for record in sorted(self._records.values(), key=lambda r: r.manifest.id)]

    def get(self, skill_id: str) -> SkillRecord | None:
        return self._records.get(skill_id)

    def require(self, skill_id: str) -> SkillRecord:
        record = self.get(skill_id)
        if record is None:
            raise AIError(
                ErrorCategory.NOT_FOUND,
                f"Skill '{skill_id}' was not found.",
                retryable=False,
            )
        return record

    def review(self, skill_id: str, *, reviewed: bool = True) -> SkillRecord:
        record = self.require(skill_id)
        record.reviewed = reviewed
        record.reviewed_at = _now() if reviewed else None
        if record.source == "external" and not reviewed:
            record.enabled = False
        self._persist()
        return record

    def set_enabled(self, skill_id: str, enabled: bool) -> SkillRecord:
        record = self.require(skill_id)
        if enabled and record.source == "external" and not record.reviewed:
            raise AIError(
                ErrorCategory.POLICY_VIOLATION,
                "External skills must be reviewed before they can be enabled.",
                retryable=False,
            )
        if enabled:
            unknown = [name for name in record.manifest.tools if self.tools.get(name) is None]
            if unknown:
                raise AIError(
                    ErrorCategory.POLICY_VIOLATION,
                    f"Skill requests unknown tools: {', '.join(unknown)}.",
                    retryable=False,
                )
        record.enabled = enabled
        self._persist()
        return record
