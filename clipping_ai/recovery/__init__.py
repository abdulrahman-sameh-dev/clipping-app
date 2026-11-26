"""Durable recovery primitives for the local clipping pipeline."""

from clipping_ai.recovery.idempotency import (
    IdempotencyRecord,
    IdempotencyStore,
    release_idempotency,
    reserve_idempotency,
    stable_fingerprint,
)
from clipping_ai.recovery.lock import DataDirectoryLock, DataDirectoryLockedError
from clipping_ai.recovery.providers import ProviderRuntime, ProviderRuntimeStore
from clipping_ai.recovery.reconcile import ReconciliationReport, StartupReconciler
from clipping_ai.recovery.schema import SCHEMA_VERSION, migrate_jobs_db, schema_version
from clipping_ai.recovery.stages import (
    StageArtifactError,
    StageError,
    StageOwnershipError,
    StageRecord,
    StageStatus,
    StageStore,
)

__all__ = [
    "DataDirectoryLock",
    "DataDirectoryLockedError",
    "IdempotencyRecord",
    "IdempotencyStore",
    "ProviderRuntime",
    "ProviderRuntimeStore",
    "ReconciliationReport",
    "SCHEMA_VERSION",
    "StageArtifactError",
    "StageError",
    "StageOwnershipError",
    "StageRecord",
    "StageStatus",
    "StageStore",
    "StartupReconciler",
    "migrate_jobs_db",
    "release_idempotency",
    "reserve_idempotency",
    "schema_version",
    "stable_fingerprint",
]
