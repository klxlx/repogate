"""git/trees 清单解析：纯元数据统计，BLOB_CAP 触发时按优先级采样。"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

from .errors import fetch_failed
from .models import FileEntry, ScopeInfo, StatsInfo

BLOB_CAP = 20000
MAX_ANALYZABLE_BYTES = 1024 * 1024  # 单文件分析上限：超过 1MB 的文本不进分析口径

EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git", "node_modules", "vendor", "dist", "build", ".venv", "venv",
        "__pycache__", ".pytest_cache", ".idea", ".vs", "obj",
    }
)

BINARY_EXTENSIONS = frozenset(
    {
        ".bin", ".exe", ".dll", ".so", ".dylib", ".a", ".lib", ".obj", ".o",
        ".pdb", ".class", ".pyc", ".pyo", ".wasm", ".node", ".dat", ".db",
        ".sqlite", ".sqlite3", ".mdb", ".pak", ".blend", ".fbx", ".glb",
        ".ttf", ".otf", ".woff", ".woff2", ".eot", ".pdf",
        ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    }
)
MEDIA_EXTENSIONS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tif", ".tiff",
        ".svg", ".mp4", ".avi", ".mov", ".mkv", ".webm", ".wmv", ".flv", ".m4v",
        ".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a",
    }
)
ARCHIVE_EXTENSIONS = frozenset(
    {
        ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".lz4", ".7z",
        ".rar", ".whl", ".egg", ".nupkg", ".vsix", ".jar",
    }
)
WINDOWS_SCRIPT_EXTENSIONS = frozenset({".bat", ".cmd", ".ps1", ".vbs"})

CONFIG_EXTENSIONS = frozenset({".yaml", ".yml", ".toml", ".ini", ".json", ".conf"})
SCRIPT_EXTENSIONS = frozenset({".bat", ".cmd", ".ps1", ".vbs", ".sh", ".mk"})
SOURCE_EXTENSIONS = frozenset(
    {
        ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".go",
        ".rs", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".cs", ".rb",
        ".php", ".swift", ".kt", ".kts", ".scala", ".m", ".mm", ".lua", ".r",
        ".jl", ".dart", ".vue", ".svelte", ".sql", ".proto", ".gradle",
        ".groovy", ".pl", ".pm", ".ex", ".exs", ".erl", ".hrl", ".fs", ".fsx",
        ".vb", ".asm", ".s",
    }
)


@dataclass(frozen=True)
class ScanLimits:
    blob_cap: int = BLOB_CAP


DEFAULT_LIMITS = ScanLimits()


@dataclass
class TreeScanResult:
    scope: ScopeInfo
    stats: StatsInfo
    files: list[FileEntry]


def _extension(path: str) -> str:
    return posixpath.splitext(path)[1].lower()


def is_safe_path(path: str) -> bool:
    if not path or path.startswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _is_textizable(path: str) -> bool:
    ext = _extension(path)
    return ext not in BINARY_EXTENSIONS and ext not in MEDIA_EXTENSIONS and ext not in ARCHIVE_EXTENSIONS


def _priority(path: str) -> int:
    """采样优先级：1 最高，5 最低。数字小者先入清单。"""
    name = path.rsplit("/", 1)[-1].lower()
    ext = _extension(path)

    if name.startswith(".env") or ext in CONFIG_EXTENSIONS:
        return 1
    if ext in SCRIPT_EXTENSIONS or name == "makefile" or name == "dockerfile" or name.startswith("dockerfile."):
        return 2
    if (
        name.startswith("readme")
        or name.startswith("license")
        or (name.startswith("requirements") and ext == ".txt")
        or name in {"package.json", "pyproject.toml", "go.mod", "pom.xml"}
    ):
        return 3
    if ext in SOURCE_EXTENSIONS:
        return 4
    return 5


def is_excluded_dir(path: str) -> bool:
    """路径中任一目录段命中排除名单（最后一段是文件名，不参与）。"""
    return any(part in EXCLUDED_DIR_NAMES for part in path.split("/")[:-1])


def _is_analyzable(item: dict) -> bool:
    """分析口径（v1.5 冻结）：文本、≤1MB、路径不含排除目录段。"""
    return (
        not is_excluded_dir(item["path"])
        and _is_textizable(item["path"])
        and 0 <= _size(item) <= MAX_ANALYZABLE_BYTES
    )


def _size(item: dict) -> int:
    return int(item.get("size") or 0)


def _count_windows_scripts(entries: list[dict]) -> int:
    count = 0
    for item in entries:
        if item.get("type") != "blob":
            continue
        path = item.get("path")
        if isinstance(path, str) and _extension(path) in WINDOWS_SCRIPT_EXTENSIONS:
            count += 1
    return count


def _sample_by_priority(entries: list[dict], cap: int) -> list[dict]:
    buckets: dict[int, list[dict]] = {1: [], 2: [], 3: [], 4: [], 5: []}
    for item in entries:
        buckets[_priority(item["path"])].append(item)

    selected: list[dict] = []
    for priority in (1, 2, 3, 4, 5):
        bucket = sorted(buckets[priority], key=lambda item: item["path"])
        for item in bucket:
            if len(selected) >= cap:
                return selected
            selected.append(item)
    return selected


def scan_tree(
    tree_payload: dict,
    limits: ScanLimits = DEFAULT_LIMITS,
    github_truncated: bool = False,
) -> TreeScanResult:
    entries = tree_payload.get("tree")
    if not isinstance(entries, list):
        raise fetch_failed("文件树结构异常（缺少 tree 数组）")

    blobs = [
        item
        for item in entries
        if isinstance(item, dict)
        and item.get("type") == "blob"
        and isinstance(item.get("path"), str)
        and is_safe_path(item["path"])
    ]
    total_blobs = len(blobs)

    if total_blobs > limits.blob_cap:
        selected = _sample_by_priority(blobs, limits.blob_cap)
        truncated = True
        reason = "github_tree_limit" if github_truncated else "blob_cap"
    else:
        selected = blobs
        truncated = github_truncated
        reason = "github_tree_limit" if github_truncated else "none"

    listed = selected
    analyzable = [item for item in listed if _is_analyzable(item)]
    files_scanned = len(analyzable)
    files_skipped = len(listed) - files_scanned
    unlisted_blobs = total_blobs - len(listed)

    # 冻结口径的两个恒等式：进清单 = 纳入分析 + 被跳过；全树 = 进清单 + 没看到
    assert files_scanned + files_skipped == len(listed)
    assert len(listed) + unlisted_blobs == total_blobs

    scope = ScopeInfo(
        files_scanned=files_scanned,
        files_skipped=files_skipped,
        bytes_scanned=sum(_size(item) for item in analyzable),
        truncated=truncated,
        truncate_reason=reason,
    )
    stats = StatsInfo(
        text_files=len(analyzable),
        windows_script_count=_count_windows_scripts(entries),
        total_blobs_in_tree=total_blobs,
        listed_blobs=len(listed),
        bytes_total_listed=sum(_size(item) for item in listed),
        unlisted_blobs=unlisted_blobs,
    )

    display_files = [
        FileEntry(path=item["path"], size=_size(item), is_text=True)
        for item in analyzable
    ]
    display_files.sort(key=lambda item: item.path)
    return TreeScanResult(scope=scope, stats=stats, files=display_files[:50])
