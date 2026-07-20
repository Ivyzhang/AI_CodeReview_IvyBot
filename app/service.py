from __future__ import annotations

import hashlib
import logging
import time

import httpx

from app.context import ChangedFile, build_context
from app.github import InlinePositionError
from app.models import Finding, ReviewTask, Severity, TaskStatus, TriggerMode
from app.policy import PolicyError, parse_policy
from app.review import ReviewEngine, build_prompt, validate_findings
from app.storage import TaskStore


log = logging.getLogger(__name__)


def _findings_markdown(findings: list[Finding]) -> str:
    return "\n".join(
        f"- **{item.severity.value.upper()}** `{item.path}:{item.line}`: {item.body}"
        for item in findings
    )


class ReviewService:
    def __init__(
        self,
        store: TaskStore,
        github,
        engine: ReviewEngine,
        *,
        max_patch_chars: int = 6000,
        max_input_chars: int = 60_000,
        max_comments: int = 20,
        max_task_seconds: float = 300,
    ) -> None:
        self.store = store
        self.github = github
        self.engine = engine
        self.max_patch_chars = max_patch_chars
        self.max_input_chars = max_input_chars
        self.max_comments = max_comments
        self.max_task_seconds = max_task_seconds

    def process(self, task: ReviewTask) -> None:
        try:
            self._process(task)
        except Exception:
            self.store.set_status(task.id, TaskStatus.FAILED)
            raise

    def _process(self, task: ReviewTask) -> None:
        started = time.monotonic()
        pr = self.github.get_pr(
            task.installation_id, task.owner, task.repo, task.pull_number
        )
        if pr.get("state") != "open" or pr["head"]["sha"] != task.head_sha:
            self._supersede(task)
            return

        policy_text = self.github.get_repository_file(
            task.installation_id,
            task.owner,
            task.repo,
            ".ai-review.yml",
            ref=pr["base"]["ref"],
        )
        policy = parse_policy(policy_text)
        raw_files = self.github.list_pr_files(
            task.installation_id, task.owner, task.repo, task.pull_number
        )
        files = [
            ChangedFile(
                path=item["filename"],
                patch=item.get("patch") or "",
                status=item.get("status", "modified"),
                additions=int(item.get("additions", 0)),
                deletions=int(item.get("deletions", 0)),
            )
            for item in raw_files
        ]
        context = build_context(
            files,
            policy,
            max_patch_chars=self.max_patch_chars,
            max_input_chars=self.max_input_chars,
        )
        self._ensure_time(started)
        prompt = build_prompt(
            context,
            metadata={
                "repository": f"{task.owner}/{task.repo}",
                "number": task.pull_number,
                "title": pr.get("title", ""),
                "body": pr.get("body") or "",
                "base": pr["base"]["ref"],
                "head": pr["head"]["ref"],
            },
            focus=task.normalized_focus,
            language=policy.review_language,
        )
        result = self.engine.review(prompt)
        self._ensure_time(started)
        valid, invalid = validate_findings(
            result,
            context.line_map,
            max_comments=self.max_comments,
            minimum_severity=policy.minimum_severity,
        )

        if not self.store.can_publish(task.id):
            self.store.set_status(task.id, TaskStatus.FAILED)
            return
        if not self._head_is_current(task):
            self._supersede(task)
            return

        marker = hashlib.sha256(task.idempotency_key.encode()).hexdigest()[:24]
        published = self.store.get_publish(task.id)
        if published:
            self.store.set_status(task.id, TaskStatus.COMPLETED)
            return
        existing_id = self.github.find_review_by_marker(
            task.installation_id,
            task.owner,
            task.repo,
            task.pull_number,
            marker,
        )
        if existing_id:
            self.store.record_publish(
                task.id, marker, github_review_id=existing_id, mode="inline_review"
            )
            self._complete(task)
            return
        existing_comment_id = self.github.find_comment_by_marker(
            task.installation_id,
            task.owner,
            task.repo,
            task.pull_number,
            marker,
        )
        if existing_comment_id:
            self.store.record_publish(
                task.id, marker, github_comment_id=existing_comment_id, mode="fallback_comment"
            )
            self._complete(task)
            return

        body = f"<!-- ai-review:{marker} -->\n{result.summary.strip()}\n\n{context.coverage}"
        if invalid:
            body += "\n\n### 无法可靠定位的发现\n" + _findings_markdown(invalid)
        comments = [
            {
                "path": item.path,
                "line": item.line,
                "side": "RIGHT",
                "body": f"**{item.severity.value.upper()}**: {item.body}",
            }
            for item in valid
        ]
        try:
            review_id = self.github.create_review(
                task.installation_id,
                task.owner,
                task.repo,
                task.pull_number,
                commit_id=task.head_sha,
                body=body,
                comments=comments,
            )
            self.store.record_publish(
                task.id, marker, github_review_id=review_id, mode="inline_review"
            )
        except InlinePositionError:
            if not self._head_is_current(task):
                self._supersede(task)
                return
            fallback = body
            if valid:
                fallback += "\n\n### Findings\n" + _findings_markdown(valid)
            fallback += "\n\n> 行内位置不可用，结果已作为普通评论发布。"
            comment_id = self.github.create_comment(
                task.installation_id,
                task.owner,
                task.repo,
                task.pull_number,
                fallback,
            )
            self.store.record_publish(
                task.id, marker, github_comment_id=comment_id, mode="fallback_comment"
            )
        except httpx.HTTPError:
            existing_id = self.github.find_review_by_marker(
                task.installation_id,
                task.owner,
                task.repo,
                task.pull_number,
                marker,
            )
            if not existing_id:
                existing_comment_id = self.github.find_comment_by_marker(
                    task.installation_id,
                    task.owner,
                    task.repo,
                    task.pull_number,
                    marker,
                )
                if existing_comment_id:
                    self.store.record_publish(
                        task.id,
                        marker,
                        github_comment_id=existing_comment_id,
                        mode="fallback_comment",
                    )
                    self._complete(task)
                    return
            if not existing_id:
                raise
            self.store.record_publish(
                task.id, marker, github_review_id=existing_id, mode="inline_review"
            )
        self._complete(task)

    def _ensure_time(self, started: float) -> None:
        if time.monotonic() - started > self.max_task_seconds:
            raise TimeoutError("review task exceeded total time limit")

    def _head_is_current(self, task: ReviewTask) -> bool:
        pr = self.github.get_pr(
            task.installation_id, task.owner, task.repo, task.pull_number
        )
        return pr.get("state") == "open" and pr["head"]["sha"] == task.head_sha

    def _complete(self, task: ReviewTask) -> None:
        self.store.set_status(task.id, TaskStatus.COMPLETED)
        if task.trigger_mode is TriggerMode.MANUAL and task.source_comment_id:
            self.github.add_reaction(
                task.installation_id,
                task.owner,
                task.repo,
                task.source_comment_id,
                "+1",
            )

    def _supersede(self, task: ReviewTask) -> None:
        self.store.set_status(task.id, TaskStatus.SUPERSEDED)
        if task.trigger_mode is TriggerMode.MANUAL and task.source_comment_id:
            self.github.add_reaction(
                task.installation_id,
                task.owner,
                task.repo,
                task.source_comment_id,
                "confused",
            )

    def notify_failure(self, task: ReviewTask) -> None:
        if task.trigger_mode is not TriggerMode.MANUAL:
            return
        try:
            self.github.create_comment(
                task.installation_id,
                task.owner,
                task.repo,
                task.pull_number,
                "AI Review 失败，请稍后重试或联系服务管理员。",
            )
        except Exception:
            log.exception("failed to notify user for task %s", task.id)
