import httpx
import pytest

from app.github import GitHubClient, InlinePositionError


class Tokens:
    def token(self, installation_id: int) -> str:
        assert installation_id == 7
        return "installation-token"


def test_pr_files_are_paginated_and_review_marker_is_found() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/files"):
            page = request.url.params.get("page")
            return httpx.Response(200, json=[{"filename": f"{page}.py"}] if page == "1" else [])
        return httpx.Response(200, json=[{"id": 44, "body": "<!-- ai-review:marker -->"}])

    client = GitHubClient(Tokens(), http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert [item["filename"] for item in client.list_pr_files(7, "acme", "api", 3)] == ["1.py"]
    assert client.find_review_by_marker(7, "acme", "api", 3, "marker") == 44
    assert all(request.headers["authorization"] == "Bearer installation-token" for request in requests)


def test_create_review_sends_one_batch() -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(__import__("json").loads(request.content))
        return httpx.Response(200, json={"id": 91})

    client = GitHubClient(Tokens(), http=httpx.Client(transport=httpx.MockTransport(handler)))
    review_id = client.create_review(
        7,
        "acme",
        "api",
        3,
        commit_id="abc123",
        body="summary",
        comments=[{"path": "a.py", "line": 2, "side": "RIGHT", "body": "risk"}],
    )
    assert review_id == 91
    assert payloads == [
        {
            "commit_id": "abc123",
            "event": "COMMENT",
            "body": "summary",
            "comments": [{"path": "a.py", "line": 2, "side": "RIGHT", "body": "risk"}],
        }
    ]


def test_unprocessable_review_is_classified_as_inline_position_error() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(422, json={"message": "invalid line"}))
    client = GitHubClient(Tokens(), http=httpx.Client(transport=transport))
    with pytest.raises(InlinePositionError):
        client.create_review(7, "acme", "api", 3, commit_id="abc", body="x", comments=[])
