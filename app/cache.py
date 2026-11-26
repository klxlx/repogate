"""按 commit 寻址的报告缓存：原子写、最近 5 份 LRU、损坏文件跳过。

默认目录 `.cache/reports/<owner>__<repo>/<sha>.json`，不入库。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .version import SCHEMA_VERSION

logger = logging.getLogger("repogate.cache")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_ROOT = PROJECT_ROOT / ".cache" / "reports"
MAX_ENTRIES_PER_REPO = 5
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class CachedReport:
    report: dict
    cached_at: str
    duration_ms: int
    path: Path


def cache_root() -> Path:
    """允许测试通过 REPOGATE_CACHE_DIR 指向临时目录；生产默认项目下 .cache/reports。"""
    override = os.environ.get("REPOGATE_CACHE_DIR", "").strip()
    return Path(override) if override else DEFAULT_CACHE_ROOT


def repo_dir(owner: str, repo: str) -> Path:
    # owner/repo 已由 parse_repo_url 限定字符；这里只挡路径分隔符。
    safe_owner = owner.replace("/", "_").replace("\\", "_")
    safe_repo = repo.replace("/", "_").replace("\\", "_")
    return cache_root() / f"{safe_owner}__{safe_repo}"


def cache_key(owner: str, repo: str, sha: str) -> str:
    return f"{owner}/{repo}@{sha}"


def path_for(owner: str, repo: str, sha: str) -> Path:
    if not _SHA_RE.match(sha):
        raise ValueError(f"invalid commit sha: {sha!r}")
    return repo_dir(owner, repo) / f"{sha}.json"


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _validate(data: object) -> tuple[dict, str, int] | None:
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != SCHEMA_VERSION:
        return None
    report = data.get("report")
    if not isinstance(report, dict):
        return None
    cached_at = data.get("cached_at")
    if not isinstance(cached_at, str) or not cached_at:
        return None
    duration = data.get("duration_ms")
    if not isinstance(duration, int) or duration < 0:
        return None
    return report, cached_at, duration


def _load_path(path: Path, *, touch: bool) -> CachedReport | None:
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    checked = _validate(data)
    if checked is None:
        return None
    report, cached_at, duration_ms = checked
    if touch:
        try:
            os.utime(path, None)
        except OSError:
            pass
    return CachedReport(
        report=report,
        cached_at=cached_at,
        duration_ms=duration_ms,
        path=path,
    )


def load(owner: str, repo: str, sha: str) -> CachedReport | None:
    try:
        path = path_for(owner, repo, sha)
    except ValueError:
        return None
    if not path.is_file():
        return None
    return _load_path(path, touch=True)


def load_latest(owner: str, repo: str) -> CachedReport | None:
    """限流兜底：任意旧 sha 的最新一份；按 mtime 排序，损坏文件跳过。"""
    directory = repo_dir(owner, repo)
    if not directory.is_dir():
        return None
    paths = sorted(
        directory.glob("*.json"),
        key=lambda item: item.stat().st_mtime if item.exists() else 0,
        reverse=True,
    )
    for path in paths:
        cached = _load_path(path, touch=False)
        if cached is not None:
            return cached
    return None


def _prune(owner: str, repo: str) -> None:
    directory = repo_dir(owner, repo)
    paths = sorted(
        directory.glob("*.json"),
        key=lambda item: item.stat().st_mtime if item.exists() else 0,
        reverse=True,
    )
    for path in paths[MAX_ENTRIES_PER_REPO:]:
        try:
            path.unlink()
            logger.info("cache lru evict %s", path)
        except OSError:
            logger.warning("cache lru evict failed %s", path)


def save(owner: str, repo: str, sha: str, report: dict, duration_ms: int) -> str:
    """原子写 tmp + rename；返回 cached_at。失败由上层决定是否降级。"""
    cached_at = _utc_now()
    directory = repo_dir(owner, repo)
    directory.mkdir(parents=True, exist_ok=True)
    target = path_for(owner, repo, sha)

    wrapper = {
        "schema_version": SCHEMA_VERSION,
        "cached_at": cached_at,
        "duration_ms": int(duration_ms),
        "report": report,
    }

    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(directory),
            prefix=f".{sha}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            json.dump(wrapper, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
        tmp_name = None
    finally:
        if tmp_name:
            try:
                Path(tmp_name).unlink()
            except OSError:
                pass

    _prune(owner, repo)
    logger.info("cache saved %s duration_ms=%d", target, duration_ms)
    return cached_at
