# GitHub AI Review V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the complete single-instance GitHub App AI code review service defined in `project-design-v1.md`.

**Architecture:** FastAPI accepts and verifies GitHub webhooks, then atomically persists deliveries and review tasks in SQLite. A single worker restores and executes tasks through injected GitHub, context, LLM, validation, and publishing boundaries; publishing always rechecks the PR head SHA and uses a marker for idempotency.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, httpx, PyJWT/cryptography, PyYAML, SQLite, pytest

---

## File Structure

- `pyproject.toml`: package metadata, runtime dependencies, pytest settings.
- `.env.example`: runtime setting names without secrets.
- `.gitignore`: local environment, database, cache, and secret exclusions.
- `app/config.py`: validated process settings and system limits.
- `app/models.py`: tasks, findings, review results, repository policy, and statuses.
- `app/storage.py`: SQLite schema, delivery/task transactions, recovery, and publish records.
- `app/policy.py`: `.ai-review.yml` parsing and deterministic defaults.
- `app/events.py`: webhook event-to-task conversion and business idempotency keys.
- `app/github.py`: GitHub App token provider and REST adapter.
- `app/context.py`: patch line annotation, file ranking, truncation, and prompt context.
- `app/review.py`: prompt construction, model response parsing, and finding validation.
- `app/service.py`: task orchestration, double SHA gate, idempotent publish, and fallback rules.
- `app/worker.py`: persistent single-worker loop and stale task recovery.
- `app/main.py`: FastAPI webhook, health/readiness/metrics, and lifespan wiring.
- `tests/`: business-level tests only; no configuration-file, migration-file, or framework-boilerplate tests.

### Task 1: Package Skeleton and Domain Models

**Files:** Create `pyproject.toml`, `.gitignore`, `.env.example`, `app/__init__.py`, `app/models.py`, `tests/test_models.py`.

- [ ] Write a failing test that normalizes manual focus and builds stable automatic/manual idempotency keys.
- [ ] Run `python3.12 -m pytest tests/test_models.py -q` and confirm imports fail because domain code does not exist.
- [ ] Implement enums and Pydantic/dataclass models for `ReviewTask`, `Finding`, `ReviewResult`, `RepositoryPolicy`, and key helpers.
- [ ] Run the focused test and confirm it passes.

### Task 2: SQLite Persistence and Recovery

**Files:** Create `app/storage.py`, `tests/test_storage.py`.

- [ ] Write failing tests for atomic delivery acceptance, automatic task merging, manual focus separation, stale-running recovery, and publish uniqueness.
- [ ] Run `python3.12 -m pytest tests/test_storage.py -q` and confirm failures are due to missing storage behavior.
- [ ] Implement schema creation and a transaction-based `TaskStore` using SQLite unique constraints.
- [ ] Run storage tests and confirm they pass.

### Task 3: Event Routing and Repository Policy

**Files:** Create `app/events.py`, `app/policy.py`, `tests/test_events.py`, `tests/test_policy_behavior.py`.

- [ ] Write failing business tests for supported PR actions, drafts, bot PR policy, authorized `/review`, unauthorized commands, installation disable events, and base-branch policy behavior.
- [ ] Run the focused tests and confirm expected failures.
- [ ] Implement event parsing and policy application with `.ai-review.yml` defaults and safe upper bounds.
- [ ] Run focused tests and confirm they pass.

### Task 4: Context Selection and Diff Lines

**Files:** Create `app/context.py`, `tests/test_context.py`.

- [ ] Write failing tests for multi-hunk RIGHT-side lines, deleted-line rejection, deterministic file priority, exclusion, truncation, and coverage reporting.
- [ ] Run the focused tests and confirm expected failures.
- [ ] Implement patch annotation, ranking, budget enforcement, and `ReviewContext` assembly.
- [ ] Run focused tests and confirm they pass.

### Task 5: Structured AI Review

**Files:** Create `app/review.py`, `tests/test_review.py`.

- [ ] Write failing tests for prompt trust boundaries, JSON parsing, one repair attempt, comment limits, and invalid path/line separation.
- [ ] Run the focused tests and confirm expected failures.
- [ ] Implement an injected model client protocol, OpenAI-compatible adapter, prompt builder, parser, and finding validator.
- [ ] Run focused tests and confirm they pass.

### Task 6: GitHub App Adapter

**Files:** Create `app/github.py`, `tests/test_github_behavior.py`.

- [ ] Write failing behavior tests for token caching boundaries, paginated PR files, review marker lookup, batch review payloads, fallback comments, and command reactions.
- [ ] Run focused tests and confirm expected failures.
- [ ] Implement GitHub App JWT/Installation Token creation and the minimal REST operations required by the service.
- [ ] Run focused tests and confirm they pass.

### Task 7: Review Orchestration

**Files:** Create `app/service.py`, `tests/test_service.py`.

- [ ] Write failing tests proving the pre-model SHA gate, post-model SHA gate, no fallback for stale/closed/unauthorized PRs, fallback only for explicit invalid positions, and reconciliation after ambiguous publish errors.
- [ ] Run focused tests and confirm expected failures.
- [ ] Implement `ReviewService` with injected adapters and explicit task status transitions.
- [ ] Run focused tests and confirm they pass.

### Task 8: Worker and HTTP Application

**Files:** Create `app/worker.py`, `app/config.py`, `app/main.py`, `tests/test_webhook.py`, `tests/test_worker.py`.

- [ ] Write failing business tests for signature rejection, ignored/duplicate/existing/accepted responses, persistence before `202`, task recovery, and health/readiness business state.
- [ ] Run focused tests and confirm expected failures.
- [ ] Implement the worker lifecycle, FastAPI routes, GitHub signature validation, metrics counters, and dependency wiring.
- [ ] Run focused tests and confirm they pass.

### Task 9: Full Verification and Operations Documentation

**Files:** Create `README.md`; update no design documents.

- [ ] Document GitHub App permissions/events, local setup, SQLite persistent-volume requirement, `.ai-review.yml`, run command, endpoints, and privacy boundary.
- [ ] Run `python3.12 -m pytest -q` and require zero failures.
- [ ] Run `python3.12 -m compileall -q app` and require exit code 0.
- [ ] Import `app.main` with test settings and verify the FastAPI routes exist.
- [ ] Scan source for placeholders, accidental secrets, tutorial/demo language, and obsolete in-memory queue behavior.
