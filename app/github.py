"""GitHub 取数：严格 3 次 API 调用（meta + commit + trees），不下载任何文件内容。"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx

from .errors import (
    fetch_failed,
    invalid_url,
    not_found,
    rate_limited,
    timeout,
    upstream,
)
from .models import TimingInfo

logger = logging.getLogger("repogate.github")

GITHUB_API = "https://api.github.com"
MAX_API_REQUESTS = 3
MAX_REQUEST_ATTEMPTS = 4  # 仅 tree 按 commit_sha 失败时，允许回退分支名重试一次
MAX_NETWORK_ATTEMPTS = 3  # 5xx / RemoteProtocolError 共用：首试 + 2 次重试
RETRY_DELAYS_S = (1.0, 3.0)
TREE_SLOW_MS = 45000  # 超过即标记 tree_slow，页面提示「超大仓库」
TREE_HARD_TIMEOUT_MS = 90000  # 硬超时：90 秒仍未返回才报 TIMEOUT
TREE_READ_TIMEOUT_S = 90.0
USER_AGENT = "RepoGate/0.1 (repository intake inspection)"

_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass
class RepoBundle:
    repo: dict
    ref: str
    commit_sha: str
    tree: dict
    tree_ref_fallback: bool
    tree_truncated_by_github: bool
    request_count: int
    timing: TimingInfo


def parse_repo_url(raw: str) -> tuple[str, str]:
    """只接受 https://github.com/{owner}/{repo}，其余一律 INVALID_URL。"""
    text = (raw or "").strip()
    if not text:
        raise invalid_url("仓库地址不能为空")

    parts = urlsplit(text)
    if parts.scheme != "https":
        raise invalid_url("仅支持 https:// 开头的 GitHub 仓库地址")
    if parts.netloc.lower() not in {"github.com", "www.github.com"}:
        raise invalid_url("仅支持 github.com 域名（不支持 GitLab / Gitee 等）")

    path = parts.path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    segments = path.split("/")
    if len(segments) != 2:
        raise invalid_url("地址应形如 https://github.com/{owner}/{repo}")

    owner, repo = segments
    if not _OWNER_RE.match(owner):
        raise invalid_url("owner 名称不合法")
    if not _REPO_RE.match(repo):
        raise invalid_url("repo 名称不合法")

    return owner, repo


