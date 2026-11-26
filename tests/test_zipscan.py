from __future__ import annotations

import io
import zipfile

import pytest

from app.errors import ScanError
from app.zipscan import ScanLimits, scan_zip

PREFIX = "octocat-Hello-World-abc123/"


def build_zip(entries: dict[str, bytes], prefix: str = PREFIX) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(prefix + name, data)
    return buffer.getvalue()


def test_scans_text_files_and_strips_wrapper_dir() -> None:
    payload = build_zip(
        {
            "README.md": b"# hello\n",
            "src/app.py": b"print('hi')\n",
            "LICENSE": b"MIT\n",
        }
    )

    result = scan_zip(payload)

    assert result.scope.files_scanned == 3
    assert result.scope.files_skipped == 0
    assert result.scope.bytes_scanned == len(b"# hello\n") + len(b"print('hi')\n") + len(b"MIT\n")
    assert result.scope.truncated is False
    assert [item.path for item in result.files] == ["LICENSE", "README.md", "src/app.py"]
    assert all(item.is_text is True for item in result.files)


def test_skips_excluded_directories() -> None:
    payload = build_zip(
        {
            "README.md": b"keep\n",
            "node_modules/a/index.js": b"skip\n",
            ".git/config": b"skip\n",
            "vendor/lib.go": b"skip\n",
            "dist/bundle.js": b"skip\n",
            "build/out.txt": b"skip\n",
            ".venv/pyvenv.cfg": b"skip\n",
            "src/node_modules/c.js": b"skip\n",
        }
    )

    result = scan_zip(payload)

    assert result.scope.files_scanned == 1
    assert result.scope.files_skipped == 7
    assert [item.path for item in result.files] == ["README.md"]


def test_skips_media_binary_archive_and_oversized_file() -> None:
    payload = build_zip(
        {
            "docs/readme.md": b"keep\n",
            "assets/logo.png": b"\x89PNG binary",
            "assets/demo.mp4": b"binary",
            "tools/run.exe": b"MZ binary",
            "release/package.zip": b"PK binary",
            "big.txt": b"x" * (1024 * 1024 + 1),
            
        }
    )

    result = scan_zip(payload)

    assert result.scope.files_scanned == 1
    assert result.scope.files_skipped == 5
    assert result.scope.truncate_reason is None


def test_skips_non_utf8_content_as_binary() -> None:
    payload = build_zip({"app.binlog": b"abc\x00def"})

    result = scan_zip(payload)

    assert result.scope.files_scanned == 0
    assert result.scope.files_skipped == 1


def test_truncates_when_file_count_limit_is_reached() -> None:
    payload = build_zip({"a.txt": b"a", "b.txt": b"b", "c.txt": b"c"})

    result = scan_zip(payload, ScanLimits(max_files=2))

    assert result.scope.files_scanned == 2
    assert result.scope.truncated is True
    assert "2" in (result.scope.truncate_reason or "")


def test_truncates_when_total_bytes_limit_is_reached() -> None:
    payload = build_zip({"a.txt": b"123456", "b.txt": b"123456"})

    result = scan_zip(payload, ScanLimits(max_total_bytes=10))

    assert result.scope.files_scanned == 1
    assert result.scope.truncated is True
    assert "上限" in (result.scope.truncate_reason or "")


def test_keeps_only_fifty_files() -> None:
    entries = {f"file{index:02d}.txt": b"x" for index in range(60)}

    result = scan_zip(build_zip(entries))

    assert result.scope.files_scanned == 60
    assert len(result.files) == 50
    assert result.files[0].path == "file00.txt"
    assert result.files[-1].path == "file49.txt"


def test_sample_prioritizes_root_files_and_directory_rotation() -> None:
    entries: dict[str, bytes] = {}
    for index in range(12):
        entries[f"root{index:02d}.txt"] = b"x"
    for index in range(10):
        entries[f"src/deep{index:02d}.txt"] = b"x"
    for index in range(10):
        entries[f"tools/deep{index:02d}.txt"] = b"x"

    result = scan_zip(build_zip(entries))
    paths = [item.path for item in result.files]

    assert len(paths) == 32
    assert "root11.txt" in paths
    assert any(path.startswith("src/") for path in paths)
    assert any(path.startswith("tools/") for path in paths)


def test_rejects_unsafe_paths() -> None:
    payload = build_zip({"../evil.py": b"bad", "ok.py": b"good"})

    result = scan_zip(payload)

    assert result.scope.files_scanned == 1
    assert result.scope.files_skipped == 1
    assert [item.path for item in result.files] == ["ok.py"]


def test_invalid_zip_raises_business_error() -> None:
    with pytest.raises(ScanError) as caught:
        scan_zip(b"not a zip")

    assert caught.value.code == "UPSTREAM_ERROR"
    assert caught.value.http_status == 502
