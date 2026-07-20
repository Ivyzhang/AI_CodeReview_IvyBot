from datetime import UTC, datetime, timedelta

from app.models import ReviewTaskDraft, TaskStatus, TriggerMode
from app.storage import AcceptStatus, TaskStore


def draft(*, mode: TriggerMode = TriggerMode.AUTOMATIC, focus: str = "") -> ReviewTaskDraft:
    return ReviewTaskDraft(
        installation_id=7,
        repository_id=11,
        owner="acme",
        repo="api",
        pull_number=3,
        head_sha="abc123",
        trigger_mode=mode,
        trigger="opened" if mode is TriggerMode.AUTOMATIC else "comment:/review",
        focus=focus,
        user_initiated=mode is TriggerMode.MANUAL,
    )


def test_duplicate_delivery_is_accepted_once(tmp_path) -> None:
    store = TaskStore(tmp_path / "review.sqlite3")

    first = store.accept("delivery-1", "pull_request", draft())
    second = store.accept("delivery-1", "pull_request", draft())

    assert first.status is AcceptStatus.ACCEPTED
    assert second.status is AcceptStatus.DUPLICATE
    assert second.task.id == first.task.id


def test_automatic_events_for_same_head_share_task(tmp_path) -> None:
    store = TaskStore(tmp_path / "review.sqlite3")

    first = store.accept("delivery-1", "pull_request", draft())
    changed_action = draft().model_copy(update={"trigger": "ready_for_review"})
    second = store.accept("delivery-2", "pull_request", changed_action)

    assert second.status is AcceptStatus.EXISTING
    assert second.task.id == first.task.id


def test_new_delivery_requeues_failed_task_for_same_head(tmp_path) -> None:
    store = TaskStore(tmp_path / "review.sqlite3")
    first = store.accept("delivery-1", "pull_request", draft())
    store.set_status(first.task.id, TaskStatus.FAILED)

    retried = store.accept("delivery-2", "pull_request", draft())

    assert retried.status is AcceptStatus.ACCEPTED
    assert retried.task.id == first.task.id
    assert retried.task.status is TaskStatus.QUEUED
    assert store.depth() == 1


def test_new_delivery_does_not_requeue_completed_task(tmp_path) -> None:
    store = TaskStore(tmp_path / "review.sqlite3")
    first = store.accept("delivery-1", "pull_request", draft())
    store.set_status(first.task.id, TaskStatus.COMPLETED)

    repeated = store.accept("delivery-2", "pull_request", draft())

    assert repeated.status is AcceptStatus.EXISTING
    assert repeated.task.status is TaskStatus.COMPLETED


def test_manual_focus_creates_distinct_tasks(tmp_path) -> None:
    store = TaskStore(tmp_path / "review.sqlite3")

    first = store.accept("delivery-1", "issue_comment", draft(mode=TriggerMode.MANUAL, focus="security"))
    second = store.accept("delivery-2", "issue_comment", draft(mode=TriggerMode.MANUAL, focus="performance"))

    assert first.task.id != second.task.id


def test_stale_running_task_is_recovered(tmp_path) -> None:
    store = TaskStore(tmp_path / "review.sqlite3")
    accepted = store.accept("delivery-1", "pull_request", draft())
    old = datetime.now(UTC) - timedelta(hours=1)
    store.mark_running(accepted.task.id, now=old)

    assert store.recover_stale(before=datetime.now(UTC) - timedelta(minutes=10)) == 1
    assert store.get(accepted.task.id).status is TaskStatus.QUEUED


def test_publish_record_is_unique_per_task(tmp_path) -> None:
    store = TaskStore(tmp_path / "review.sqlite3")
    task = store.accept("delivery-1", "pull_request", draft()).task

    assert store.record_publish(task.id, "marker", github_review_id=91, mode="inline_review")
    assert not store.record_publish(task.id, "marker", github_review_id=92, mode="inline_review")
    assert store.get_publish(task.id).github_review_id == 91


def test_disabled_installation_stops_unfinished_tasks(tmp_path) -> None:
    store = TaskStore(tmp_path / "review.sqlite3")
    current = store.accept("delivery-1", "pull_request", draft()).task

    assert store.record_installation("installation-1", "installation", 7, active=False)
    assert not store.record_installation("installation-1", "installation", 7, active=False)
    assert not store.installation_active(7)
    assert store.get(current.id).status is TaskStatus.FAILED


def test_tasks_since_counts_installation_usage(tmp_path) -> None:
    store = TaskStore(tmp_path / "review.sqlite3")
    store.accept("delivery-1", "pull_request", draft())
    start_of_day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    assert store.tasks_since(7, start_of_day) == 1


def test_removed_repository_is_no_longer_active(tmp_path) -> None:
    store = TaskStore(tmp_path / "review.sqlite3")
    assert store.record_repositories(
        "repos-1", 7, added=[11], removed=[]
    )
    assert store.repository_active(7, 11)
    assert store.record_repositories(
        "repos-2", 7, added=[], removed=[11]
    )
    assert not store.repository_active(7, 11)