class GitHubClient:
    """每实例一次扫描；正常路径 3 次请求，tree 回退是唯一允许的第 4 次。"""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self.request_count = 0
        self.tree_attempts = 0
        self._last_attempts = 0
        self._headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    def _begin_request(self, method: str, url: str, extra: bool = False) -> None:
        if self.request_count >= MAX_API_REQUESTS and not extra:
            raise upstream("内部请求预算超限（单仓库最多 3 次 GitHub 请求）")
        if self.request_count >= MAX_REQUEST_ATTEMPTS:
            raise upstream("内部请求预算超限（tree 回退只允许一次）")
        self.request_count += 1
        logger.info(
            "github request %d/%d: %s %s",
            self.request_count,
            MAX_API_REQUESTS,
            method,
            url,
        )

    def _map_status(self, status: int, headers: httpx.Headers) -> None:
        if status == 404:
            raise not_found()
        if status in (403, 429):
            raise rate_limited(_reset_hhmm(headers))
        if status >= 400:
            raise upstream(f"GitHub 返回 HTTP {status}")

    async def _send(self, url: str) -> tuple[httpx.Response, int]:
        """5xx 与 RemoteProtocolError 重试 2 次（1s → 3s）；超时与 4xx 不重试。"""
        for attempt in range(MAX_NETWORK_ATTEMPTS):
            index = attempt + 1
            self._last_attempts = index
            try:
                response = await self._client.get(
                    url, headers=self._headers, follow_redirects=True
                )
            except httpx.TimeoutException as exc:
                raise timeout() from exc
            except httpx.RemoteProtocolError as exc:
                if index >= MAX_NETWORK_ATTEMPTS:
                    raise fetch_failed(
                        f"网络请求 {index} 次仍失败（RemoteProtocolError）"
                    ) from exc
                logger.warning(
                    "github network retry %d/%d url=%s", index, MAX_NETWORK_ATTEMPTS, url
                )
                await asyncio.sleep(RETRY_DELAYS_S[attempt])
                continue
            except httpx.HTTPError as exc:
                raise upstream(f"网络错误：{exc.__class__.__name__}") from exc

            if response.status_code >= 500:
                if index >= MAX_NETWORK_ATTEMPTS:
                    raise fetch_failed(
                        f"GitHub 连续返回 HTTP {response.status_code}（{index} 次）"
                    )
                logger.warning(
                    "github http %d, retry %d/%d url=%s",
                    response.status_code,
                    index,
                    MAX_NETWORK_ATTEMPTS,
                    url,
                )
                await asyncio.sleep(RETRY_DELAYS_S[attempt])
                continue
            return response, index

        raise fetch_failed("网络请求重试耗尽")

    async def _get_json(self, url: str, *, allow_missing: bool = False, extra: bool = False) -> dict | None:
        self._begin_request("GET", url, extra=extra)
        try:
            response, attempts = await self._send(url)
        finally:
            if "/git/trees/" in url:
                self.tree_attempts += self._last_attempts

        if allow_missing and response.status_code in (404, 422):
            logger.info("tree ref missing status=%d url=%s", response.status_code, url)
            return None

        self._map_status(response.status_code, response.headers)
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise upstream("GitHub 返回内容不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise upstream("GitHub 返回数据结构异常")
        return payload

    async def get_repo(self, owner: str, repo: str) -> dict:
        payload = await self._get_json(f"{GITHUB_API}/repos/{quote(owner)}/{quote(repo)}")
        assert payload is not None
        return payload

    async def get_commit(self, owner: str, repo: str, ref: str) -> dict:
        payload = await self._get_json(
            f"{GITHUB_API}/repos/{quote(owner)}/{quote(repo)}/commits/{quote(ref, safe='')}"
        )
        assert payload is not None
        return payload

    async def get_tree(self, owner: str, repo: str, commit_sha: str, branch: str) -> tuple[dict, bool]:
        """按 commit SHA 取树；404/422 时回退分支名，最多一次。"""
        base = f"{GITHUB_API}/repos/{quote(owner)}/{quote(repo)}/git/trees"
        tree = await self._get_json(
            f"{base}/{quote(commit_sha, safe='')}?recursive=1", allow_missing=True
        )
        if tree is not None:
            return tree, False

        tree = await self._get_json(
            f"{base}/{quote(branch, safe='')}?recursive=1", allow_missing=True, extra=True
        )
        if tree is None:
            raise fetch_failed("commit 与默认分支都无法解析为文件树")
        return tree, True


def _reset_hhmm(headers: httpx.Headers) -> str | None:
    """把 X-RateLimit-Reset（epoch 秒）转成本地 HH:MM；解析失败就不带恢复时间。"""
    raw = headers.get("x-ratelimit-reset")
    if not raw:
        return None
    try:
        timestamp = int(raw)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.datetime.fromtimestamp(timestamp).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return None


def _normalize_repo(meta: dict) -> dict:
    license_obj = meta.get("license") or {}
    return {
        "full_name": meta.get("full_name") or "",
        "stars": int(meta.get("stargazers_count") or 0),
        "language": meta.get("language"),
        "license": license_obj.get("spdx_id") if isinstance(license_obj, dict) else None,
        "archived": bool(meta.get("archived")),
        "pushed_at": meta.get("pushed_at"),
    }


def _elapsed_ms(started_at: float) -> int:
    return int(round((time.monotonic() - started_at) * 1000))


async def fetch_repo_bundle(raw_url: str) -> RepoBundle:
    owner, repo = parse_repo_url(raw_url)

    limits = httpx.Timeout(connect=10.0, read=TREE_READ_TIMEOUT_S, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=limits) as client:
        gh = GitHubClient(client)

        meta_started = time.monotonic()
        meta = await gh.get_repo(owner, repo)
        meta_ms = _elapsed_ms(meta_started)

        branch = meta.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise fetch_failed("无法从仓库元信息中获取默认分支")

        commit_started = time.monotonic()
        commit = await gh.get_commit(owner, repo, branch)
        commit_ms = _elapsed_ms(commit_started)

        sha = commit.get("sha")
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise fetch_failed("无法获取有效的 40 位 commit SHA")

        tree_started = time.monotonic()
        tree, fallback = await gh.get_tree(owner, repo, sha, branch)
        tree_ms = _elapsed_ms(tree_started)

        if tree_ms > TREE_HARD_TIMEOUT_MS:
            raise timeout(f"获取文件树超过 90 秒上限（tree_ms={tree_ms}）")
        tree_slow = tree_ms > TREE_SLOW_MS
        if tree_slow:
            logger.warning(
                "tree request slow tree_ms=%d threshold_ms=%d repo=%s/%s",
                tree_ms,
                TREE_SLOW_MS,
                owner,
                repo,
            )

        entries = tree.get("tree")
        if not isinstance(entries, list):
            raise fetch_failed("文件树结构异常（缺少 tree 数组）")

        logger.info(
            "tree received entries=%d github_truncated=%s ref_fallback=%s",
            len(entries),
            bool(tree.get("truncated")),
            fallback,
        )

        return RepoBundle(
            repo=_normalize_repo(meta),
            ref=branch,
            commit_sha=sha,
            tree=tree,
            tree_ref_fallback=fallback,
            tree_truncated_by_github=bool(tree.get("truncated")),
            request_count=gh.request_count,
            timing=TimingInfo(
                meta_ms=meta_ms,
                commit_ms=commit_ms,
                tree_ms=tree_ms,
                tree_slow=tree_slow,
                tree_attempts=gh.tree_attempts or 1,
            ),
        )
