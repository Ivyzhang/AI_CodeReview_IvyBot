from app.models import ReviewTaskDraft, TriggerMode, normalize_focus


def test_normalize_focus_collapses_whitespace() -> None:
    assert normalize_focus("  security\n  and   auth  ") == "security and auth"


def test_automatic_tasks_share_a_key_for_the_same_head() -> None:
    opened = ReviewTaskDraft(
        installation_id=7,
        repository_id=11,
        owner="acme",
        repo="api",
        pull_number=3,
        head_sha="abc123",
        trigger_mode=TriggerMode.AUTOMATIC,
        trigger="opened",
    )
    synchronized = opened.model_copy(update={"trigger": "synchronize"})

    assert opened.idempotency_key == synchronized.idempotency_key


def test_manual_tasks_separate_distinct_normalized_focus() -> None:
    base = ReviewTaskDraft(
        installation_id=7,
        repository_id=11,
        owner="acme",
        repo="api",
        pull_number=3,
        head_sha="abc123",
        trigger_mode=TriggerMode.MANUAL,
        trigger="comment:/review",
        focus=" security   only ",
    )
    same = base.model_copy(update={"focus": "security only"})
    different = base.model_copy(update={"focus": "performance"})

    assert base.idempotency_key == same.idempotency_key
    assert base.idempotency_key != different.idempotency_key
