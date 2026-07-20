from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from app.models import ReviewTask, ReviewTaskDraft, TaskStatus


class AcceptStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    EXISTING = "existing"


@dataclass(frozen=True)
class AcceptResult:
    status: AcceptStatus
    task: ReviewTask


@dataclass(frozen=True)
class PublishedReview:
    task_id: str
    marker: str
    github_review_id: int | None
    github_comment_id: int | None
    mode: str


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).isoformat()


class TaskStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    installation_id INTEGER NOT NULL,
                    repository_id INTEGER NOT NULL,
                    owner TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    pull_number INTEGER NOT NULL,
                    head_sha TEXT NOT NULL,
                    trigger_mode TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    focus TEXT NOT NULL,
                    user_initiated INTEGER NOT NULL,
                    source_comment_id INTEGER,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    task_id TEXT REFERENCES tasks(id),
                    received_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS installations (
                    installation_id INTEGER PRIMARY KEY,
                    active INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS repositories (
                    installation_id INTEGER NOT NULL,
                    repository_id INTEGER NOT NULL,
                    active INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (installation_id, repository_id)
                );
                CREATE TABLE IF NOT EXISTS published_reviews (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                    marker TEXT NOT NULL UNIQUE,
                    github_review_id INTEGER,
                    github_comment_id INTEGER,
                    mode TEXT NOT NULL,
                    published_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_status_created
                ON tasks(status, created_at);
                """
            )

    def accept(
        self, delivery_id: str, event_type: str, draft: ReviewTaskDraft
    ) -> AcceptResult:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            delivered = connection.execute(
                "SELECT task_id FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if delivered:
                return AcceptResult(
                    AcceptStatus.DUPLICATE,
                    self._get_with(connection, delivered["task_id"]),
                )

            existing = connection.execute(
                "SELECT * FROM tasks WHERE idempotency_key = ?",
                (draft.idempotency_key,),
            ).fetchone()
            if existing:
                task = self._task(existing)
                if task.status is TaskStatus.FAILED:
                    connection.execute(
                        """UPDATE tasks
                           SET status = ?, trigger = ?, focus = ?, user_initiated = ?,
                               source_comment_id = ?, created_at = ?,
                               started_at = NULL, finished_at = NULL
                           WHERE id = ?""",
                        (
                            TaskStatus.QUEUED.value,
                            draft.trigger,
                            draft.normalized_focus,
                            int(draft.user_initiated),
                            draft.source_comment_id,
                            _iso(),
                            task.id,
                        ),
                    )
                    task = self._get_with(connection, task.id)
                    status = AcceptStatus.ACCEPTED
                else:
                    status = AcceptStatus.EXISTING
            else:
                task_id = uuid.uuid4().hex
                connection.execute(
                    """
                    INSERT INTO tasks (
                        id, idempotency_key, installation_id, repository_id,
                        owner, repo, pull_number, head_sha, trigger_mode, trigger,
                        focus, user_initiated, source_comment_id, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        draft.idempotency_key,
                        draft.installation_id,
                        draft.repository_id,
                        draft.owner,
                        draft.repo,
                        draft.pull_number,
                        draft.head_sha,
                        draft.trigger_mode.value,
                        draft.trigger,
                        draft.normalized_focus,
                        int(draft.user_initiated),
                        draft.source_comment_id,
                        TaskStatus.QUEUED.value,
                        _iso(),
                    ),
                )
                task = self._get_with(connection, task_id)
                status = AcceptStatus.ACCEPTED

            connection.execute(
                "INSERT INTO deliveries VALUES (?, ?, ?, ?)",
                (delivery_id, event_type, task.id, _iso()),
            )
            return AcceptResult(status, task)

    def get(self, task_id: str) -> ReviewTask:
        with self._connect() as connection:
            return self._get_with(connection, task_id)

    def record_installation(
        self, delivery_id: str, event_type: str, installation_id: int, *, active: bool
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone():
                return False
            connection.execute(
                """INSERT INTO installations VALUES (?, ?, ?)
                   ON CONFLICT(installation_id) DO UPDATE
                   SET active = excluded.active, updated_at = excluded.updated_at""",
                (installation_id, int(active), _iso()),
            )
            connection.execute(
                "INSERT INTO deliveries VALUES (?, ?, NULL, ?)",
                (delivery_id, event_type, _iso()),
            )
            if not active:
                connection.execute(
                    """UPDATE tasks SET status = ?, finished_at = ?
                       WHERE installation_id = ? AND status IN (?, ?)""",
                    (
                        TaskStatus.FAILED.value,
                        _iso(),
                        installation_id,
                        TaskStatus.QUEUED.value,
                        TaskStatus.RUNNING.value,
                    ),
                )
            return True

    def installation_active(self, installation_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT active FROM installations WHERE installation_id = ?",
                (installation_id,),
            ).fetchone()
        return row is None or bool(row["active"])

    def tasks_since(self, installation_id: int, since: datetime) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT COALESCE(SUM(CASE WHEN attempt_count = 0 THEN 1
                       ELSE attempt_count END), 0) AS count FROM tasks
                   WHERE installation_id = ? AND created_at >= ?""",
                (installation_id, _iso(since)),
            ).fetchone()
        return int(row["count"])

    def requeue(self, task_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE tasks SET status = ?, started_at = NULL, finished_at = NULL
                   WHERE id = ? AND status = ?""",
                (TaskStatus.QUEUED.value, task_id, TaskStatus.FAILED.value),
            )

    def status_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def record_repositories(
        self,
        delivery_id: str,
        installation_id: int,
        *,
        added: list[int],
        removed: list[int],
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone():
                return False
            for repository_id, active in [(item, True) for item in added] + [
                (item, False) for item in removed
            ]:
                connection.execute(
                    """INSERT INTO repositories VALUES (?, ?, ?, ?)
                       ON CONFLICT(installation_id, repository_id) DO UPDATE
                       SET active = excluded.active, updated_at = excluded.updated_at""",
                    (installation_id, repository_id, int(active), _iso()),
                )
                if not active:
                    connection.execute(
                        """UPDATE tasks SET status = ?, finished_at = ?
                           WHERE installation_id = ? AND repository_id = ?
                           AND status IN (?, ?)""",
                        (
                            TaskStatus.FAILED.value,
                            _iso(),
                            installation_id,
                            repository_id,
                            TaskStatus.QUEUED.value,
                            TaskStatus.RUNNING.value,
                        ),
                    )
            connection.execute(
                "INSERT INTO deliveries VALUES (?, 'installation_repositories', NULL, ?)",
                (delivery_id, _iso()),
            )
            return True

    def repository_active(self, installation_id: int, repository_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT active FROM repositories
                   WHERE installation_id = ? AND repository_id = ?""",
                (installation_id, repository_id),
            ).fetchone()
        return row is None or bool(row["active"])

    def can_publish(self, task_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT t.status, i.active AS installation_active,
                          r.active AS repository_active
                   FROM tasks t
                   LEFT JOIN installations i ON i.installation_id = t.installation_id
                   LEFT JOIN repositories r ON r.installation_id = t.installation_id
                                             AND r.repository_id = t.repository_id
                   WHERE t.id = ?""",
                (task_id,),
            ).fetchone()
        if row is None or row["status"] != TaskStatus.RUNNING.value:
            return False
        return row["installation_active"] != 0 and row["repository_active"] != 0


    def _get_with(self, connection: sqlite3.Connection, task_id: str) -> ReviewTask:
        row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._task(row)

    @staticmethod
    def _task(row: sqlite3.Row) -> ReviewTask:
        return ReviewTask.model_validate(
            {
                "id": row["id"],
                "installation_id": row["installation_id"],
                "repository_id": row["repository_id"],
                "owner": row["owner"],
                "repo": row["repo"],
                "pull_number": row["pull_number"],
                "head_sha": row["head_sha"],
                "trigger_mode": row["trigger_mode"],
                "trigger": row["trigger"],
                "focus": row["focus"],
                "user_initiated": bool(row["user_initiated"]),
                "source_comment_id": row["source_comment_id"],
                "status": row["status"],
                "attempt_count": row["attempt_count"],
            }
        )

    def mark_running(self, task_id: str, *, now: datetime | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE tasks
                   SET status = ?, started_at = ?, attempt_count = attempt_count + 1
                   WHERE id = ?""",
                (TaskStatus.RUNNING.value, _iso(now), task_id),
            )

    def set_status(self, task_id: str, status: TaskStatus) -> None:
        finished = _iso() if status is not TaskStatus.QUEUED else None
        with self._connect() as connection:
            connection.execute(
                "UPDATE tasks SET status = ?, finished_at = ? WHERE id = ?",
                (status.value, finished, task_id),
            )

    def recover_stale(self, *, before: datetime) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE tasks SET status = ?, started_at = NULL
                   WHERE status = ? AND started_at < ?""",
                (TaskStatus.QUEUED.value, TaskStatus.RUNNING.value, _iso(before)),
            )
            return cursor.rowcount

    def claim_next(self) -> ReviewTask | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id FROM tasks WHERE status = ? ORDER BY created_at LIMIT 1",
                (TaskStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                return None
            self._mark_running_with(connection, row["id"])
            return self._get_with(connection, row["id"])

    @staticmethod
    def _mark_running_with(connection: sqlite3.Connection, task_id: str) -> None:
        connection.execute(
            """UPDATE tasks
               SET status = ?, started_at = ?, attempt_count = attempt_count + 1
               WHERE id = ?""",
            (TaskStatus.RUNNING.value, _iso(), task_id),
        )

    def record_publish(
        self,
        task_id: str,
        marker: str,
        *,
        mode: str,
        github_review_id: int | None = None,
        github_comment_id: int | None = None,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO published_reviews
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    marker,
                    github_review_id,
                    github_comment_id,
                    mode,
                    _iso(),
                ),
            )
            return cursor.rowcount == 1

    def get_publish(self, task_id: str) -> PublishedReview | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM published_reviews WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        return PublishedReview(
            task_id=row["task_id"],
            marker=row["marker"],
            github_review_id=row["github_review_id"],
            github_comment_id=row["github_comment_id"],
            mode=row["mode"],
        )

    def depth(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM tasks WHERE status = ?",
                (TaskStatus.QUEUED.value,),
            ).fetchone()
            return int(row["count"])
