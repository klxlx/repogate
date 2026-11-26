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
from app.rules.maintenance import R12, R13, R14
from app.rules.secrets import R09, R10, R11
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
    assert "根目录与一层" in outcome.basis


def test_r01_passes_with_root_readme_and_reports_level() -> None:
    outcome = detect_readme_missing(snapshot({"README.md": 10}))
    assert outcome.status == "pass"
    assert outcome.basis == "根目录 README.md"

    outcome = detect_readme_missing(snapshot({"README": 10}))
    assert outcome.status == "pass"
    assert outcome.basis == "根目录 README（无扩展名）"


def test_r01_degrades_in_weak_dir_only() -> None:
    outcome = detect_readme_missing(snapshot({"docs/README.md": 10}))

    assert outcome.status == "degraded"
    assert outcome.basis == "仅在弱目录 docs/README.md 发现，无法确认主项目"


# ---------- R02 readme-incomplete ----------


def test_r02_fails_when_less_than_two_categories() -> None:
    outcome = detect_readme_incomplete(
        snapshot({"README.md": 500}, {"README.md": "Hello World!"})
    )

    assert outcome.status == "fail"
    assert outcome.finding is not None
    assert "根目录 README.md" in outcome.basis


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
    assert outcome.finding is None
    assert "读取失败" in outcome.basis


# ---------- R03 deps-missing ----------


def test_r03_fails_without_any_manifest() -> None:
    outcome = detect_deps_missing(snapshot({"README.md": 10, "src/app.py": 20}))

    assert outcome.status == "fail"
    assert outcome.finding is not None


def test_r03_passes_with_root_manifest_and_reports_level() -> None:
    outcome = detect_deps_missing(snapshot({"vcpkg.json": 10}))
    assert outcome.status == "pass"
    assert outcome.basis == "根目录 vcpkg.json"

    outcome = detect_deps_missing(snapshot({"App.csproj": 10, "vcpkg.json": 10}))
    assert outcome.basis == "根目录 App.csproj（共 2 个候选）"


def test_r03_prefers_root_over_weak_dir() -> None:
    outcome = detect_deps_missing(snapshot({"package.json": 10, "docs/requirements.txt": 10}))

    assert outcome.status == "pass"
    assert outcome.basis == "根目录 package.json"
    assert "docs" not in outcome.basis


def test_r03_degrades_when_only_weak_dir() -> None:
    outcome = detect_deps_missing(snapshot({"docs/requirements.txt": 10}))

    assert outcome.status == "degraded"
    assert outcome.basis == "仅在弱目录 docs/requirements.txt 发现，无法确认主项目"


# ---------- R04 deps-unpinned ----------


def test_r04_is_na_without_parseable_manifest() -> None:
    outcome = detect_deps_unpinned(snapshot({"README.md": 10}))

    assert outcome.status == "n/a"
    assert outcome.basis == "无可解析清单，规则不适用"
    assert outcome.finding is None


def test_r04_mostly_floating_is_pass_with_hint() -> None:
    text = json.dumps({"dependencies": {"a": "^1.0.0", "b": "~2.0.0"}})
    outcome = detect_deps_unpinned(snapshot({"package.json": 100}, {"package.json": text}))

    assert outcome.status == "pass"
    assert outcome.finding is None
    assert outcome.extra_findings[0].severity == "low"
    assert "仅提示 1 条" in outcome.basis
    assert "根目录 package.json" in outcome.basis


def test_r04_passes_when_pinned() -> None:
    text = json.dumps({"dependencies": {"a": "1.0.0", "b": "2.0.0"}})
    assert detect_deps_unpinned(snapshot({"package.json": 100}, {"package.json": text})).status == "pass"


def test_r04_passes_when_lockfile_exists() -> None:
    text = json.dumps({"dependencies": {"a": "^1.0.0", "b": "^2.0.0"}})
    paths = {"package.json": 100, "package-lock.json": 5000}
    outcome = detect_deps_unpinned(snapshot(paths, {"package.json": text}))
    assert outcome.status == "pass"
    assert outcome.basis == "根目录 package-lock.json"


def test_r04_is_na_when_lockfile_only_in_nested_subdir() -> None:
    outcome = detect_deps_unpinned(snapshot({"src/Monaco/package-lock.json": 10}))
    assert outcome.status == "n/a"


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


def test_r04_degrades_when_only_weak_manifest() -> None:
    outcome = detect_deps_unpinned(snapshot({"docs/requirements.txt": 10}))
    assert outcome.status == "degraded"
    assert "弱目录" in outcome.basis


# ---------- R05 start-missing ----------


def test_r05_fails_without_any_entry() -> None:
    outcome = detect_start_missing(snapshot({"README.md": 10}))

    assert outcome.status == "fail"
    assert outcome.finding is not None
    assert "根目录与一层" in outcome.basis


