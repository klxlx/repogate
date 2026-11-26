from __future__ import annotations

import json

from app.rules import evaluate
from app.rules.deps import (
    R03,
    R04,
    R05,
    detect_deps_missing,
    detect_deps_unpinned,
    detect_start_missing,
)
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


def test_r01_fails_when_no_readme() -> None:
    outcome = detect_readme_missing(snapshot({"src/app.py": 10}))

    assert outcome.status == "fail"
    assert outcome.finding is not None
    assert outcome.finding.rule_id == "R01"
    assert outcome.finding.severity == "high"
    assert "根目录无 README" in outcome.basis


def test_r01_passes_with_readme_variants() -> None:
    assert detect_readme_missing(snapshot({"README.md": 10})).status == "pass"
    assert detect_readme_missing(snapshot({"readme.rst": 10})).status == "pass"
    outcome = detect_readme_missing(snapshot({"README": 10}))
    assert outcome.status == "pass"
    assert "无扩展名" in outcome.basis


# ---------- R02 readme-incomplete ----------


def test_r02_fails_when_less_than_two_categories() -> None:
    outcome = detect_readme_incomplete(
        snapshot({"README.md": 500}, {"README.md": "Hello World!"})
    )

    assert outcome.status == "fail"
    assert outcome.finding is not None
    assert "README 缺" in outcome.basis


def test_r02_passes_with_install_and_run() -> None:
    text = "# demo\n\n## Install\npip install demo\n\n## Usage\nrun demo\n"
    assert detect_readme_incomplete(snapshot({"README.md": 500}, {"README.md": text})).status == "pass"


def test_r02_degrades_without_readme() -> None:
    outcome = detect_readme_incomplete(snapshot({"src/app.py": 10}))

    assert outcome.status == "degraded"
    assert outcome.finding is None


def test_r02_degrades_when_content_unavailable() -> None:
    outcome = detect_readme_incomplete(snapshot({"README.md": 500}))

    assert outcome.status == "degraded"
    assert outcome.finding is None  # 唯一依据失败，不出 finding
    assert "读取失败" in outcome.basis


# ---------- R03 deps-missing ----------


def test_r03_fails_without_any_manifest() -> None:
    outcome = detect_deps_missing(snapshot({"README.md": 10, "src/app.py": 20}))

    assert outcome.status == "fail"
    assert outcome.finding is not None
    assert "package.json" in outcome.finding.evidence_masked


def test_r03_passes_with_manifest_variants() -> None:
    outcome = detect_deps_missing(snapshot({"vcpkg.json": 10}))
    assert outcome.status == "pass"
    assert outcome.basis == "命中清单 vcpkg.json"
    assert detect_deps_missing(snapshot({"src/app.csproj": 10})).status == "pass"
    assert detect_deps_missing(snapshot({"requirements-dev.txt": 10})).status == "pass"
    assert detect_deps_missing(snapshot({"CMakeLists.txt": 10})).status == "pass"


# ---------- R04 deps-unpinned ----------


def test_r04_fails_when_mostly_floating() -> None:
    text = json.dumps({"dependencies": {"a": "^1.0.0", "b": "~2.0.0"}})
    outcome = detect_deps_unpinned(snapshot({"package.json": 100}, {"package.json": text}))

    assert outcome.status == "fail"
    assert outcome.finding is not None
    assert "浮动 2 个 / 总计 2 个" in outcome.basis


def test_r04_passes_when_pinned() -> None:
    text = json.dumps({"dependencies": {"a": "1.0.0", "b": "2.0.0"}})
    assert detect_deps_unpinned(snapshot({"package.json": 100}, {"package.json": text})).status == "pass"


def test_r04_passes_when_lockfile_exists() -> None:
    text = json.dumps({"dependencies": {"a": "^1.0.0", "b": "^2.0.0"}})
    paths = {"package.json": 100, "package-lock.json": 5000}
    outcome = detect_deps_unpinned(snapshot(paths, {"package.json": text}))
    assert outcome.status == "pass"
    assert "package-lock.json" in outcome.basis


