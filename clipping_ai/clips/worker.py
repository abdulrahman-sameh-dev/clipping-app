"""Single local worker for queued clip rendering jobs."""

from __future__ import annotations

import logging
import threading

from clipping_ai.clips.models import ClipJob
from clipping_ai.clips.service import ClipService
from clipping_ai.errors import AIError

logger = logging.getLogger(__name__)


class ClipWorker:
    def __init__(self, service: ClipService, *, poll_seconds: float | None = None) -> None:
        self.service = service
        self.poll_seconds = poll_seconds or service.config.worker_poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self.service.recover_interrupted()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="clipping-clip-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def run_once(self) -> ClipJob | None:
        job = self.service.store.claim_next_queued()
        if job is None:
            return None
        try:
            return self.service.process(job.id)
        except AIError as exc:
            logger.warning("Clip job %s failed: %s", job.id, exc.message)
            return self.service.store.get(job.id)
        except Exception:
            logger.exception("Unexpected clip worker failure for job %s", job.id)
            return self.service.store.get(job.id)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.run_once()
            except Exception:
                logger.exception("Clip worker loop failed")
                job = None
            if job is None:
                self._stop.wait(self.poll_seconds)
