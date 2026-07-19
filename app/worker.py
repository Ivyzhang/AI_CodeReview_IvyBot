from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta

from app.service import ReviewService
from app.storage import TaskStore


log = logging.getLogger(__name__)


class ReviewWorker:
    def __init__(
        self,
        store: TaskStore,
        service: ReviewService,
        *,
        poll_seconds: float = 0.5,
        stale_after: timedelta = timedelta(minutes=10),
    ) -> None:
        self.store = store
        self.service = service
        self.poll_seconds = poll_seconds
        self.stale_after = stale_after
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.alive = False

    def start(self) -> None:
        self.store.recover_stale(before=datetime.now(UTC) - self.stale_after)
        self._thread = threading.Thread(target=self._run, daemon=True, name="review-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        self.alive = True
        try:
            while not self._stop.is_set():
                task = self.store.claim_next()
                if task is None:
                    self._stop.wait(self.poll_seconds)
                    continue
                try:
                    self.service.process(task)
                except Exception:
                    log.exception("review task failed: %s", task.id)
        finally:
            self.alive = False
