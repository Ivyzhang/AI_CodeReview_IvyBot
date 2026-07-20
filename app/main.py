from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI, HTTPException, Request, Response

from app.config import Settings
from app.events import ALLOWED_ASSOCIATIONS, COMMAND_RE, task_from_event
from app.github import GitHubAppTokens, GitHubClient
from app.models import RepositoryPolicy, TriggerMode
from app.policy import PolicyError, parse_policy
from app.review import OpenAIModelClient, ReviewEngine
from app.service import ReviewService
from app.storage import TaskStore
from app.worker import ReviewWorker


def _valid_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def create_app(
    store: TaskStore,
    github,
    webhook_secret: str,
    *,
    service: ReviewService | None = None,
    start_worker: bool = True,
    poll_seconds: float = 0.5,
    stale_minutes: int = 10,
    installation_daily_task_limit: int = 200,
    max_webhook_body_bytes: int = 1_000_000,
) -> FastAPI:
    worker = (
        ReviewWorker(
            store,
            service,
            poll_seconds=poll_seconds,
            stale_after=timedelta(minutes=stale_minutes),
        )
        if start_worker and service
        else None
    )
    counters = {
        "accepted": 0,
        "ignored": 0,
        "duplicate": 0,
        "existing": 0,
        "limited": 0,
    }

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if worker:
            worker.start()
        yield
        if worker:
            worker.stop()

    app = FastAPI(title="GitHub AI Review", lifespan=lifespan)

    @app.post("/hook", status_code=202)
    async def hook(request: Request) -> dict[str, str]:
        body = await request.body()
        if len(body) > max_webhook_body_bytes:
            raise HTTPException(status_code=413, detail="webhook body too large")
        if not _valid_signature(
            body, request.headers.get("X-Hub-Signature-256", ""), webhook_secret
        ):
            raise HTTPException(status_code=401, detail="invalid signature")
        delivery_id = request.headers.get("X-GitHub-Delivery", "")
        if not delivery_id:
            raise HTTPException(status_code=400, detail="missing delivery id")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc
        event = request.headers.get("X-GitHub-Event", "")

        if event in {"installation", "installation_repositories"}:
            installation_id = int((payload.get("installation") or {}).get("id", 0))
            if not installation_id:
                counters["ignored"] += 1
                return {"status": "ignored"}
            if event == "installation_repositories":
                accepted = store.record_repositories(
                    delivery_id,
                    installation_id,
                    added=[int(item["id"]) for item in payload.get("repositories_added", [])],
                    removed=[int(item["id"]) for item in payload.get("repositories_removed", [])],
                )
            else:
                active = payload.get("action", "") not in {
                    "deleted",
                    "suspend",
                    "suspended",
                }
                accepted = store.record_installation(
                    delivery_id, event, installation_id, active=active
                )
            status = "accepted" if accepted else "duplicate"
            counters[status] += 1
            return {"status": status}

        installation = payload.get("installation", {}).get("id")
        repository = payload.get("repository") or {}
        full_name = repository.get("full_name", "")
        if not installation or "/" not in full_name:
            counters["ignored"] += 1
            return {"status": "ignored"}
        owner, repo = full_name.split("/", 1)
        repository_id = int(repository.get("id", 0))
        if not store.installation_active(int(installation)) or not store.repository_active(
            int(installation), repository_id
        ):
            counters["ignored"] += 1
            return {"status": "ignored"}

        manual_sha = None
        if event == "issue_comment" and "pull_request" in (payload.get("issue") or {}):
            comment = payload.get("comment") or {}
            body_text = (comment.get("body") or "").strip()
            if (
                payload.get("action") != "created"
                or not COMMAND_RE.match(body_text)
                or comment.get("author_association", "").upper()
                not in ALLOWED_ASSOCIATIONS
                or payload.get("sender", {}).get("type") == "Bot"
            ):
                counters["ignored"] += 1
                return {"status": "ignored"}
            try:
                pr = github.get_pr(installation, owner, repo, int(payload["issue"]["number"]))
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=503, detail="github unavailable") from exc
            manual_sha = pr["head"]["sha"]
            base_ref = pr["base"]["ref"]
        else:
            base_ref = (payload.get("pull_request") or {}).get("base", {}).get("ref")
        try:
            policy_text = (
                github.get_repository_file(
                    installation, owner, repo, ".ai-review.yml", ref=base_ref
                )
                if base_ref
                else None
            )
            policy = parse_policy(policy_text)
        except PolicyError:
            if event == "issue_comment" and manual_sha:
                try:
                    github.create_comment(
                        int(installation),
                        owner,
                        repo,
                        int((payload.get("issue") or {}).get("number", 0)),
                        "AI Review 配置无效，请检查默认分支上的 .ai-review.yml。",
                    )
                except Exception:
                    pass
            counters["ignored"] += 1
            return {"status": "ignored"}

        draft = task_from_event(event, payload, policy, manual_head_sha=manual_sha)
        if draft is None:
            counters["ignored"] += 1
            return {"status": "ignored"}
        since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        if store.tasks_since(draft.installation_id, since) >= installation_daily_task_limit:
            counters["limited"] += 1
            if draft.trigger_mode is TriggerMode.MANUAL:
                github.create_comment(
                    draft.installation_id,
                    draft.owner,
                    draft.repo,
                    draft.pull_number,
                    "AI Review 今日调用额度已用完，请稍后再试。",
                )
            return {"status": "limited"}
        try:
            result = store.accept(delivery_id, event, draft)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="task persistence failed") from exc
        counters[result.status.value] += 1
        if (
            result.status.value == "accepted"
            and draft.trigger_mode is TriggerMode.MANUAL
            and draft.source_comment_id
        ):
            github.add_reaction(
                draft.installation_id,
                draft.owner,
                draft.repo,
                draft.source_comment_id,
                "eyes",
            )
        return {"status": result.status.value}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> dict[str, str]:
        try:
            store.depth()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="storage unavailable") from exc
        if worker and not worker.alive:
            raise HTTPException(status_code=503, detail="worker unavailable")
        return {"status": "ready"}

    @app.get("/metrics")
    def metrics() -> Response:
        counts = store.status_counts()
        lines = [f"review_queue_depth {store.depth()}"]
        lines.extend(f"review_tasks{{status=\"{key}\"}} {value}" for key, value in counts.items())
        lines.extend(f"review_webhooks_total{{status=\"{key}\"}} {value}" for key, value in counters.items())
        return Response("\n".join(lines) + "\n", media_type="text/plain")

    return app


def create_app_from_env() -> FastAPI:
    settings = Settings()
    private_key = settings.github_app_private_key_path.read_text()
    tokens = GitHubAppTokens(app_id=settings.github_app_id, private_key=private_key)
    github = GitHubClient(tokens)
    store = TaskStore(settings.database_path)
    model = OpenAIModelClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout=settings.model_timeout_seconds,
    )
    engine = ReviewEngine(model, max_comments=settings.max_comments)
    service = ReviewService(
        store,
        github,
        engine,
        max_patch_chars=settings.max_patch_chars,
        max_input_chars=settings.max_input_chars,
        max_comments=settings.max_comments,
        max_task_seconds=settings.max_task_seconds,
    )
    return create_app(
        store,
        github,
        settings.github_webhook_secret,
        service=service,
        poll_seconds=settings.worker_poll_seconds,
        stale_minutes=settings.stale_task_minutes,
        installation_daily_task_limit=settings.installation_daily_task_limit,
        max_webhook_body_bytes=settings.max_webhook_body_bytes,
    )
