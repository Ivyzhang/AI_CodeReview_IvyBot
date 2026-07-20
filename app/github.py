from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Protocol

import httpx
import jwt


class InlinePositionError(RuntimeError):
    pass


class TokenProvider(Protocol):
    def token(self, installation_id: int) -> str: ...


@dataclass
class _CachedToken:
    value: str
    expires_at: float


class GitHubAppTokens:
    def __init__(
        self,
        *,
        app_id: str,
        private_key: str,
        http: httpx.Client | None = None,
    ) -> None:
        self.app_id = app_id
        self.private_key = private_key
        self.http = http or httpx.Client(timeout=20)
        self._cache: dict[int, _CachedToken] = {}

    def token(self, installation_id: int) -> str:
        cached = self._cache.get(installation_id)
        now = time.time()
        if cached and cached.expires_at - 60 > now:
            return cached.value
        app_jwt = jwt.encode(
            {"iat": int(now) - 60, "exp": int(now) + 540, "iss": self.app_id},
            self.private_key,
            algorithm="RS256",
        )
        response = self.http.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        payload = response.json()
        self._cache[installation_id] = _CachedToken(payload["token"], now + 3500)
        return payload["token"]


class GitHubClient:
    def __init__(self, tokens: TokenProvider, *, http: httpx.Client | None = None) -> None:
        self.tokens = tokens
        self.http = http or httpx.Client(timeout=httpx.Timeout(120, connect=10))

    def _request(self, method: str, installation_id: int, path: str, **kwargs) -> httpx.Response:
        response = self.http.request(
            method,
            f"https://api.github.com{path}",
            headers={
                "Authorization": f"Bearer {self.tokens.token(installation_id)}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            **kwargs,
        )
        response.raise_for_status()
        return response

    def get_pr(self, installation_id: int, owner: str, repo: str, number: int) -> dict:
        return self._request(
            "GET", installation_id, f"/repos/{owner}/{repo}/pulls/{number}"
        ).json()

    def list_pr_files(
        self, installation_id: int, owner: str, repo: str, number: int
    ) -> list[dict]:
        files: list[dict] = []
        for page in range(1, 11):
            batch = self._request(
                "GET",
                installation_id,
                f"/repos/{owner}/{repo}/pulls/{number}/files",
                params={"per_page": 100, "page": page},
            ).json()
            files.extend(batch)
            if len(batch) < 100:
                break
        return files

    def get_repository_file(
        self,
        installation_id: int,
        owner: str,
        repo: str,
        path: str,
        *,
        ref: str,
    ) -> str | None:
        try:
            payload = self._request(
                "GET",
                installation_id,
                f"/repos/{owner}/{repo}/contents/{path}",
                params={"ref": ref},
            ).json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return base64.b64decode(payload["content"]).decode()

    def find_review_by_marker(
        self,
        installation_id: int,
        owner: str,
        repo: str,
        number: int,
        marker: str,
    ) -> int | None:
        needle = f"<!-- ai-review:{marker} -->"
        for page in range(1, 11):
            reviews = self._request(
                "GET",
                installation_id,
                f"/repos/{owner}/{repo}/pulls/{number}/reviews",
                params={"per_page": 100, "page": page},
            ).json()
            for review in reviews:
                if needle in (review.get("body") or ""):
                    return int(review["id"])
            if len(reviews) < 100:
                break
        return None

    def find_comment_by_marker(
        self,
        installation_id: int,
        owner: str,
        repo: str,
        number: int,
        marker: str,
    ) -> int | None:
        needle = f"<!-- ai-review:{marker} -->"
        for page in range(1, 11):
            comments = self._request(
                "GET",
                installation_id,
                f"/repos/{owner}/{repo}/issues/{number}/comments",
                params={"per_page": 100, "page": page},
            ).json()
            for comment in comments:
                if needle in (comment.get("body") or ""):
                    return int(comment["id"])
            if len(comments) < 100:
                break
        return None

    def create_review(
        self,
        installation_id: int,
        owner: str,
        repo: str,
        number: int,
        *,
        commit_id: str,
        body: str,
        comments: list[dict],
    ) -> int:
        try:
            response = self._request(
                "POST",
                installation_id,
                f"/repos/{owner}/{repo}/pulls/{number}/reviews",
                json={
                    "commit_id": commit_id,
                    "event": "COMMENT",
                    "body": body,
                    "comments": comments,
                },
            )
        except httpx.HTTPStatusError as exc:
            message = (exc.response.json().get("message") or "").lower()
            if exc.response.status_code == 422 and any(
                word in message for word in ("line", "position", "side", "diff")
            ):
                raise InlinePositionError("GitHub rejected inline positions") from exc
            raise
        return int(response.json()["id"])

    def create_comment(
        self, installation_id: int, owner: str, repo: str, number: int, body: str
    ) -> int:
        response = self._request(
            "POST",
            installation_id,
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            json={"body": body},
        )
        return int(response.json()["id"])

    def add_reaction(
        self, installation_id: int, owner: str, repo: str, comment_id: int, reaction: str
    ) -> None:
        self._request(
            "POST",
            installation_id,
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions",
            json={"content": reaction},
        )
