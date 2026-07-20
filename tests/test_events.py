from app.events import task_from_event
from app.models import RepositoryPolicy, TriggerMode


def pr_payload(action: str = "opened", *, draft: bool = False, sender_type: str = "User") -> dict:
    return {
        "action": action,
        "installation": {"id": 7},
        "repository": {"id": 11, "full_name": "acme/api"},
        "number": 3,
        "sender": {"type": sender_type},
        "pull_request": {
            "draft": draft,
            "head": {"sha": "abc123", "ref": "feature"},
            "base": {"ref": "main"},
        },
    }


def test_supported_pr_event_creates_automatic_task() -> None:
    task = task_from_event("pull_request", pr_payload(), RepositoryPolicy())
    assert task is not None
    assert task.trigger_mode is TriggerMode.AUTOMATIC
    assert task.head_sha == "abc123"


def test_draft_and_bot_pr_follow_policy() -> None:
    assert task_from_event("pull_request", pr_payload(draft=True), RepositoryPolicy()) is None
    assert task_from_event("pull_request", pr_payload(sender_type="Bot"), RepositoryPolicy()) is None


def test_webhook_parse_can_skip_repository_policy_filters() -> None:
    task = task_from_event(
        "pull_request",
        pr_payload(action="synchronize", draft=True, sender_type="Bot"),
        RepositoryPolicy(),
        apply_policy=False,
    )
    assert task is not None


def test_authorized_review_command_keeps_focus() -> None:
    payload = pr_payload()
    payload.update(
        {
            "action": "created",
            "issue": {"number": 3, "pull_request": {}},
            "comment": {
                "id": 99,
                "body": "/review  security only ",
                "author_association": "MEMBER",
            },
        }
    )
    payload.pop("pull_request")

    task = task_from_event(
        "issue_comment", payload, RepositoryPolicy(), manual_head_sha="abc123"
    )
    assert task is not None
    assert task.trigger_mode is TriggerMode.MANUAL
    assert task.normalized_focus == "security only"
    assert task.source_comment_id == 99


def test_unauthorized_review_command_is_ignored() -> None:
    payload = pr_payload()
    payload.update(
        {
            "action": "created",
            "issue": {"number": 3, "pull_request": {}},
            "comment": {"id": 99, "body": "/review", "author_association": "NONE"},
        }
    )
    payload.pop("pull_request")
    assert (
        task_from_event(
            "issue_comment", payload, RepositoryPolicy(), manual_head_sha="abc123"
        )
        is None
    )
