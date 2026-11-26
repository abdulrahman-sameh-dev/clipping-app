"""Versioned schema additions for durable recovery state.

The recovery tables live in the existing ``jobs.db``.  Each migration is
applied under ``BEGIN IMMEDIATE`` and recorded in ``schema_migrations`` so a
repeated startup cannot replay it.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1

_MIGRATION_1 = (
    """
    CREATE TABLE IF NOT EXISTS job_stages (
        job_type TEXT NOT NULL,
        job_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        unit_key TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'failed')),
        attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
        input_fingerprint TEXT,
        checkpoint_json TEXT,
        artifact_path TEXT,
        artifact_sha256 TEXT,
        artifact_size INTEGER,
        error_json TEXT,
        lease_owner TEXT,
        lease_expires_at REAL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT,
        PRIMARY KEY (job_type, job_id, stage, unit_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_job_stages_claim
        ON job_stages(status, lease_expires_at, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_job_stages_job
        ON job_stages(job_type, job_id, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS provider_runtime (
        provider TEXT NOT NULL,
        model TEXT NOT NULL DEFAULT '',
        cooldown_until REAL,
        cooldown_reason TEXT,
        cooldown_category TEXT,
        quota_reset_at REAL,
        retry_after_at REAL,
        last_success_at TEXT,
        last_failure_at TEXT,
        last_failure_category TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (provider, model)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_provider_runtime_cooldown
        ON provider_runtime(cooldown_until)
    """,
    """
    CREATE TABLE IF NOT EXISTS idempotency_keys (
        scope TEXT NOT NULL,
        key TEXT NOT NULL,
        job_type TEXT NOT NULL,
        job_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (scope, key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_idempotency_keys_job
        ON idempotency_keys(job_type, job_id)
    """,
)


def migrate_jobs_db(db_path: Path | str) -> int:
    """Apply pending migrations and return the current schema version."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        conn.execute("BEGIN IMMEDIATE")
        try:
            applied = {
                int(row["version"])
                for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
            }
            if SCHEMA_VERSION not in applied:
                for statement in _MIGRATION_1:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, datetime.now(UTC).isoformat()),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return max(applied | {SCHEMA_VERSION})
    finally:
        conn.close()


def schema_version(db_path: Path | str) -> int:
    """Return the highest applied migration version, or 0 when uninitialised."""
    path = Path(db_path)
    if not path.exists():
        return 0
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()
        return int(row["version"]) if row and row["version"] is not None else 0
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()
