"""GitHub 取数：严格 3 次 API 调用（meta + commit + zipball），不 clone、不逐文件。"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx

from .errors import ScanError, invalid_url, not_found, rate_limited, timeout, upstream

logger = logging.getLogger("repogate.github")

GITHUB_API = "https://api.github.com"
MAX_API_REQUESTS = 3
MAX_ZIP_BYTES = 128 * 1024 * 1024  # 防 OOM 的下载硬顶，正常仓库远低于此值
MAX_ZIP_SECONDS = 180  # 防大仓库把请求挂死；超时按 truncated 返回，不 500
USER_AGENT = "RepoGate/0.1 (repository intake inspection)"

_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass
class RepoBundle:
    repo: dict
    ref: str
    commit_sha: str
    zip_bytes: bytes | None
    truncated: bool
    truncate_reason: str | None
    requests_used: int


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
    """每实例一次扫描，请求计数只增不减，任何越界调用立即失败。"""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self.request_count = 0
        self._headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _begin_request(self, method: str, url: str) -> None:
        if self.request_count >= MAX_API_REQUESTS:
            raise upstream("内部请求预算超限（单仓库最多 3 次 GitHub 请求）")
        self.request_count += 1
        logger.info(
            "github request %d/%d: %s %s",
            self.request_count,
            MAX_API_REQUESTS,
            method,
            url,
        )

    def _map_status(self, status: int) -> None:
        if status == 404:
            raise not_found()
        if status in (403, 429):
            raise rate_limited()
        if status >= 400:
            raise upstream(f"GitHub 返回 HTTP {status}")

    async def _get_json(self, url: str) -> dict:
        self._begin_request("GET", url)
        try:
            response = await self._client.get(url, headers=self._headers, follow_redirects=True)
        except httpx.TimeoutException as exc:
            raise timeout() from exc
        except httpx.HTTPError as exc:
            raise upstream(f"网络错误：{exc.__class__.__name__}") from exc

        self._map_status(response.status_code)
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise upstream("GitHub 返回内容不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise upstream("GitHub 返回数据结构异常")
        return payload

    async def get_repo(self, owner: str, repo: str) -> dict:
        return await self._get_json(f"{GITHUB_API}/repos/{quote(owner)}/{quote(repo)}")

    async def get_commit(self, owner: str, repo: str, ref: str) -> dict:
        return await self._get_json(
            f"{GITHUB_API}/repos/{quote(owner)}/{quote(repo)}/commits/{quote(ref, safe='')}"
        )

    async def download_zipball(self, owner: str, repo: str, commit_sha: str) -> tuple[bytes | None, bool, str | None]:
        """按 commit SHA 取 zipball，保证报告锚点与内容同源；流式写入内存。"""
        url = f"{GITHUB_API}/repos/{quote(owner)}/{quote(repo)}/zipball/{quote(commit_sha, safe='')}"
        self._begin_request("GET", url)

        buffer = bytearray()
        truncated = False
        reason: str | None = None
        started_at = time.monotonic()
        next_progress = 10 * 1024 * 1024
        try:
            async with self._client.stream(
                "GET", url, headers=self._headers, follow_redirects=True
            ) as response:
                self._map_status(response.status_code)
                async for chunk in response.aiter_bytes():
                    if len(buffer) + len(chunk) > MAX_ZIP_BYTES:
                        truncated = True
                        reason = f"压缩包超过 {MAX_ZIP_BYTES // (1024 * 1024)}MB 下载上限，已停止下载"
                        break
                    if time.monotonic() - started_at > MAX_ZIP_SECONDS:
                        truncated = True
                        reason = f"压缩包下载超过 {MAX_ZIP_SECONDS} 秒上限，已停止下载（快照不完整）"
                        break
                    buffer.extend(chunk)
                    if len(buffer) >= next_progress:
                        logger.info(
                            "zipball progress bytes=%d elapsed=%.1fs",
                            len(buffer),
                            time.monotonic() - started_at,
                        )
                        next_progress += 10 * 1024 * 1024
        except httpx.TimeoutException as exc:
            raise timeout("下载仓库压缩包超时，请稍后重试") from exc
        except httpx.HTTPError as exc:
            raise upstream(f"下载仓库压缩包失败：{exc.__class__.__name__}") from exc

        logger.info(
            "zipball downloaded bytes=%d truncated=%s elapsed=%.1fs",
            len(buffer),
            truncated,
            time.monotonic() - started_at,
        )
        if truncated:
            return None, True, reason
        return bytes(buffer), False, None


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


async def fetch_repo_bundle(raw_url: str) -> RepoBundle:
    owner, repo = parse_repo_url(raw_url)

    limits = httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=limits) as client:
        gh = GitHubClient(client)

        meta = await gh.get_repo(owner, repo)
        branch = meta.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise upstream("无法从仓库元信息中获取默认分支")

        commit = await gh.get_commit(owner, repo, branch)
        sha = commit.get("sha")
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise upstream("无法获取有效的 40 位 commit SHA")

        zip_bytes, truncated, reason = await gh.download_zipball(owner, repo, sha)
        return RepoBundle(
            repo=_normalize_repo(meta),
            ref=branch,
            commit_sha=sha,
            zip_bytes=zip_bytes,
            truncated=truncated,
            truncate_reason=reason,
            requests_used=gh.request_count,
        )