def test_r05_passes_with_cross_platform_entries() -> None:
    outcome = detect_start_missing(snapshot({"Dockerfile": 10}))
    assert outcome.status == "pass"
    assert outcome.basis == "根目录 Dockerfile"

    assert detect_start_missing(snapshot({"docker-compose.yml": 10})).status == "pass"
    assert detect_start_missing(snapshot({"Makefile": 10})).status == "pass"

    scripts = json.dumps({"scripts": {"dev": "vite"}})
    outcome = detect_start_missing(snapshot({"package.json": 100}, {"package.json": scripts}))
    assert outcome.status == "pass"
    assert outcome.basis == "根目录 package.json:scripts.dev（启动入口）"

    scripts = json.dumps({"scripts": {"compile": "tsc"}})
    outcome = detect_start_missing(snapshot({"package.json": 100}, {"package.json": scripts}))
    assert outcome.basis == "根目录 package.json:scripts.compile（构建入口，无启动命令）"

    scripts = json.dumps({"scripts": {"watch": "tsc -w"}})
    assert detect_start_missing(snapshot({"package.json": 100}, {"package.json": scripts})).status == "fail"


def test_r05_passes_with_windows_entries_and_marks_impact() -> None:
    outcome = detect_start_missing(snapshot({"PowerToys.slnx": 88348}))

    assert outcome.status == "pass"
    assert outcome.basis.startswith("根目录 PowerToys.slnx")
    assert "Windows 专用" in outcome.basis

    assert detect_start_missing(snapshot({"App.csproj": 10})).status == "pass"
    assert detect_start_missing(snapshot({"build.ps1": 10})).status == "pass"


def test_r05_ignores_nested_csproj_and_cmake() -> None:
    assert detect_start_missing(snapshot({"src/App.csproj": 10})).status == "fail"
    assert detect_start_missing(snapshot({"src/CMakeLists.txt": 10})).status == "fail"


def test_r05_degrades_when_only_weak_dir_entry() -> None:
    outcome = detect_start_missing(snapshot({".devcontainer/Dockerfile": 10}))

    assert outcome.status == "degraded"
    assert outcome.basis == "仅在弱目录 .devcontainer/Dockerfile 发现，无法确认主项目"


def test_r05_degrades_when_package_json_unreadable() -> None:
    outcome = detect_start_missing(snapshot({"package.json": 100}))

    assert outcome.status == "degraded"
    assert outcome.finding is None


# ---------- 引擎 ----------


def test_evaluate_returns_check_per_rule_and_consistent_findings() -> None:
    evaluation = evaluate(snapshot())

    assert len(evaluation.checks) == 14
    assert [check.rule_id for check in evaluation.checks] == [
        "R01", "R02", "R03", "R04", "R05", "R06", "R07", "R08",
        "R09", "R10", "R11", "R12", "R13", "R14",
    ]
    assert all(check.basis.strip() for check in evaluation.checks)

    by_rule: dict[str, list] = {}
    for finding in evaluation.findings:
        by_rule.setdefault(finding.rule_id, []).append(finding)

    for check in evaluation.checks:
        items = by_rule.get(check.rule_id, [])
        if check.status == "fail":
            assert any(item.severity in ("medium", "high", "critical") for item in items)
        elif check.status in ("pass", "n/a"):
            assert all(item.severity in ("low", "info") for item in items)


def test_rule_metadata_matches_contract() -> None:
    assert (R01.dimension, R01.severity, R01.points, R01.needs_content) == ("docs", "high", -10, False)
    assert (R02.dimension, R02.severity, R02.points, R02.needs_content) == ("docs", "medium", -4, True)
    assert (R03.dimension, R03.severity, R03.points, R03.needs_content) == ("deps", "high", -8, False)
    assert (R04.dimension, R04.severity, R04.points, R04.needs_content) == ("deps", "low", -2, True)
    assert (R09.dimension, R09.severity, R09.needs_content) == ("secrets", "critical", True)
    assert (R10.dimension, R10.severity, R10.needs_content) == ("secrets", "critical", True)
    assert (R11.dimension, R11.severity, R11.needs_content) == ("secrets", "critical", True)
    assert (R05.dimension, R05.severity, R05.points, R05.needs_content) == ("deps", "medium", -4, True)
    assert (R12.dimension, R12.severity, R12.points, R12.needs_content) == ("maintenance", "low", -4, False)
    assert (R13.dimension, R13.severity, R13.points, R13.needs_content) == ("maintenance", "low", -3, False)
    assert (R14.dimension, R14.severity, R14.points, R14.needs_content) == ("maintenance", "low", -3, False)
