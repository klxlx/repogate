from __future__ import annotations

import asyncio
import datetime
import logging

import httpx
import pytest

from app.errors import ScanError
from app.github import GitHubClient, parse_repo_url, resolve_head


ATOM_SHA = "7fd1a60b01f91b314f59955a4e4d4e80d8edf11d"


def _atom_body() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        "  <entry>\n"
        f"    <id>tag:github.com,2008:Grit::Commit/{ATOM_SHA}</id>\n"
        "    <title>Fix &amp; stuff</title>\n"
        "    <updated>2025-01-02T03:04:05Z</updated>\n"
        "    <author><name>Octo Cat</name></author>\n"
        "  </entry>\n"
        "</feed>\n"
    )


def test_resolve_head_uses_atom_without_api_quota(monkeypatch) -> None:
    calls: list[str] = []

    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            assert request.url.host == "github.com"
            return httpx.Response(200, text=_atom_body())

        async def fake_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        monkeypatch.setattr("app.github.get_client", fake_client)
        head = await resolve_head("https://github.com/octocat/Hello-World", branch="master")

        assert head.commit_sha == ATOM_SHA
        assert head.ref == "master"
        assert head.request_count == 0
        assert head.commit["message"] == "Fix & stuff"
        assert calls and "commits/master.atom" in calls[0]

    asyncio.run(run())


def test_atom_404_falls_back_to_api_meta_then_atom(monkeypatch) -> None:
    calls: list[str] = []

    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if request.url.host == "github.com":
                if "commits/old-name.atom" in str(request.url):
                    return httpx.Response(404, text="not found")
                return httpx.Response(200, text=_atom_body())
            return httpx.Response(
                200,
                json={"default_branch": "main", "full_name": "octocat/Hello-World", "stargazers_count": 1},
            )

        async def fake_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        monkeypatch.setattr("app.github.get_client", fake_client)
        head = await resolve_head("https://github.com/octocat/Hello-World", branch="old-name")

        assert head.ref == "main"
        assert head.repo_meta is not None
        assert head.repo_meta["full_name"] == "octocat/Hello-World"
        assert head.repo_meta["stars"] == 1
        assert any("/repos/octocat/Hello-World" in url for url in calls)

    asyncio.run(run())


def test_atom_network_failure_falls_back_to_api_commits(monkeypatch) -> None:
    calls: list[str] = []

    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if request.url.host == "github.com":
                raise httpx.ConnectTimeout("atom unreachable", request=request)
            if request.url.path.endswith("/commits/main"):
                return httpx.Response(
                    200,
                    json={
                        "sha": ATOM_SHA,
                        "commit": {
                            "message": "API fallback commit",
                            "author": {"name": "Octo Cat", "date": "2025-01-02T03:04:05Z"},
                        },
                    },
                )
            return httpx.Response(
                200,
                json={"default_branch": "main", "full_name": "octocat/Hello-World"},
            )

        async def fake_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        monkeypatch.setattr("app.github.get_client", fake_client)
        head = await resolve_head("https://github.com/octocat/Hello-World", branch="old-name")

        assert head.commit_sha == ATOM_SHA
        assert head.ref == "main"
        assert head.commit["message"] == "API fallback commit"
        assert head.request_count == 2
        assert any(url.endswith("/commits/main") for url in calls)

    asyncio.run(run())


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
