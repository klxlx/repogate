"""git/trees 清单解析：只读元数据、不下载内容、限额可注入便于测试。"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

from .errors import fetch_failed
from .models import FileEntry, ScopeInfo, StatsInfo

MAX_FILES = 2000
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 1 * 1024 * 1024

EXCLUDED_DIR_NAMES = frozenset({".git", "node_modules", "vendor", "dist", "build", ".venv"})

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


@dataclass(frozen=True)
class ScanLimits:
    max_files: int = MAX_FILES
    max_total_bytes: int = MAX_TOTAL_BYTES
    max_file_bytes: int = MAX_FILE_BYTES


DEFAULT_LIMITS = ScanLimits()


@dataclass
class TreeScanResult:
    scope: ScopeInfo
    stats: StatsInfo
    files: list[FileEntry]


def _extension(path: str) -> str:
    return posixpath.splitext(path)[1].lower()


def _is_safe_path(path: str) -> bool:
    if not path or path.startswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _is_excluded_dir(path: str) -> bool:
    parts = path.split("/")
    return any(part in EXCLUDED_DIR_NAMES for part in parts[:-1])


def _count_windows_scripts(entries: list[dict]) -> int:
    """按仓库全量 blob 统计脚本数，不受目录/大小过滤影响，供验收对照。"""
    count = 0
    for item in entries:
        if item.get("type") != "blob":
            continue
        path = item.get("path")
        if isinstance(path, str) and _extension(path) in WINDOWS_SCRIPT_EXTENSIONS:
            count += 1
    return count


def scan_tree(tree_payload: dict, limits: ScanLimits = DEFAULT_LIMITS) -> TreeScanResult:
    entries = tree_payload.get("tree")
    if not isinstance(entries, list):
        raise fetch_failed("文件树结构异常（缺少 tree 数组）")

    scope = ScopeInfo()
    stats = StatsInfo(windows_script_count=_count_windows_scripts(entries))
    files: list[FileEntry] = []

    for item in entries:
        if not isinstance(item, dict) or item.get("type") != "blob":
            continue

        path = item.get("path")
        size = int(item.get("size") or 0)
        if not isinstance(path, str) or not _is_safe_path(path):
            scope.files_skipped += 1
            continue

        if _is_excluded_dir(path):
            scope.files_skipped += 1
            continue

        ext = _extension(path)
        if ext in ARCHIVE_EXTENSIONS or ext in MEDIA_EXTENSIONS or ext in BINARY_EXTENSIONS:
            scope.files_skipped += 1
            continue

        if size > limits.max_file_bytes:
            scope.files_skipped += 1
            continue

        if scope.files_scanned >= limits.max_files:
            scope.truncated = True
            scope.truncate_reason = f"文件数超过 {limits.max_files} 上限，已停止遍历"
            break

        if scope.bytes_scanned + size > limits.max_total_bytes:
            scope.truncated = True
            scope.truncate_reason = (
                f"文件总大小超过 {limits.max_total_bytes // (1024 * 1024)}MB 上限，已停止遍历"
            )
            break

        scope.files_scanned += 1
        scope.bytes_scanned += size
        stats.text_files += 1
        files.append(FileEntry(path=path, size=size, is_text=True))

    ordered = sorted(files, key=lambda item: item.path)
    return TreeScanResult(scope=scope, stats=stats, files=ordered[:50])
