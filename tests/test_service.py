import httpx
import pytest

from app.context import ChangedFile
from app.github import InlinePositionError
from app.models import Finding, ReviewResult, Severity, TaskStatus, TriggerMode
from app.service import ReviewService
from app.storage import TaskStore
from tests.test_storage import draft


class Engine:
    def __init__(self, result: ReviewResult, after=None) -> None:
        self.result = result
        self.calls = 0
        self.after = after

    def review(self, prompt: str) -> ReviewResult:
        self.calls += 1
        if self.after:
            self.after()
        return self.result


class GitHub:
    def __init__(self, heads: list[str], *, reject_inline: bool = False, policy_text=None) -> None:
        self.heads = heads
        self.reject_inline = reject_inline
        self.reviews: list[dict] = []
        self.comments: list[str] = []
        self.installation_active = True
        self.policy_text = policy_text

    def get_pr(self, *args):
        head = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
        return {
            "title": "PR",
            "body": "body",
            "state": "open",
            "head": {"sha": head, "ref": "feature"},
            "base": {"ref": "main"},
        }

    def get_repository_file(self, *args, **kwargs):
        return self.policy_text

    def list_pr_files(self, *args):
        return [
            {
                "filename": "src/api.py",
                "patch": "@@ -1 +1 @@\n-old\n+risky()",
                "status": "modified",
                "additions": 1,
                "deletions": 1,
            }
        ]

    def find_review_by_marker(self, *args):
        return None

    def find_comment_by_marker(self, *args):
        return None

    def create_review(self, *args, **kwargs):
        if self.reject_inline:
            raise InlinePositionError()
        self.reviews.append(kwargs)
        return 91

    def create_comment(self, *args):
        self.comments.append(args[-1])
        return 92

    def add_reaction(self, *args):
        pass


def finding_result() -> ReviewResult:
    return ReviewResult(
        summary="risk",
        comments=[Finding(path="src/api.py", line=1, severity=Severity.HIGH, body="bug")],
    )


def task(store: TaskStore):
    current = store.accept("d1", "pull_request", draft()).task
    store.mark_running(current.id)
    return store.get(current.id)


def test_stale_before_model_stops_without_review(tmp_path) -> None:
    store = TaskStore(tmp_path / "db.sqlite3")
    engine = Engine(finding_result())
    github = GitHub(["new-sha"])
    service = ReviewService(store, github, engine)

    current = task(store)
    service.process(current)

    assert engine.calls == 0
    assert github.reviews == []
    assert store.get(current.id).status is TaskStatus.SUPERSEDED


def test_sha_change_during_model_call_never_writes_or_falls_back(tmp_path) -> None:
    store = TaskStore(tmp_path / "db.sqlite3")
    github = GitHub(["abc123", "new-sha"])
    service = ReviewService(store, github, Engine(finding_result()))
    current = task(store)

    service.process(current)

    assert github.reviews == []
    assert github.comments == []
    assert store.get(current.id).status is TaskStatus.SUPERSEDED


def test_invalid_inline_positions_fall_back_only_when_sha_is_current(tmp_path) -> None:
    store = TaskStore(tmp_path / "db.sqlite3")
    github = GitHub(["abc123"], reject_inline=True)
    service = ReviewService(store, github, Engine(finding_result()))
    current = task(store)

    service.process(current)

    assert github.comments and "bug" in github.comments[0]
    assert store.get(current.id).status is TaskStatus.COMPLETED


def test_installation_revocation_during_model_stops_publish(tmp_path) -> None:
    store = TaskStore(tmp_path / "db.sqlite3")
    github = GitHub(["abc123"])
    engine = Engine(finding_result(), after=lambda: store.record_installation("off", "installation", 7, active=False))
    service = ReviewService(store, github, engine)
    current = task(store)

    service.process(current)

    assert github.reviews == []
    assert github.comments == []
    assert store.get(current.id).status is TaskStatus.FAILED


def test_service_does_not_notify_manual_failure_before_retry_decision(tmp_path) -> None:
    store = TaskStore(tmp_path / "db.sqlite3")
    github = GitHub(["abc123"])

    class FailingEngine:
        def review(self, prompt: str):
            raise httpx.ConnectError("temporary")

    service = ReviewService(store, github, FailingEngine())
    current = task(store).model_copy(
        update={"trigger_mode": TriggerMode.MANUAL, "user_initiated": True}
    )

    with pytest.raises(httpx.ConnectError):
        service.process(current)

    assert github.comments == []


def test_auto_review_false_still_runs_opened_event(tmp_path) -> None:
    store = TaskStore(tmp_path / "db.sqlite3")
    github = GitHub(["abc123"], policy_text="auto_review: false")
    service = ReviewService(store, github, Engine(finding_result()))
    current = task(store).model_copy(update={"trigger": "opened"})

    service.process(current)

    assert service.engine.calls == 1


def test_auto_review_false_skips_synchronize_event(tmp_path) -> None:
    store = TaskStore(tmp_path / "db.sqlite3")
    github = GitHub(["abc123"], policy_text="auto_review: false")
    engine = Engine(finding_result())
    service = ReviewService(store, github, engine)
    current = task(store).model_copy(update={"trigger": "synchronize"})

    service.process(current)

    assert engine.calls == 0
    assert store.get(current.id).status is TaskStatus.SUPERSEDED


def test_manual_review_ignores_automatic_branch_policy(tmp_path) -> None:
    store = TaskStore(tmp_path / "db.sqlite3")
    github = GitHub(["abc123"], policy_text="include_branches: [release]")
    service = ReviewService(store, github, Engine(finding_result()))
    current = task(store).model_copy(
        update={"trigger_mode": TriggerMode.MANUAL, "user_initiated": True}
    )

    service.process(current)

    assert service.engine.calls == 1


def test_bot_trigger_is_filtered_using_event_actor(tmp_path) -> None:
    store = TaskStore(tmp_path / "db.sqlite3")
    github = GitHub(["abc123"], policy_text="review_bot_prs: false")
    engine = Engine(finding_result())
    service = ReviewService(store, github, engine)
    current = task(store).model_copy(update={"trigger_actor_type": "Bot"})

    service.process(current)

    assert engine.calls == 0
    assert store.get(current.id).status is TaskStatus.SUPERSEDED
