from __future__ import annotations

import pytest

from app.errors import ScanError
from app.treescan import ScanLimits, scan_tree


def blob(path: str, size: int = 10) -> dict:
    return {"path": path, "type": "blob", "size": size, "sha": "x"}


def tree(path: str) -> dict:
    return {"path": path, "type": "tree", "sha": "x"}


def make_tree(items: list[dict], truncated: bool = False) -> dict:
    return {"sha": "root", "tree": items, "truncated": truncated, "url": "https://api.github.com/x"}


def test_scans_blobs_and_sorts_paths() -> None:
    payload = make_tree([blob("README.md", 13), blob("src/app.py", 9), tree("src")])

    result = scan_tree(payload)

    assert result.scope.files_scanned == 2
    assert result.scope.files_skipped == 0
    assert result.scope.bytes_scanned == 22
    assert result.scope.truncated is False
    assert result.stats.text_files == 2
    assert result.stats.windows_script_count == 0
    assert [item.path for item in result.files] == ["README.md", "src/app.py"]


def test_skips_excluded_directories() -> None:
    payload = make_tree(
        [
            blob("README.md"),
            blob("node_modules/a/index.js"),
            blob(".git/config"),
            blob("vendor/lib.go"),
            blob("dist/bundle.js"),
            blob("build/out.txt"),
            blob(".venv/pyvenv.cfg"),
            blob("src/node_modules/c.js"),
        ]
    )

    result = scan_tree(payload)

    assert result.scope.files_scanned == 1
    assert result.scope.files_skipped == 7
    assert [item.path for item in result.files] == ["README.md"]


def test_skips_media_binary_archive_and_oversized_file() -> None:
    payload = make_tree(
        [
            blob("docs/readme.md"),
            blob("assets/logo.png"),
            blob("assets/demo.mp4"),
            blob("tools/run.exe"),
            blob("release/package.zip"),
            blob("big.txt", 1024 * 1024 + 1),
        ]
    )

    result = scan_tree(payload)

    assert result.scope.files_scanned == 1
    assert result.scope.files_skipped == 5
    assert result.scope.truncated is False


def test_truncates_when_file_count_limit_is_reached() -> None:
    payload = make_tree([blob("a.txt"), blob("b.txt"), blob("c.txt")])

    result = scan_tree(payload, ScanLimits(max_files=2))

    assert result.scope.files_scanned == 2
    assert result.scope.truncated is True
    assert "2" in (result.scope.truncate_reason or "")


def test_truncates_when_total_bytes_limit_is_reached() -> None:
    payload = make_tree([blob("a.txt", 6), blob("b.txt", 6)])

    result = scan_tree(payload, ScanLimits(max_total_bytes=10))

    assert result.scope.files_scanned == 1
    assert result.scope.truncated is True
    assert "上限" in (result.scope.truncate_reason or "")


def test_keeps_only_fifty_files_sorted_by_path() -> None:
    items = [blob(f"file{index:02d}.txt") for index in range(60)]

    result = scan_tree(make_tree(items))

    assert result.scope.files_scanned == 60
    assert len(result.files) == 50
    assert result.files[0].path == "file00.txt"
    assert result.files[-1].path == "file49.txt"


def test_counts_windows_scripts_across_whole_tree() -> None:
    payload = make_tree(
        [
            blob("tools/a.ps1"),
            blob("node_modules/install.bat"),
            blob("dist/run.cmd"),
            blob("README.md"),
        ]
    )

    result = scan_tree(payload)

    assert result.scope.files_scanned == 2
    assert result.scope.files_skipped == 2
    assert result.stats.text_files == 2
    assert result.stats.windows_script_count == 3


def test_rejects_unsafe_paths() -> None:
    payload = make_tree([blob("../evil.py"), blob("ok.py")])

    result = scan_tree(payload)

    assert result.scope.files_scanned == 1
    assert result.scope.files_skipped == 1
    assert [item.path for item in result.files] == ["ok.py"]


def test_passes_through_github_truncated_flag() -> None:
    payload = make_tree([blob("README.md")], truncated=True)

    result = scan_tree(payload)

    assert result.scope.truncated is False
    assert result.stats.text_files == 1


def test_invalid_tree_payload_raises_fetch_failed() -> None:
    with pytest.raises(ScanError) as caught:
        scan_tree({"tree": "not-a-list"})

    assert caught.value.code == "FETCH_FAILED"
    assert caught.value.http_status == 502
