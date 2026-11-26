from __future__ import annotations

import asyncio

import httpx

from app.content import MAX_FILE_BYTES, MAX_FILES, ContentResult, fetch_contents

SHA = "a" * 40


def transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "missing.md":
            return httpx.Response(404)
        if name == "boom.txt":
            raise httpx.ConnectError("boom")
        return httpx.Response(200, text="hello")

    return httpx.MockTransport(handler)


def run(paths: list[tuple[str, int]]) -> ContentResult:
    return asyncio.run(fetch_contents("owner", "repo", SHA, paths, transport=transport()))


def test_reads_successful_files() -> None:
    result = run([("README.md", 10)])

    assert result.requests == 1
    assert result.bytes == 5
    assert result.contents == {"README.md": "hello"}
    assert result.failed == frozenset()


def test_marks_http_error_as_failed() -> None:
    result = run([("missing.md", 10)])

    assert result.requests == 1
    assert result.contents == {}
    assert result.failed == frozenset({"missing.md"})


def test_marks_network_error_as_failed() -> None:
    result = run([("boom.txt", 10)])

    assert result.requests == 1
    assert result.failed == frozenset({"boom.txt"})


def test_skips_oversized_file_without_request() -> None:
    result = run([("big.txt", MAX_FILE_BYTES + 1)])

    assert result.requests == 0
    assert result.contents == {}


def test_caps_file_count() -> None:
    paths = [(f"file{index}.txt", 1) for index in range(MAX_FILES + 3)]

    result = run(paths)

    assert result.requests == MAX_FILES
    assert len(result.contents) == MAX_FILES


def flaky_transport(mode: str) -> httpx.MockTransport:
    state = {"attempts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["attempts"] += 1
        if state["attempts"] == 1:
            if mode == "timeout":
                raise httpx.ReadTimeout("boom", request=request)
            return httpx.Response(503)
        return httpx.Response(200, text="ok")

    return httpx.MockTransport(handler)


def test_retries_once_on_timeout() -> None:
    result = asyncio.run(
        fetch_contents(
            "owner", "repo", SHA, [("README.md", 10)], transport=flaky_transport("timeout")
        )
    )

    assert result.requests == 2
    assert result.contents == {"README.md": "ok"}
    assert result.failed == frozenset()


def test_retries_once_on_5xx() -> None:
    result = asyncio.run(
        fetch_contents(
            "owner", "repo", SHA, [("README.md", 10)], transport=flaky_transport("5xx")
        )
    )

    assert result.requests == 2
    assert result.contents == {"README.md": "ok"}


def test_timeout_twice_is_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("boom", request=request)

    result = asyncio.run(
        fetch_contents(
            "owner", "repo", SHA, [("README.md", 10)], transport=httpx.MockTransport(handler)
        )
    )

    assert result.requests == 2
    assert result.failed == frozenset({"README.md"})
