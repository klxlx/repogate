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


def assert_identities(result) -> None:
    """v1.4 冻结恒等式：清单内=分析+跳过；全树=清单+未看到。"""
    assert result.scope.files_scanned + result.scope.files_skipped == result.stats.listed_blobs
    assert result.stats.listed_blobs + result.stats.unlisted_blobs == result.stats.total_blobs_in_tree


def test_counts_all_blobs_as_listed_but_only_analyzable_as_scanned() -> None:
    payload = make_tree(
        [
            blob("README.md", 10),
            blob("assets/logo.png", 20),
            blob("src/app.py", 30),
            blob("node_modules/x.js", 5),
            tree("src"),
        ]
    )

    result = scan_tree(payload)

    assert result.scope.files_scanned == 2
    assert result.scope.files_skipped == 2
    assert result.scope.bytes_scanned == 40
    assert result.stats.bytes_total_listed == 65
    assert result.scope.truncated is False
    assert result.scope.truncate_reason == "none"
    assert result.stats.total_blobs_in_tree == 4
    assert result.stats.listed_blobs == 4
    assert result.stats.unlisted_blobs == 0
    assert result.stats.text_files == 2
    assert result.stats.windows_script_count == 0
    assert [item.path for item in result.files] == ["README.md", "src/app.py"]
    assert_identities(result)


def test_blob_cap_samples_by_priority_not_alphabetical() -> None:
    payload = make_tree(
        [
            blob("aaa.png", 1),
            blob("zzz.ps1", 2),
            blob("mmm.yaml", 3),
            blob("README.md", 4),
            blob("src/app.py", 5),
        ]
    )

    result = scan_tree(payload, ScanLimits(blob_cap=2))

    assert result.scope.files_scanned == 2
    assert result.scope.files_skipped == 0
    assert result.scope.bytes_scanned == 5
    assert result.scope.truncated is True
    assert result.scope.truncate_reason == "blob_cap"
    assert result.stats.total_blobs_in_tree == 5
    assert result.stats.listed_blobs == 2
    assert result.stats.unlisted_blobs == 3
    assert [item.path for item in result.files] == ["mmm.yaml", "zzz.ps1"]
    assert_identities(result)


def test_no_truncation_when_blob_count_equals_cap() -> None:
    payload = make_tree([blob("a.txt"), blob("b.txt"), blob("c.txt")])

    result = scan_tree(payload, ScanLimits(blob_cap=3))

    assert result.scope.files_scanned == 3
    assert result.scope.files_skipped == 0
    assert result.scope.truncated is False
    assert result.scope.truncate_reason == "none"
    assert result.stats.unlisted_blobs == 0
    assert_identities(result)


def test_github_tree_truncation_uses_github_reason() -> None:
    payload = make_tree([blob("README.md"), blob("src/app.py")], truncated=True)

    result = scan_tree(payload, github_truncated=True)

    assert result.scope.truncated is True
    assert result.scope.truncate_reason == "github_tree_limit"
    assert result.stats.listed_blobs == 2
    assert result.stats.unlisted_blobs == 0  # 上限在 GitHub 侧，我们看到的都进了清单
    assert_identities(result)


def test_github_tree_truncation_wins_over_blob_cap_reason() -> None:
    payload = make_tree([blob("a.py"), blob("b.ps1"), blob("c.yaml")], truncated=True)

    result = scan_tree(payload, ScanLimits(blob_cap=2), github_truncated=True)

    assert result.scope.truncated is True
    assert result.scope.truncate_reason == "github_tree_limit"
    assert result.stats.listed_blobs == 2
    assert result.stats.unlisted_blobs == 1
    assert_identities(result)


def test_keeps_only_fifty_display_files_sorted_by_path() -> None:
    items = [blob(f"file{index:02d}.txt") for index in range(60)]

    result = scan_tree(make_tree(items))

    assert result.scope.files_scanned == 60
    assert len(result.files) == 50
    assert result.files[0].path == "file00.txt"
    assert result.files[-1].path == "file49.txt"
    assert_identities(result)


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
    assert result.stats.windows_script_count == 1
    assert result.raw_windows_script_count == 3
    assert [item.path for item in result.files] == ["README.md", "tools/a.ps1"]
    assert_identities(result)


def test_text_files_matches_files_scanned() -> None:
    payload = make_tree(
        [
            blob("README.md"),
            blob("logo.png"),
            blob("tool.exe"),
            blob("package.zip"),
        ]
    )

    result = scan_tree(payload)

    assert result.stats.text_files == 1
    assert result.stats.text_files == result.scope.files_scanned
    assert result.stats.total_blobs_in_tree == 4
    assert result.scope.files_skipped == 3
    assert_identities(result)


def test_oversized_text_file_is_skipped_from_analysis() -> None:
    payload = make_tree([blob("big.log", 2 * 1024 * 1024), blob("small.log", 10)])

    result = scan_tree(payload)

    assert result.stats.listed_blobs == 2
    assert result.scope.files_scanned == 1
    assert result.scope.files_skipped == 1
    assert result.scope.bytes_scanned == 10
    assert result.stats.bytes_total_listed == 2 * 1024 * 1024 + 10
    assert [item.path for item in result.files] == ["small.log"]
    assert_identities(result)


def test_unsafe_paths_are_not_counted_in_total() -> None:
    payload = make_tree([blob("../evil.py"), blob("ok.py")])

    result = scan_tree(payload)

    assert result.stats.total_blobs_in_tree == 1
    assert result.scope.files_scanned == 1
    assert result.stats.unlisted_blobs == 0
    assert_identities(result)


def test_invalid_tree_payload_raises_fetch_failed() -> None:
    with pytest.raises(ScanError) as caught:
        scan_tree({"tree": "not-a-list"})

    assert caught.value.code == "FETCH_FAILED"
    assert caught.value.http_status == 502
