from __future__ import annotations

import fnmatch
import re

from app.models import RepositoryPolicy, ReviewTaskDraft, TriggerMode


ALLOWED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
COMMAND_RE = re.compile(r"^/review(?:\s+|$)")


def task_from_event(
    event: str,
    payload: dict,
    policy: RepositoryPolicy,
    *,
    manual_head_sha: str | None = None,
) -> ReviewTaskDraft | None:
    if not policy.enabled:
        return None
    repository = payload.get("repository") or {}
    installation = payload.get("installation") or {}
    full_name = repository.get("full_name", "")
    if "/" not in full_name or not installation.get("id") or not repository.get("id"):
        return None
    owner, repo = full_name.split("/", 1)

    if event == "pull_request":
        pull = payload.get("pull_request") or {}
        action = payload.get("action", "")
        allowed = {"opened", "reopened", "ready_for_review"}
        if policy.auto_review:
            allowed.add("synchronize")
        if action not in allowed:
            return None
        if pull.get("draft") and not policy.review_drafts:
            return None
        if payload.get("sender", {}).get("type") == "Bot" and not policy.review_bot_prs:
            return None
        base_ref = pull.get("base", {}).get("ref", "")
        if not any(fnmatch.fnmatch(base_ref, pattern) for pattern in policy.include_branches):
            return None
        return ReviewTaskDraft(
            installation_id=int(installation["id"]),
            repository_id=int(repository["id"]),
            owner=owner,
            repo=repo,
            pull_number=int(payload["number"]),
            head_sha=pull["head"]["sha"],
            trigger_mode=TriggerMode.AUTOMATIC,
            trigger=action,
        )

    if event != "issue_comment" or payload.get("action") != "created":
        return None
    issue = payload.get("issue") or {}
    comment = payload.get("comment") or {}
    body = (comment.get("body") or "").strip()
    if (
        "pull_request" not in issue
        or not COMMAND_RE.match(body)
        or comment.get("author_association", "").upper() not in ALLOWED_ASSOCIATIONS
        or payload.get("sender", {}).get("type") == "Bot"
        or not manual_head_sha
    ):
        return None
    return ReviewTaskDraft(
        installation_id=int(installation["id"]),
        repository_id=int(repository["id"]),
        owner=owner,
        repo=repo,
        pull_number=int(issue["number"]),
        head_sha=manual_head_sha,
        trigger_mode=TriggerMode.MANUAL,
        trigger="comment:/review",
        focus=body[len("/review") :][:1000],
        user_initiated=True,
        source_comment_id=int(comment["id"]),
    )
