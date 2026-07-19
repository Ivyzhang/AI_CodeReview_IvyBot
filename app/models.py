from __future__ import annotations

import hashlib
import re
from enum import StrEnum

from pydantic import BaseModel, Field, computed_field


class TriggerMode(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class Severity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


def normalize_focus(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


class ReviewTaskDraft(BaseModel):
    installation_id: int = Field(gt=0)
    repository_id: int = Field(gt=0)
    owner: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    pull_number: int = Field(gt=0)
    head_sha: str = Field(min_length=1)
    trigger_mode: TriggerMode
    trigger: str = Field(min_length=1)
    focus: str = ""
    user_initiated: bool = False
    source_comment_id: int | None = None

    @computed_field
    @property
    def normalized_focus(self) -> str:
        return normalize_focus(self.focus)

    @computed_field
    @property
    def idempotency_key(self) -> str:
        parts = [
            str(self.installation_id),
            str(self.repository_id),
            str(self.pull_number),
            self.head_sha,
            self.trigger_mode.value,
        ]
        if self.trigger_mode is TriggerMode.MANUAL:
            digest = hashlib.sha256(self.normalized_focus.encode()).hexdigest()
            parts.append(digest)
        return ":".join(parts)


class ReviewTask(ReviewTaskDraft):
    id: str
    status: TaskStatus
    attempt_count: int = 0


class Finding(BaseModel):
    path: str = Field(min_length=1)
    line: int = Field(gt=0)
    severity: Severity
    body: str = Field(min_length=1)


class ReviewResult(BaseModel):
    summary: str = Field(min_length=1)
    comments: list[Finding] = Field(default_factory=list)


class RepositoryPolicy(BaseModel):
    enabled: bool = True
    auto_review: bool = True
    review_drafts: bool = False
    review_bot_prs: bool = False
    include_branches: list[str] = Field(default_factory=lambda: ["*"])
    exclude_paths: list[str] = Field(
        default_factory=lambda: ["vendor/**", "dist/**", "**/*.min.js"]
    )
    max_files: int = Field(default=20, gt=0, le=100)
    max_changed_lines: int = Field(default=2000, gt=0, le=10_000)
    review_language: str = "zh-CN"
    minimum_severity: Severity = Severity.LOW
