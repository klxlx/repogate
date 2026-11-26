"""zipball 内存解析：只读快照、跳过规则明确、限额可注入便于测试。"""

from __future__ import annotations

import io
import posixpath
import zipfile
from dataclasses import dataclass
from typing import Iterable

from .errors import upstream
from .models import FileEntry, ScopeInfo

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


@dataclass(frozen=True)
class ScanLimits:
    max_files: int = MAX_FILES
    max_total_bytes: int = MAX_TOTAL_BYTES
    max_file_bytes: int = MAX_FILE_BYTES


DEFAULT_LIMITS = ScanLimits()


@dataclass
class ScanResult:
    scope: ScopeInfo
    files: list[FileEntry]


def _root_prefix(zf: zipfile.ZipFile) -> str:
    """识别 GitHub zipball 的 {owner}-{repo}-{sha}/ 包装层，避免它进入相对路径。"""
    names = [info.filename.replace("\\", "/") for info in zf.infolist() if not info.is_dir()]
    if not names:
        return ""
    first_segments = {name.split("/")[0] for name in names}
    if len(first_segments) != 1:
        return ""
    if any(name.count("/") == 0 for name in names):
        return ""
    return next(iter(first_segments)) + "/"


def _relative_path(filename: str, prefix: str) -> str | None:
    normalized = filename.replace("\\", "/")
    if prefix and not normalized.startswith(prefix):
        return None
    rel = normalized[len(prefix):] if prefix else normalized
    if not rel:
        return None
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    if rel.startswith("/"):
        return None
    return rel


def _extension(name: str) -> str:
    return posixpath.splitext(name)[1].lower()


def _skip_by_name(rel: str, size: int, limits: ScanLimits) -> str | None:
    parts = rel.split("/")
    if any(part in EXCLUDED_DIR_NAMES for part in parts[:-1]):
        return "excluded_dir"
    ext = _extension(parts[-1])
    if ext in ARCHIVE_EXTENSIONS:
        return "archive"
    if ext in MEDIA_EXTENSIONS:
        return "media"
    if ext in BINARY_EXTENSIONS:
        return "binary_ext"
    if size > limits.max_file_bytes:
        return "too_large"
    return None


def _is_text_bytes(data: bytes) -> bool:
    if not data:
        return True
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _presentation_sample(files: Iterable[FileEntry], limit: int = 50) -> list[FileEntry]:
    """清单抽样：根级文件全出，其余按顶层目录轮转，最多 limit 条。

    契约只要求“前 50 条”，未固定排序；这里优先保证 README / setup.py / LICENSE
    这类根级关键文件与各顶层目录的代表性，同一输入顺序恒定。
    """
    ordered: list[FileEntry] = []
    root_files = sorted(
        (item for item in files if "/" not in item.path), key=lambda item: item.path
    )
    ordered.extend(root_files[:limit])

    grouped: dict[str, list[FileEntry]] = {}
    for item in files:
        if "/" not in item.path:
            continue
        grouped.setdefault(item.path.split("/")[0], []).append(item)

    buckets = [sorted(grouped[name], key=lambda item: item.path) for name in sorted(grouped)]
    index = 0
    while len(ordered) < limit:
        moved = False
        for bucket in buckets:
            if index >= len(bucket):
                continue
            ordered.append(bucket[index])
            moved = True
            if len(ordered) >= limit:
                break
        if not moved:
            break
        index += 1

    return ordered


def scan_zip(zip_bytes: bytes, limits: ScanLimits = DEFAULT_LIMITS) -> ScanResult:
    """遍历 zipball 条目，按契约统计 scanned / skipped / bytes 与截断原因。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise upstream("仓库压缩包无法解析（不是有效 zip）") from exc

    scope = ScopeInfo()
    files: list[FileEntry] = []

    try:
        prefix = _root_prefix(zf)
        for info in zf.infolist():
            if info.is_dir():
                continue

            rel = _relative_path(info.filename, prefix)
            if rel is None:
                scope.files_skipped += 1
                continue

            skip_reason = _skip_by_name(rel, info.file_size, limits)
            if skip_reason is not None:
                scope.files_skipped += 1
                continue

            if scope.files_scanned >= limits.max_files:
                scope.truncated = True
                scope.truncate_reason = f"文件数超过 {limits.max_files} 上限，已停止扫描"
                break

            if scope.bytes_scanned + info.file_size > limits.max_total_bytes:
                scope.truncated = True
                scope.truncate_reason = (
                    f"解压总量超过 {limits.max_total_bytes // (1024 * 1024)}MB 上限，已停止扫描"
                )
                break

            try:
                data = zf.read(info)
            except (RuntimeError, zipfile.BadZipFile, OSError):
                scope.files_skipped += 1
                continue

            if scope.bytes_scanned + len(data) > limits.max_total_bytes:
                scope.truncated = True
                scope.truncate_reason = (
                    f"解压总量超过 {limits.max_total_bytes // (1024 * 1024)}MB 上限，已停止扫描"
                )
                break

            if not _is_text_bytes(data):
                scope.files_skipped += 1
                continue

            scope.files_scanned += 1
            scope.bytes_scanned += len(data)
            files.append(FileEntry(path=rel, size=len(data), is_text=True))
    finally:
        zf.close()

    # 同一 commit 无论 zip 条目顺序如何，清单输出保持确定性，保证两次扫描一致。
    return ScanResult(scope=scope, files=_presentation_sample(files))
