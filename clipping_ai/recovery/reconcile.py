"""Startup reconciliation for interrupted work and stale local artefacts.

Reconciliation is deliberately conservative: it reports problems, releases
expired leases, and removes only old temporary files.  Final video, audio, and
clip artefacts are never deleted here.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clipping_ai.clips.recovery import CLIP_JOB_TYPE, STAGE_OUTPUT
from clipping_ai.recovery.stages import StageRecord, StageStatus, StageStore

logger = logging.getLogger(__name__)

_TEMP_SUFFIXES = {".part", ".tmp"}


def _is_temp_file(path: Path) -> bool:
    return path.suffix in _TEMP_SUFFIXES or ".part." in path.name


@dataclass(frozen=True)
class ReconciliationReport:
    expired_leases: int = 0
    inconsistent_stages: tuple[str, ...] = ()
    missing_artifacts: tuple[str, ...] = ()
    invalid_artifacts: tuple[str, ...] = ()
    orphaned_temp_files: tuple[str, ...] = ()
    removed_temp_files: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "expired_leases": self.expired_leases,
            "inconsistent_stages": list(self.inconsistent_stages),
            "missing_artifacts": list(self.missing_artifacts),
            "invalid_artifacts": list(self.invalid_artifacts),
            "orphaned_temp_files": list(self.orphaned_temp_files),
            "removed_temp_files": list(self.removed_temp_files),
        }

    @property
    def issues_found(self) -> bool:
        return any(
            (
                self.expired_leases,
                self.inconsistent_stages,
                self.missing_artifacts,
                self.invalid_artifacts,
                self.orphaned_temp_files,
            )
        )


def _stage_label(record: StageRecord) -> str:
    suffix = f":{record.unit_key}" if record.unit_key else ""
    return f"{record.job_type}:{record.job_id}:{record.stage}{suffix}"


def _file_hash(path: Path) -> str | None:
    import hashlib

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


@dataclass
class StartupReconciler:
    data_dir: Path | str
    stage_store: StageStore
    temp_max_age_seconds: float = 24 * 60 * 60
    remove_stale_temp: bool = True
    verify_artifact_hashes: bool = True
    _data_dir: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._data_dir = Path(self.data_dir).expanduser().resolve()
        self._data_dir.mkdir(parents=True, exist_ok=True)

    def run(self, *, now: float | None = None) -> ReconciliationReport:
        current = time.time() if now is None else now
        expired = self.stage_store.expire_leases(now=current)
        inconsistent = tuple(
            _stage_label(record) for record in self.stage_store.list_inconsistent()
        )

        missing: list[str] = []
        invalid: list[str] = []
        for record in self.stage_store.list_stages(status=StageStatus.DONE):
            if record.artifact_path is None:
                continue
            label = _stage_label(record)
            if not record.artifact_path.is_file():
                missing.append(label)
                continue
            if (
                self.verify_artifact_hashes
                and record.artifact_sha256
                and _file_hash(record.artifact_path) != record.artifact_sha256
            ):
                invalid.append(label)

        preserved = self._preserved_paths()
        orphaned, removed = self._temp_files(current, preserved)
        report = ReconciliationReport(
            expired_leases=expired,
            inconsistent_stages=inconsistent,
            missing_artifacts=tuple(missing),
            invalid_artifacts=tuple(invalid),
            orphaned_temp_files=tuple(str(path) for path in orphaned),
            removed_temp_files=tuple(str(path) for path in removed),
        )
        if report.issues_found:
            logger.warning(
                "Startup reconciliation found %d expired lease(s), %d inconsistent "
                "stage(s), %d missing artefact(s), %d invalid artefact(s), "
                "%d orphaned temporary file(s)",
                report.expired_leases,
                len(report.inconsistent_stages),
                len(report.missing_artifacts),
                len(report.invalid_artifacts),
                len(report.orphaned_temp_files),
            )
        return report

    def _preserved_paths(self) -> set[Path]:
        preserved: set[Path] = set()
        for record in self.stage_store.list_stages(job_type=CLIP_JOB_TYPE):
            if record.stage != STAGE_OUTPUT:
                continue
            checkpoint = record.checkpoint
            if not isinstance(checkpoint, dict):
                continue
            staged = checkpoint.get("staging_path")
            expected = checkpoint.get("staged_sha256")
            if not isinstance(staged, str) or not isinstance(expected, str):
                continue
            path = Path(staged)
            if path.is_file() and _file_hash(path) == expected:
                preserved.add(path.resolve())
        return preserved

    def _temp_files(
        self, now: float, preserved: set[Path] | None = None
    ) -> tuple[list[Path], list[Path]]:
        orphaned: list[Path] = []
        removed: list[Path] = []
        keep = preserved or set()
        for root, _dirs, files in os.walk(self._data_dir):
            for name in files:
                path = Path(root) / name
                if not _is_temp_file(path):
                    continue
                if path.resolve() in keep:
                    continue
                try:
                    age = now - path.stat().st_mtime
                except OSError:
                    continue
                if age < self.temp_max_age_seconds:
                    continue
                orphaned.append(path)
                if not self.remove_stale_temp:
                    continue
                if not path.resolve().is_relative_to(self._data_dir):
                    continue
                try:
                    path.unlink()
                except OSError:
                    continue
                removed.append(path)
        return orphaned, removed
