from __future__ import annotations

import logging
import threading
import time
import httpx
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
                self.store.recover_stale(
                    before=datetime.now(UTC) - self.stale_after
                )
                task = self.store.claim_next()
                if task is None:
                    self._stop.wait(self.poll_seconds)
                    continue
                try:
                    self.service.process(task)
                except Exception as exc:
                    log.exception("review task failed: %s", task.id)
                    if task.attempt_count < 3 and self._retryable_error(exc):
                        self._stop.wait(min(2 ** task.attempt_count, 30))
                        self.store.requeue(task.id)
                    else:
                        self.service.notify_failure(task)
        finally:
            self.alive = False

    @staticmethod
    def _retryable_error(exc: Exception) -> bool:
        # The service logs the original exception; the worker only retries
        # transient network/rate-limit failures in the current process.
        if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code == 429 or exc.response.status_code >= 500
        return False
