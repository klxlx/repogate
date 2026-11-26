"""GitHub 取数：HEAD 走免配额 atom，内容路径为 meta + trees。

v3.2 拆成两步：
- resolve_head：默认分支用 atom feed 解析（1 次非 API 请求），用于命中 commit 缓存；
- fetch_repo_bundle：缓存未命中时补元信息并按 commit SHA 拉文件树。
"""

from __future__ import annotations

import asyncio
import datetime
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
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
MAX_API_REQUESTS = 2  # HEAD 走 atom；首次扫描只允许 meta + tree 两次 API，tree 分支回退另计
MAX_REQUEST_ATTEMPTS = 4  # 仅 tree 按 commit_sha 失败时，允许回退分支名重试一次
MAX_NETWORK_ATTEMPTS = 3  # 5xx / RemoteProtocolError 共用：首试 + 2 次重试
RETRY_DELAYS_S = (1.0, 3.0)
TREE_SLOW_MS = 45000  # 超过即标记 tree_slow，页面提示「超大仓库」
TREE_HARD_TIMEOUT_MS = 90000  # 硬超时：90 秒仍未返回才报 TIMEOUT
TREE_READ_TIMEOUT_S = 90.0
USER_AGENT = "RepoGate/3.2 (repository intake inspection)"
DEFAULT_TIMEOUT = httpx.Timeout(
    connect=10.0, read=TREE_READ_TIMEOUT_S, write=30.0, pool=10.0
)
GITHUB_WEB = "https://github.com"
_ATOM_ENTRY_RE = re.compile(r"<entry\b.*?</entry>", re.IGNORECASE | re.DOTALL)
_ATOM_SHA_RE = re.compile(r"Grit::Commit/([0-9a-fA-F]{40})")
_ATOM_TITLE_RE = re.compile(r"<title>\s*(.*?)\s*</title>", re.IGNORECASE | re.DOTALL)
_ATOM_AUTHOR_RE = re.compile(
    r"<author>\s*<name>\s*(.*?)\s*</name>", re.IGNORECASE | re.DOTALL
)
_ATOM_UPDATED_RE = re.compile(r"<updated>\s*(.*?)\s*</updated>", re.IGNORECASE | re.DOTALL)

_client: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    """复用连接池，避免每次请求重做 TLS 握手；进程退出时由 lifespan 关闭。"""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None

_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class AtomUnavailable(Exception):
    """atom feed 不可用（404/5xx/解析失败）；上层可回落 API 元信息。"""


@dataclass
class RepoHead:
    """默认分支 HEAD 的轻量解析结果；缓存判断只需要它。"""

    owner: str
    repo: str
    commit_sha: str
    commit: dict
    repo_meta: dict | None = None
    ref: str = ""
    request_count: int = 0
    timing: TimingInfo = field(default_factory=TimingInfo)


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
    commit: dict = field(default_factory=dict)
    owner: str = ""
    repo_name: str = ""


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
    """每实例一次请求阶段；正常单仓库 2 次 API（meta + tree）+ 1 次 atom。"""

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
            raise upstream("内部请求预算超限（单仓库最多 2 次 GitHub API 请求）")
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
            reset_hhmm, retry_after = _rate_info(headers)
            raise rate_limited(reset_hhmm, retry_after)
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

    async def _get_json(
        self, url: str, *, allow_missing: bool = False, extra: bool = False
    ) -> dict | None:
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

    async def get_tree(
        self, owner: str, repo: str, commit_sha: str, branch: str
    ) -> tuple[dict, bool]:
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


def _rate_info(headers: httpx.Headers) -> tuple[str | None, int | None]:
    """X-RateLimit-Reset（epoch 秒）→（本地 HH:MM, 剩余秒数）。"""
    raw = headers.get("x-ratelimit-reset")
    retry_after: int | None = None
    reset_hhmm: str | None = None

    if raw:
        try:
            timestamp = int(raw)
            retry_after = max(0, int(timestamp - time.time()))
            reset_hhmm = datetime.datetime.fromtimestamp(timestamp).strftime("%H:%M")
        except (TypeError, ValueError, OverflowError, OSError):
            retry_after = None

    if retry_after is None:
        retry_header = headers.get("retry-after")
        if retry_header:
            try:
                retry_after = max(0, int(retry_header))
            except (TypeError, ValueError):
                retry_after = None

    return reset_hhmm, retry_after


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


def _normalize_commit(payload: dict) -> dict:
    """把 GitHub commits 响应压成契约里的 commit{sha,message,author,date}。"""
    inner = payload.get("commit") if isinstance(payload.get("commit"), dict) else {}
    author = inner.get("author") if isinstance(inner.get("author"), dict) else {}
    committer = inner.get("committer") if isinstance(inner.get("committer"), dict) else {}
    login = payload.get("author") if isinstance(payload.get("author"), dict) else {}
    return {
        "sha": payload.get("sha") or "",
        "message": inner.get("message") or payload.get("message") or "",
        "author": author.get("name") or login.get("login") or "",
        "date": author.get("date") or committer.get("date") or "",
    }


