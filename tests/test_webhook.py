import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.main import create_app
from app.storage import TaskStore


SECRET = "secret"


class GitHub:
    def __init__(self):
        self.get_pr_calls = 0

    def get_pr(self, *args):
        self.get_pr_calls += 1
        return {"head": {"sha": "abc123", "ref": "feature"}, "base": {"ref": "main"}}

    def get_repository_file(self, *args, **kwargs):
        return None


def payload(head_sha: str = "abc123") -> dict:
    return {
        "action": "opened",
        "installation": {"id": 7},
        "repository": {"id": 11, "full_name": "acme/api"},
        "number": 3,
        "sender": {"type": "User"},
        "pull_request": {
            "draft": False,
            "head": {"sha": head_sha, "ref": "feature"},
            "base": {"ref": "main"},
        },
    }


def post(client: TestClient, body: dict, delivery: str = "d1", event: str = "pull_request"):
    raw = json.dumps(body).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        "/hook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature,
            "X-GitHub-Delivery": delivery,
            "X-GitHub-Event": event,
        },
    )


def test_invalid_signature_is_rejected(tmp_path) -> None:
    app = create_app(TaskStore(tmp_path / "db.sqlite3"), GitHub(), SECRET, start_worker=False)
    client = TestClient(app)
    response = client.post("/hook", content=b"{}", headers={"X-Hub-Signature-256": "bad"})
    assert response.status_code == 401


def test_webhook_persists_before_accepting_and_deduplicates(tmp_path) -> None:
    store = TaskStore(tmp_path / "db.sqlite3")
    client = TestClient(create_app(store, GitHub(), SECRET, start_worker=False))

    assert post(client, payload()).json() == {"status": "accepted"}
    assert store.depth() == 1
    assert post(client, payload()).json() == {"status": "duplicate"}
    assert store.depth() == 1


def test_health_readiness_and_metrics_reflect_service_state(tmp_path) -> None:
    client = TestClient(
        create_app(TaskStore(tmp_path / "db.sqlite3"), GitHub(), SECRET, start_worker=False)
    )
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").status_code == 200
    assert "review_queue_depth 0" in client.get("/metrics").text


def test_suspended_installation_blocks_new_reviews(tmp_path) -> None:
    store = TaskStore(tmp_path / "db.sqlite3")
    client = TestClient(create_app(store, GitHub(), SECRET, start_worker=False))
    installation = {"action": "suspended", "installation": {"id": 7}}

    assert post(client, installation, event="installation").json() == {"status": "accepted"}
    assert post(client, payload(), delivery="d2").json() == {"status": "ignored"}


def test_installation_daily_limit_rejects_new_head(tmp_path) -> None:
    store = TaskStore(tmp_path / "db.sqlite3")
    client = TestClient(
        create_app(
            store,
            GitHub(),
            SECRET,
            start_worker=False,
            installation_daily_task_limit=1,
        )
    )
    assert post(client, payload(), delivery="d1").json() == {"status": "accepted"}
    assert post(client, payload("def456"), delivery="d2").json() == {"status": "limited"}


def test_removed_repository_blocks_new_reviews(tmp_path) -> None:
    store = TaskStore(tmp_path / "db.sqlite3")
    client = TestClient(create_app(store, GitHub(), SECRET, start_worker=False))
    removed = {
        "action": "removed",
        "installation": {"id": 7},
        "repositories_added": [],
        "repositories_removed": [{"id": 11}],
    }
    assert post(client, removed, event="installation_repositories").json() == {
        "status": "accepted"
    }
    assert post(client, payload(), delivery="d2").json() == {"status": "ignored"}


def test_ordinary_pr_comment_is_ignored_without_github_lookup(tmp_path) -> None:
    store = TaskStore(tmp_path / "db.sqlite3")
    github = GitHub()
    client = TestClient(create_app(store, github, SECRET, start_worker=False))
    ordinary = {
        "action": "created",
        "installation": {"id": 7},
        "repository": {"id": 11, "full_name": "acme/api"},
        "issue": {"number": 3, "pull_request": {}},
        "comment": {"id": 99, "body": "ordinary discussion", "author_association": "MEMBER"},
        "sender": {"type": "User"},
    }

    assert post(client, ordinary, delivery="ordinary", event="issue_comment").json() == {
        "status": "ignored"
    }
    assert github.get_pr_calls == 0
