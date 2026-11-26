from __future__ import annotations

import asyncio
import datetime
import logging

import httpx
import pytest

from app.errors import ScanError
from app.github import GitHubClient, parse_repo_url


def test_tree_falls_back_to_branch_on_404() -> None:
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "/git/trees/" in request.url.path and request.url.path.endswith("a" * 40):
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"sha": "root", "tree": [], "truncated": False})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            gh = GitHubClient(client)
            tree, fallback = await gh.get_tree("owner", "repo", "a" * 40, "main")

        assert fallback is True
        assert tree["tree"] == []
        assert gh.request_count == 2

    asyncio.run(run())


def test_tree_retries_remote_protocol_error(monkeypatch) -> None:
    monkeypatch.setattr("app.github.RETRY_DELAYS_S", (0.0, 0.0))
    calls: list[str] = []

    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if len(calls) < 3:
                raise httpx.RemoteProtocolError("peer closed", request=request)
            return httpx.Response(200, json={"sha": "root", "tree": [], "truncated": False})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            gh = GitHubClient(client)
            tree, fallback = await gh.get_tree("owner", "repo", "a" * 40, "main")

        assert fallback is False
        assert tree["tree"] == []
        assert gh.tree_attempts == 3
        assert gh.request_count == 1

    asyncio.run(run())


def test_tree_retries_5xx(monkeypatch) -> None:
    monkeypatch.setattr("app.github.RETRY_DELAYS_S", (0.0, 0.0))
    calls: list[int] = []

    async def run() -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) < 3:
                return httpx.Response(502, json={"message": "bad gateway"})
            return httpx.Response(200, json={"sha": "root", "tree": [], "truncated": False})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            gh = GitHubClient(client)
            tree, _ = await gh.get_tree("owner", "repo", "a" * 40, "main")

        assert tree["tree"] == []
        assert gh.tree_attempts == 3

    asyncio.run(run())


def test_tree_reports_fetch_failed_after_three_attempts(monkeypatch) -> None:
    monkeypatch.setattr("app.github.RETRY_DELAYS_S", (0.0, 0.0))

    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.RemoteProtocolError("peer closed", request=request)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            gh = GitHubClient(client)
            with pytest.raises(ScanError) as caught:
                await gh.get_tree("owner", "repo", "a" * 40, "main")

        assert caught.value.code == "FETCH_FAILED"
        assert gh.tree_attempts == 3

    asyncio.run(run())


def test_parse_repo_url_accepts_git_suffix_and_trailing_slash() -> None:
    assert parse_repo_url("https://github.com/psf/requests.git") == ("psf", "requests")
    assert parse_repo_url("https://github.com/psf/requests/") == ("psf", "requests")


def test_parse_repo_url_rejects_non_github() -> None:
    with pytest.raises(ScanError) as caught:
        parse_repo_url("https://gitlab.com/owner/repo")

    assert caught.value.code == "INVALID_URL"


def test_token_is_sent_in_header_but_never_logged(monkeypatch, caplog) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "dummy-token-do-not-log")
    seen: list[str] = []

    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("authorization", ""))
            return httpx.Response(200, json={"full_name": "a/b"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            gh = GitHubClient(client)
            with caplog.at_level(logging.DEBUG):
                await gh.get_repo("a", "b")

    asyncio.run(run())

    assert seen == ["Bearer dummy-token-do-not-log"]
    assert "dummy-token-do-not-log" not in caplog.text


def test_rate_limit_message_contains_reset_time() -> None:
    reset_at = 1700000000
    expected = datetime.datetime.fromtimestamp(reset_at).strftime("%H:%M")

    async def run() -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                headers={"X-RateLimit-Reset": str(reset_at)},
                json={"message": "API rate limit exceeded"},
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            gh = GitHubClient(client)
            with pytest.raises(ScanError) as caught:
                await gh.get_repo("a", "b")

        assert caught.value.code == "RATE_LIMITED"
        assert f"预计 {expected} 恢复" in caught.value.message
        assert "GITHUB_TOKEN" in caught.value.message

    asyncio.run(run())


def test_rate_limit_without_reset_header_keeps_generic_message() -> None:
    async def run() -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "Forbidden"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            gh = GitHubClient(client)
            with pytest.raises(ScanError) as caught:
                await gh.get_repo("a", "b")

        assert caught.value.code == "RATE_LIMITED"
        assert "配额已耗尽" not in caught.value.message

    asyncio.run(run())
