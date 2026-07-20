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
    def __init__(self, heads: list[str], *, reject_inline: bool = False) -> None:
        self.heads = heads
        self.reject_inline = reject_inline
        self.reviews: list[dict] = []
        self.comments: list[str] = []
        self.installation_active = True

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
        return None

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