def test_r04_degrades_when_all_manifests_unreadable() -> None:
    outcome = detect_deps_unpinned(snapshot({"package.json": 100}))

    assert outcome.status == "degraded"
    assert outcome.finding is None


def test_r04_degrades_when_partial_manifests_unreadable() -> None:
    text = json.dumps({"dependencies": {"a": "^1.0.0", "b": "~2.0.0"}})
    paths = {"package.json": 100, "pyproject.toml": 50}
    outcome = detect_deps_unpinned(snapshot(paths, {"package.json": text}))

    assert outcome.status == "degraded"
    assert outcome.finding is not None
    assert outcome.finding.severity == "info"
    assert "pyproject.toml 读取失败" in outcome.basis


# ---------- R05 start-missing ----------


def test_r05_fails_without_any_entry() -> None:
    outcome = detect_start_missing(snapshot({"README.md": 10}))

    assert outcome.status == "fail"
    assert outcome.finding is not None
    assert "根目录与一层子目录未找到" in outcome.basis


def test_r05_passes_with_cross_platform_entries() -> None:
    assert detect_start_missing(snapshot({"Dockerfile": 10})).status == "pass"
    assert detect_start_missing(snapshot({"docker-compose.yml": 10})).status == "pass"
    assert detect_start_missing(snapshot({"Makefile": 10})).status == "pass"

    scripts = json.dumps({"scripts": {"dev": "vite"}})
    outcome = detect_start_missing(snapshot({"package.json": 100}, {"package.json": scripts}))
    assert outcome.status == "pass"
    assert outcome.basis == "命中入口 package.json:scripts.dev"


def test_r05_passes_with_windows_entries_and_marks_impact() -> None:
    outcome = detect_start_missing(snapshot({"PowerToys.slnx": 88348}))

    assert outcome.status == "pass"
    assert "PowerToys.slnx" in outcome.basis
    assert "Windows 专用" in outcome.basis

    assert detect_start_missing(snapshot({"App.csproj": 10})).status == "pass"
    assert detect_start_missing(snapshot({"build.ps1": 10})).status == "pass"


def test_r05_ignores_nested_csproj_and_cmake() -> None:
    assert detect_start_missing(snapshot({"src/App.csproj": 10})).status == "fail"
    assert detect_start_missing(snapshot({"src/CMakeLists.txt": 10})).status == "fail"


def test_r05_degrades_when_package_json_unreadable() -> None:
    outcome = detect_start_missing(snapshot({"package.json": 100}))

    assert outcome.status == "degraded"
    assert outcome.finding is None


# ---------- 引擎 ----------


def test_evaluate_returns_check_per_rule_and_consistent_findings() -> None:
    evaluation = evaluate(snapshot())

    assert len(evaluation.checks) == 5
    assert [check.rule_id for check in evaluation.checks] == ["R01", "R02", "R03", "R04", "R05"]
    assert all(check.basis.strip() for check in evaluation.checks)
    fail_rules = {check.rule_id for check in evaluation.checks if check.status == "fail"}
    finding_rules = {finding.rule_id for finding in evaluation.findings}
    pass_rules = {check.rule_id for check in evaluation.checks if check.status == "pass"}
    assert fail_rules <= finding_rules
    assert not (pass_rules & finding_rules)


def test_rule_metadata_matches_contract() -> None:
    assert (R01.dimension, R01.severity, R01.points, R01.needs_content) == ("docs", "high", -10, False)
    assert (R02.dimension, R02.severity, R02.points, R02.needs_content) == ("docs", "medium", -4, True)
    assert (R03.dimension, R03.severity, R03.points, R03.needs_content) == ("deps", "high", -8, False)
    assert (R04.dimension, R04.severity, R04.points, R04.needs_content) == ("deps", "low", -2, True)
    assert (R05.dimension, R05.severity, R05.points, R05.needs_content) == ("deps", "medium", -4, True)