def _atom_text(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    if not match:
        return ""
    return html.unescape(re.sub(r"\s+", " ", match.group(1)).strip())


def _atom_commit(sha: str, entry: str) -> dict:
    return {
        "sha": sha,
        "message": _atom_text(_ATOM_TITLE_RE, entry),
        "author": _atom_text(_ATOM_AUTHOR_RE, entry),
        "date": _atom_text(_ATOM_UPDATED_RE, entry),
    }


async def _head_from_atom(
    owner: str,
    repo: str,
    branch: str,
    *,
    meta: dict | None = None,
    meta_ms: int = 0,
    request_count: int = 0,
) -> RepoHead:
    """用 commits/{branch}.atom 解析 HEAD；不消耗 api.github.com 配额。"""
    client = await get_client()
    url = (
        f"{GITHUB_WEB}/{quote(owner)}/{quote(repo)}/commits/"
        f"{quote(branch, safe='/')}.atom"
    )
    started = time.monotonic()
    try:
        response = await client.get(
            url,
            headers={"Accept": "application/atom+xml,application/xml", "User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise AtomUnavailable(f"atom 网络错误：{exc.__class__.__name__}") from exc

    if response.status_code != 200:
        raise AtomUnavailable(f"atom HTTP {response.status_code}")

    entry_match = _ATOM_ENTRY_RE.search(response.text)
    if entry_match is None:
        raise AtomUnavailable("atom 无 entry")
    entry = entry_match.group(0)
    sha_match = _ATOM_SHA_RE.search(entry)
    if sha_match is None or not _SHA_RE.match(sha_match.group(1).lower()):
        raise AtomUnavailable("atom 无有效 40 位 SHA")
    sha = sha_match.group(1).lower()

    return RepoHead(
        owner=owner,
        repo=repo,
        commit_sha=sha,
        commit=_atom_commit(sha, entry),
        repo_meta=_normalize_repo(meta) if meta is not None else None,
        ref=branch,
        request_count=request_count,
        timing=TimingInfo(meta_ms=meta_ms, commit_ms=_elapsed_ms(started)),
    )


def _elapsed_ms(started_at: float) -> int:
    return int(round((time.monotonic() - started_at) * 1000))


async def resolve_head(raw_url: str, *, branch: str | None = None) -> RepoHead:
    """解析默认分支 HEAD；优先 atom（免 API 配额），404/不可用时回落一次元信息。

    `branch` 可以从报告缓存里的 `repo.default_branch` 传入；传入时缓存命中
    路径不会请求 api.github.com。
    """
    owner, repo = parse_repo_url(raw_url)

    if branch:
        try:
            return await _head_from_atom(owner, repo, branch)
        except AtomUnavailable:
            logger.warning("atom unavailable branch=%s repo=%s/%s; fallback meta", branch, owner, repo)

    client = await get_client()
    gh = GitHubClient(client)
    meta_started = time.monotonic()
    meta = await gh.get_repo(owner, repo)
    meta_ms = _elapsed_ms(meta_started)
    new_branch = meta.get("default_branch")
    if not isinstance(new_branch, str) or not new_branch:
        raise fetch_failed("无法从仓库元信息中获取默认分支")

    try:
        return await _head_from_atom(
            owner,
            repo,
            new_branch,
            meta=meta,
            meta_ms=meta_ms,
            request_count=gh.request_count,
        )
    except AtomUnavailable as exc:
        raise fetch_failed(f"commits atom 不可用且 API 元信息无法解析 HEAD：{exc}") from exc


async def fetch_repo_bundle(raw_url: str, head: RepoHead | None = None) -> RepoBundle:
    """拿到 HEAD 后补齐元信息并按 commit SHA 拉树；缓存命中时上层不会调用。"""
    if head is None:
        head = await resolve_head(raw_url)

    client = await get_client()
    gh = GitHubClient(client)

    meta_ms = head.timing.meta_ms
    if head.repo_meta is None:
        meta_started = time.monotonic()
        meta = await gh.get_repo(head.owner, head.repo)
        meta_ms = _elapsed_ms(meta_started)
        branch = meta.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise fetch_failed("无法从仓库元信息中获取默认分支")
        head.repo_meta = _normalize_repo(meta)
        head.ref = branch

    tree_started = time.monotonic()
    tree, fallback = await gh.get_tree(
        head.owner, head.repo, head.commit_sha, head.ref
    )
    tree_ms = _elapsed_ms(tree_started)

    if tree_ms > TREE_HARD_TIMEOUT_MS:
        raise timeout(f"获取文件树超过 90 秒上限（tree_ms={tree_ms}）")
    tree_slow = tree_ms > TREE_SLOW_MS
    if tree_slow:
        logger.warning(
            "tree request slow tree_ms=%d threshold_ms=%d repo=%s/%s",
            tree_ms,
            TREE_SLOW_MS,
            head.owner,
            head.repo,
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
        repo=head.repo_meta or {},
        ref=head.ref,
        commit_sha=head.commit_sha,
        tree=tree,
        tree_ref_fallback=fallback,
        tree_truncated_by_github=bool(tree.get("truncated")),
        request_count=head.request_count + gh.request_count,
        timing=TimingInfo(
            meta_ms=meta_ms,
            commit_ms=head.timing.commit_ms,
            tree_ms=tree_ms,
            tree_slow=tree_slow,
            tree_attempts=gh.tree_attempts or 1,
        ),
        commit=head.commit,
        owner=head.owner,
        repo_name=head.repo,
    )
