"""Safe execution of declared skill tools with permission and timeout checks."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


class SkillExecutor:
    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    @staticmethod
    def _check_permissions(record: Any, tool: Any) -> None:
        permissions = record.manifest.permissions
        if tool.requires_network and not permissions.network:
            raise AIError(
                ErrorCategory.POLICY_VIOLATION,
                f"Tool '{tool.name}' requires network permission that the skill does not declare.",
                retryable=False,
            )
        if tool.requires_filesystem and not permissions.filesystem:
            raise AIError(
                ErrorCategory.POLICY_VIOLATION,
                f"Tool '{tool.name}' requires filesystem permission that the skill does not declare.",
                retryable=False,
            )
        if tool.requires_subprocess and not permissions.subprocess:
            raise AIError(
                ErrorCategory.POLICY_VIOLATION,
                f"Tool '{tool.name}' requires subprocess permission that the skill does not declare.",
                retryable=False,
            )

    def execute(self, skill_id: str, context: dict[str, Any]) -> dict[str, Any]:
        record = self.registry.require(skill_id)
        if not record.enabled:
            raise AIError(
                ErrorCategory.POLICY_VIOLATION,
                f"Skill '{skill_id}' is disabled.",
                retryable=False,
            )
        if record.source == "external" and not record.reviewed:
            raise AIError(
                ErrorCategory.POLICY_VIOLATION,
                "External skills cannot run before review.",
                retryable=False,
            )
        results: list[dict[str, Any]] = []
        for name in record.manifest.tools:
            tool = self.registry.tools.get(name)
            if tool is None:
                raise AIError(
                    ErrorCategory.POLICY_VIOLATION,
                    f"Skill requests unknown tool '{name}'.",
                    retryable=False,
                )
            self._check_permissions(record, tool)
            with ThreadPoolExecutor(
                max_workers=record.manifest.max_concurrency,
                thread_name_prefix=f"skill-{record.manifest.id}",
            ) as pool:
                future = pool.submit(tool.handler, dict(context))
                try:
                    results.append(future.result(timeout=record.manifest.timeout_seconds))
                except FutureTimeout as exc:
                    logger.warning("Skill %s tool %s timed out", skill_id, name)
                    raise AIError(
                        ErrorCategory.NETWORK_TIMEOUT,
                        f"Skill tool '{name}' timed out after "
                        f"{record.manifest.timeout_seconds:.0f}s.",
                        retryable=False,
                    ) from exc
                except AIError:
                    raise
                except Exception as exc:  # noqa: BLE001 - tool failure is contained
                    raise AIError(
                        ErrorCategory.INTERNAL_ERROR,
                        f"Skill tool '{name}' failed: {exc}",
                        retryable=False,
                    ) from exc
        logger.info("Executed skill %s with %d tool(s)", skill_id, len(results))
        return {
            "skill": skill_id,
            "enabled": True,
            "reviewed": record.reviewed,
            "results": results,
        }
