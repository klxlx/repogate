from __future__ import annotations

import asyncio

import httpx

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


def test_parse_repo_url_accepts_git_suffix_and_trailing_slash() -> None:
    assert parse_repo_url("https://github.com/psf/requests.git") == ("psf", "requests")
    assert parse_repo_url("https://github.com/psf/requests/") == ("psf", "requests")


def test_parse_repo_url_rejects_non_github() -> None:
    import pytest

    from app.errors import ScanError

    with pytest.raises(ScanError) as caught:
        parse_repo_url("https://gitlab.com/owner/repo")

    assert caught.value.code == "INVALID_URL"
