"""Local files for Facebook: the sign-in (mode 0600) and the record of what was posted."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from clipping_ai.youtube import auth


class TokenStore:
    """Holds the long-lived user token and the Pages (each with its own Page token).

    Tokens never leave this file: no route returns them and nothing logs them.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any] | None:
        data = auth.load_token(self.path)
        return data if data and data.get("pages") is not None else None

    def save(self, record: dict[str, Any]) -> None:
        auth.save_token(self.path, record)

    def delete(self) -> bool:
        if self.path.is_file():
            self.path.unlink()
            return True
        return False

    def selected_page(self) -> dict[str, Any] | None:
        data = self.load()
        if not data:
            return None
        wanted = data.get("selected_page_id")
        for page in data.get("pages", []):
            if page.get("id") == wanted:
                return page
        return None

    def select(self, page_id: str) -> dict[str, Any] | None:
        data = self.load()
        if not data:
            return None
        for page in data.get("pages", []):
            if page.get("id") == page_id:
                data["selected_page_id"] = page_id
                self.save(data)
                return page
        return None


class PostStore:
    """What was sent to Facebook. Holds no credentials. Also feeds the 24 hour limit."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, clip_id: str) -> dict[str, Any] | None:
        return self.load().get(clip_id)

    def put(self, clip_id: str, record: dict[str, Any]) -> None:
        with self._lock:
            data = self.load()
            data[clip_id] = record
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)

    def used_last_24h(self, page_id: str, now: datetime | None = None) -> int:
        """Reels sent for this Page in the last 24 hours (by when we sent them)."""
        cutoff = (now or datetime.now(UTC)) - timedelta(hours=24)
        count = 0
        for record in self.load().values():
            if record.get("page_id") != page_id:
                continue
            try:
                sent = datetime.fromisoformat(str(record.get("sent_at")))
            except ValueError:
                continue
            if sent >= cutoff:
                count += 1
        return count
