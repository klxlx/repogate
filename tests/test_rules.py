from __future__ import annotations

import json

from app.rules import evaluate
from app.rules.deps import R03, R04, R05, detect_deps_missing, detect_deps_unpinned, detect_start_missing
from app.rules.docs import R01, R02, detect_readme_incomplete, detect_readme_missing
from app.snapshot import BlobRef, Snapshot


def snapshot(
    paths: dict[str, int] | None = None,
    contents: dict[str, str] | None = None,
    failed: tuple[str, ...] = (),
) -> Snapshot:
    blobs = paths if paths is not None else {}
    return Snapshot(
        owner="owner",
        repo="repo",
        commit_sha="a" * 40,
        ref="main",
        blobs=tuple(BlobRef(path=path, size=size) for path, size in blobs.items()),
        contents=dict(contents or {}),
        failed_paths=frozenset(failed),
    )


# ---------- R01 readme-missing ----------


def test_r01_hits_when_no_readme() -> None:
    finding = detect_readme_missing(snapshot({"src/app.py": 10}))

    assert finding is not None
    assert finding.rule_id == "R01"
    assert finding.severity == "high"
    assert finding.file == ""
    assert "未找到 README" in finding.evidence_masked


def test_r01_misses_with_readme_variants() -> None:
    assert detect_readme_missing(snapshot({"README.md": 10})) is None
    assert detect_readme_missing(snapshot({"readme.rst": 10})) is None
    assert detect_readme_missing(snapshot({"README": 10})) is None  # 无扩展 README 也算存在


# ---------- R02 readme-incomplete ----------


def test_r02_hits_when_readme_has_less_than_two_categories() -> None:
    finding = detect_readme_incomplete(
        snapshot({"README.md": 500}, {"README.md": "Hello World!"})
    )

    assert finding is not None
    assert finding.severity == "medium"
    assert finding.file == "README.md"
    assert "只覆盖 0 类" in finding.evidence_masked


def test_r02_misses_with_install_and_run() -> None:
    text = "# demo\n\n## Install\npip install demo\n\n## Usage\nrun demo\n"
    assert detect_readme_incomplete(snapshot({"README.md": 500}, {"README.md": text})) is None


def test_r02_skips_when_r01_hit() -> None:
    assert detect_readme_incomplete(snapshot({"src/app.py": 10})) is None


def test_r02_degrades_when_content_unavailable() -> None:
    finding = detect_readme_incomplete(snapshot({"README.md": 500}))

    assert finding is not None
    assert finding.severity == "info"
    assert finding.confidence == "low"
    assert "内容不可用" in finding.evidence_masked


# ---------- R03 deps-missing ----------


def test_r03_hits_without_any_manifest() -> None:
    finding = detect_deps_missing(snapshot({"README.md": 10, "src/app.py": 20}))

    assert finding is not None
    assert finding.severity == "high"
    assert "package.json" in finding.evidence_masked
    assert finding.file == ""


def test_r03_misses_with_manifest_variants() -> None:
    assert detect_deps_missing(snapshot({"package.json": 10})) is None
    assert detect_deps_missing(snapshot({"src/app.csproj": 10})) is None
    assert detect_deps_missing(snapshot({"requirements-dev.txt": 10})) is None
    assert detect_deps_missing(snapshot({"CMakeLists.txt": 10})) is None


# ---------- R04 deps-unpinned ----------


def test_r04_hits_when_mostly_floating() -> None:
    text = json.dumps({"dependencies": {"a": "^1.0.0", "b": "~2.0.0"}})
    finding = detect_deps_unpinned(snapshot({"package.json": 100}, {"package.json": text}))

    assert finding is not None
    assert finding.severity == "low"
    assert finding.file == "package.json"
    assert "浮动 2 个 / 总计 2 个" in finding.evidence_masked


def test_r04_misses_when_pinned() -> None:
    text = json.dumps({"dependencies": {"a": "1.0.0", "b": "2.0.0"}})
    assert detect_deps_unpinned(snapshot({"package.json": 100}, {"package.json": text})) is None


def test_r04_misses_when_lockfile_exists() -> None:
    text = json.dumps({"dependencies": {"a": "^1.0.0", "b": "^2.0.0"}})
    paths = {"package.json": 100, "package-lock.json": 5000}
    assert detect_deps_unpinned(snapshot(paths, {"package.json": text})) is None


def test_r04_degrades_when_content_unavailable() -> None:
    finding = detect_deps_unpinned(snapshot({"package.json": 100}))

    assert finding is not None
    assert finding.severity == "info"
    assert "内容不可用" in finding.evidence_masked


# ---------- R05 start-missing ----------


def test_r05_hits_without_launch_entry() -> None:
    finding = detect_start_missing(snapshot({"README.md": 10}))

    assert finding is not None
    assert finding.severity == "medium"
    assert "Makefile / Dockerfile" in finding.evidence_masked


def test_r05_misses_with_dockerfile_or_script() -> None:
    assert detect_start_missing(snapshot({"Dockerfile": 10})) is None
    assert detect_start_missing(snapshot({"docker-compose.yml": 10})) is None
    scripts = json.dumps({"scripts": {"dev": "vite"}})
    assert (
        detect_start_missing(snapshot({"package.json": 100}, {"package.json": scripts})) is None
    )


def test_r05_degrades_when_package_json_unreadable() -> None:
    finding = detect_start_missing(snapshot({"package.json": 100}))

    assert finding is not None
    assert finding.severity == "info"


# ---------- 引擎 ----------


def test_evaluate_merges_same_rule_and_file() -> None:
    snapshot_obj = snapshot()
    findings = evaluate(snapshot_obj)

    assert [finding.rule_id for finding in findings] == ["R01", "R03", "R05"]
    assert len({finding.id for finding in findings}) == 3


def test_rule_metadata_matches_contract() -> None:
    assert (R01.dimension, R01.severity, R01.points, R01.needs_content) == ("docs", "high", -10, False)
    assert (R02.dimension, R02.severity, R02.points, R02.needs_content) == ("docs", "medium", -4, True)
    assert (R03.dimension, R03.severity, R03.points, R03.needs_content) == ("deps", "high", -8, False)
    assert (R04.dimension, R04.severity, R04.points, R04.needs_content) == ("deps", "low", -2, True)
    assert (R05.dimension, R05.severity, R05.points, R05.needs_content) == ("deps", "medium", -4, True)
