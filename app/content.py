"""raw.githubusercontent 内容读取层：只读少量文件，失败降级不崩。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import quote

import httpx

RAW_BASE = "https://raw.githubusercontent.com"
MAX_FILES = 12
MAX_TOTAL_BYTES = 3 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024
CONCURRENCY = 4
REQUEST_TIMEOUT_S = 8.0

logger = logging.getLogger("repogate.content")


@dataclass
class ContentResult:
    contents: dict[str, str]
    failed: frozenset[str]
    requests: int
    bytes: int


def _raw_url(owner: str, repo: str, commit_sha: str, path: str) -> str:
    quoted_path = "/".join(quote(segment, safe="") for segment in path.split("/"))
    return f"{RAW_BASE}/{quote(owner)}/{quote(repo)}/{commit_sha}/{quoted_path}"


def _select(wanted: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """按文件数、单文件、总字节三道配额裁剪；超限文件直接判不可用。"""
    selected: list[tuple[str, int]] = []
    budget = MAX_TOTAL_BYTES
    for path, size in wanted:
        if len(selected) >= MAX_FILES:
            break
        if size > MAX_FILE_BYTES or size > budget:
            continue
        selected.append((path, size))
        budget -= size
    return selected


async def fetch_contents(
    owner: str,
    repo: str,
    commit_sha: str,
    wanted: list[tuple[str, int]],
    transport: httpx.AsyncBaseTransport | None = None,
) -> ContentResult:
    selected = _select(wanted)
    contents: dict[str, str] = {}
    failed: set[str] = set()
    request_count = 0
    byte_count = 0

    timeout = httpx.Timeout(REQUEST_TIMEOUT_S, connect=5.0)
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, transport=transport
    ) as client:

        async def one(path: str) -> None:
            nonlocal request_count, byte_count
            async with semaphore:
                request_count += 1
                url = _raw_url(owner, repo, commit_sha, path)
                try:
                    response = await client.get(url)
                except httpx.TimeoutException:
                    failed.add(path)
                    logger.warning("content timeout path=%s", path)
                    return
                except httpx.HTTPError as exc:
                    failed.add(path)
                    logger.warning("content error path=%s err=%s", path, exc.__class__.__name__)
                    return
                if response.status_code != 200:
                    failed.add(path)
                    logger.warning("content http %d path=%s", response.status_code, path)
                    return
                body = response.content
                byte_count += len(body)
                contents[path] = body.decode("utf-8", errors="replace")

        await asyncio.gather(*(one(path) for path, _ in selected))

    return ContentResult(
        contents=contents,
        failed=frozenset(failed),
        requests=request_count,
        bytes=byte_count,
    )
