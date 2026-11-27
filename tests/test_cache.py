from __future__ import annotations

import json

from app import cache
from app.version import ENGINE_VERSION

SHA = "a" * 40


def _report(total: int = 100) -> dict:
    return {
        "schema_version": 1,
        "ok": True,
        "score": {"total": total, "grade": "A"},
    }


def test_cache_path_layout(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("REPOGATE_CACHE_DIR", str(tmp_path))
    path = cache.path_for("octocat", "Hello-World", SHA)
    assert path == tmp_path / "octocat__Hello-World" / f"{ENGINE_VERSION}__{SHA}.json"


def test_cache_save_and_load_wrapper(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("REPOGATE_CACHE_DIR", str(tmp_path))
    cached_at = cache.save("octocat", "Hello-World", SHA, _report(), 123)
    cached = cache.load("octocat", "Hello-World", SHA)

    assert cached is not None
    assert cached.cached_at == cached_at
    assert cached.duration_ms == 123
    assert cached.report["score"]["total"] == 100
    wrapper = json.loads(cached.path.read_text(encoding="utf-8"))
    assert wrapper["schema_version"] == 1
    assert wrapper["engine_version"] == ENGINE_VERSION
    assert wrapper["cached_at"] == cached_at
    assert wrapper["duration_ms"] == 123
    assert wrapper["report"]["ok"] is True
    assert not list(cached.path.parent.glob("*.tmp"))


def test_cache_lru_keeps_latest_five(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("REPOGATE_CACHE_DIR", str(tmp_path))
    for index in range(6):
        sha = f"{index:040x}"
        cache.save("octocat", "Hello-World", sha, _report(index), index)
        path = cache.path_for("octocat", "Hello-World", sha)
        # 显式拉开 mtime，避免 Windows 文件时间精度导致顺序不稳定。
        import os

        os.utime(path, (1000 + index, 1000 + index))

    remaining = sorted(path.name for path in cache.repo_dir("octocat", "Hello-World").glob("*.json"))
    assert len(remaining) == 5
    assert f"{ENGINE_VERSION}__{0:040x}.json" not in remaining
    assert f"{ENGINE_VERSION}__{5:040x}.json" in remaining


def test_cache_latest_skips_corrupt_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("REPOGATE_CACHE_DIR", str(tmp_path))
    first_sha = "b" * 40
    second_sha = "c" * 40
    cache.save("octocat", "Hello-World", first_sha, _report(90), 10)
    cache.save("octocat", "Hello-World", second_sha, _report(80), 20)

    broken = cache.path_for("octocat", "Hello-World", second_sha)
    broken.write_text("{not-json", encoding="utf-8")

    cached = cache.load_latest("octocat", "Hello-World")
    assert cached is not None
    assert cached.report["score"]["total"] == 90


def test_cache_ignores_wrong_schema(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("REPOGATE_CACHE_DIR", str(tmp_path))
    cache.save("octocat", "Hello-World", SHA, _report(), 1)
    path = cache.path_for("octocat", "Hello-World", SHA)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = 0
    path.write_text(json.dumps(data), encoding="utf-8")
    assert cache.load("octocat", "Hello-World", SHA) is None


def test_cache_engine_mismatch_is_not_hit_but_latest_stale(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("REPOGATE_CACHE_DIR", str(tmp_path))
    cache.save("octocat", "Hello-World", SHA, _report(), 1, "repogate-3.1")

    assert cache.load("octocat", "Hello-World", SHA, ENGINE_VERSION) is None
    latest = cache.load_latest("octocat", "Hello-World")
    assert latest is not None
    assert latest.engine_version == "repogate-3.1"
