from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field

from app.models import RepositoryPolicy


HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
SECURITY_NAMES = {
    "auth",
    "login",
    "permission",
    "security",
    "secret",
    "token",
    "query",
    "database",
    "middleware",
    "route",
    "api",
    "requirements",
    "pyproject",
    "package-lock",
    "dockerfile",
}


@dataclass(frozen=True)
class ChangedFile:
    path: str
    patch: str = ""
    status: str = "modified"
    additions: int = 0
    deletions: int = 0

    @property
    def changed_lines(self) -> int:
        return self.additions + self.deletions


@dataclass(frozen=True)
class ContextFile:
    path: str
    rendered_patch: str
    valid_lines: set[int]
    status: str
    additions: int
    deletions: int


@dataclass(frozen=True)
class ReviewContext:
    files: list[ContextFile] = field(default_factory=list)
    total_files: int = 0
    excluded_files: int = 0
    omitted_files: int = 0

    @property
    def coverage(self) -> str:
        return f"已检查 {len(self.files)}/{self.total_files} 个文件"

    @property
    def line_map(self) -> dict[str, set[int]]:
        return {item.path: item.valid_lines for item in self.files}


def annotate_patch(patch: str, *, max_chars: int) -> tuple[str, set[int]]:
    rendered: list[str] = []
    valid: set[int] = set()
    new_line = 0
    for raw in patch[:max_chars].splitlines():
        hunk = HUNK_RE.match(raw)
        if hunk:
            new_line = int(hunk.group(1))
            rendered.append(raw)
            continue
        if new_line == 0 or raw.startswith("\\"):
            continue
        if raw.startswith("-") and not raw.startswith("---"):
            rendered.append(f"{'-':>6} | {raw}")
            continue
        text = raw[1:] if raw.startswith(("+", " ")) else raw
        marker = "+" if raw.startswith("+") else " "
        rendered.append(f"{new_line:>6} {marker} | {text}")
        valid.add(new_line)
        new_line += 1
    return "\n".join(rendered), valid


def _excluded(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _priority(item: ChangedFile) -> tuple[int, int, str]:
    lowered = item.path.lower()
    segments = set(re.split(r"[/_.-]", lowered))
    if segments & SECURITY_NAMES:
        group = 0
    elif lowered.endswith((".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java")):
        group = 1
    elif "test" in segments or "tests" in segments:
        group = 2
    else:
        group = 3
    return group, -item.changed_lines, item.path


def build_context(
    files: list[ChangedFile],
    policy: RepositoryPolicy,
    *,
    max_patch_chars: int,
    max_input_chars: int,
) -> ReviewContext:
    candidates = [item for item in files if not _excluded(item.path, policy.exclude_paths)]
    excluded = len(files) - len(candidates)
    selected: list[ContextFile] = []
    used_chars = 0
    used_lines = 0
    for item in sorted(candidates, key=_priority):
        if len(selected) >= policy.max_files:
            break
        if used_lines + item.changed_lines > policy.max_changed_lines:
            continue
        rendered, valid = annotate_patch(item.patch, max_chars=max_patch_chars)
        if not rendered or used_chars + len(rendered) > max_input_chars:
            continue
        selected.append(
            ContextFile(
                path=item.path,
                rendered_patch=rendered,
                valid_lines=valid,
                status=item.status,
                additions=item.additions,
                deletions=item.deletions,
            )
        )
        used_chars += len(rendered)
        used_lines += item.changed_lines
    return ReviewContext(
        files=selected,
        total_files=len(files),
        excluded_files=excluded,
        omitted_files=len(candidates) - len(selected),
    )
