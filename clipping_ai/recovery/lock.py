"""Cross-process ownership of a shared local data directory."""

from __future__ import annotations

import fcntl
import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import IO


class DataDirectoryLockedError(RuntimeError):
    """Raised when another Clipping Studio process owns the data directory."""


class DataDirectoryLock:
    """Non-blocking advisory lock held for the lifetime of an application.

    ``flock`` is advisory and released automatically when the process exits,
    so a crash cannot leave a permanently stale lock.  The lock file is kept
    inside the shared data directory and contains only an ownership label.
    """

    def __init__(self, data_dir: Path | str, *, filename: str = ".clipping-ai.lock") -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.path = self.data_dir / filename
        self.owner_id = f"{socket.gethostname()}:{os.getpid()}:{os.urandom(8).hex()}"
        self._handle: IO[str] | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            owner = _read_owner(self.path)
            handle.close()
            detail = f" (owner: {owner})" if owner else ""
            raise DataDirectoryLockedError(
                "Another Clipping Studio process is already using "
                f"'{self.data_dir}'{detail}. Stop it before starting a second instance."
            ) from exc
        except OSError as exc:
            handle.close()
            raise DataDirectoryLockedError(
                f"Could not acquire the data-directory lock '{self.path}': {exc}"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(
            json.dumps(
                {
                    "owner": self.owner_id,
                    "acquired_at": datetime.now(UTC).isoformat(),
                },
                sort_keys=True,
            )
            + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> DataDirectoryLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


def _read_owner(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    owner = payload.get("owner")
    return owner if isinstance(owner, str) else None
