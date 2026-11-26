from __future__ import annotations

from pathlib import Path

from app.rules.portability import (
    detect_hardcoded_path,
    detect_no_crossplatform,
    detect_windows_scripts,
)
from app.snapshot import BlobRef, Snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "paths"


def snapshot(
    paths: dict[str, int],
    contents: dict[str, str] | None = None,
    text_total: int = 0,
) -> Snapshot:
    return Snapshot(
        owner="owner",
        repo="repo",
        commit_sha="a" * 40,
        ref="main",
        blobs=tuple(BlobRef(path=path, size=size) for path, size in paths.items()),
        contents=dict(contents or {}),
        text_total=text_total,
    )


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------- R06 ----------


def test_r06_passes_without_windows_scripts() -> None:
    outcome = detect_windows_scripts(snapshot({"README.md": 10}))

    assert outcome.status == "pass"
    assert "共 0 个 Windows 脚本" in outcome.basis
    assert outcome.finding is None


def test_r06_grades_by_count() -> None:
    one = detect_windows_scripts(snapshot({"a.ps1": 10}))
    assert one.status == "pass"
    assert one.finding is None
    assert one.extra_findings[0].severity == "info"
    assert one.penalty == 0
    assert "仅提示 1 条" in one.basis

    three = detect_windows_scripts(snapshot({"a.ps1": 1, "b.cmd": 1, "c.bat": 1}))
    assert three.finding is not None and three.finding.severity == "medium"
    assert three.penalty == 3

    six_paths = {"w%d.ps1" % index: 1 for index in range(6)}
    six = detect_windows_scripts(snapshot(six_paths))
    assert six.finding is not None and six.finding.severity == "high"
    assert six.penalty == 6
    assert six.finding.hit_count == 6
    assert "共 6 个 Windows 脚本" in six.basis
    assert "6/6" in six.basis


def test_r06_share_below_half_is_not_high() -> None:
    paths = {"w%d.ps1" % index: 1 for index in range(6)}
    paths.update({"s%d.sh" % index: 1 for index in range(10)})

    outcome = detect_windows_scripts(snapshot(paths))

    assert outcome.finding is not None and outcome.finding.severity == "medium"
    assert outcome.penalty == 3
    assert "6/16" in outcome.basis


def test_r06_basis_adds_raw_tree_footnote() -> None:
    state = snapshot({"w%d.ps1" % index: 1 for index in range(6)})
    state.raw_windows_scripts = 8

    outcome = detect_windows_scripts(state)

    assert "全树 8" in outcome.basis
    assert "其中 2 个" in outcome.basis


# ---------- R07 ----------


def test_r07_is_na_without_windows_scripts() -> None:
    outcome = detect_no_crossplatform(snapshot({"README.md": 10}))

    assert outcome.status == "n/a"
    assert outcome.basis == "无 Windows 脚本，规则不适用"


def test_r07_passes_with_makefile() -> None:
    outcome = detect_no_crossplatform(snapshot({"run.ps1": 10, "Makefile": 10}))

    assert outcome.status == "pass"
    assert outcome.basis == "存在跨平台入口：Makefile（Makefile）"


def test_r07_fails_without_cross_platform_entry() -> None:
    outcome = detect_no_crossplatform(snapshot({"run.ps1": 10, "tool.cmd": 10}))

    assert outcome.status == "fail"
    assert outcome.finding is not None
    assert outcome.penalty == 4


# ---------- R08 ----------


def test_r08_fixture_severity_matrix() -> None:
    mapping = {
        "drive.py": "drive.py",
        "unc.py": "unc.py",
        "users.md": "users.md",
        ".github/workflows/ci.yml": ".github/workflows/ci.yml",
        "url.py": "url.py",
        "placeholder.py": "placeholder.py",
        "route.py": "route.py",
    }
    contents = {path: fixture(name) for name, path in mapping.items()}
    paths = {path: len(text) for path, text in contents.items()}

    outcome = detect_hardcoded_path(snapshot(paths, contents, text_total=len(contents)))

    findings = [outcome.finding] + outcome.extra_findings
    assert outcome.status == "fail"
    by_severity: dict[str, list] = {"high": [], "low": [], "info": []}
    for finding in findings:
        by_severity[finding.severity].append(finding)
    assert len(by_severity["high"]) == 1
    assert len(by_severity["low"]) == 4
    assert len(by_severity["info"]) == 0
    assert {finding.file for finding in by_severity["high"]} == {"unc.py"}
    hit_files = {finding.file for finding in findings}
    assert "url.py" not in hit_files
    assert "route.py" not in hit_files


def test_r08_merges_hits_in_one_file() -> None:
    text = 'A = "C:\\\\Users\\\\alice\\\\one"\nB = "/Users/alice/two"\n'

    outcome = detect_hardcoded_path(snapshot({"merge.py": 80}, {"merge.py": text}, text_total=1))

    findings = outcome.extra_findings
    assert outcome.status == "pass"
    assert len(findings) == 1
    assert findings[0].hit_count == 2
    assert findings[0].line == 1
    assert findings[0].file == "merge.py"


def test_r08_penalty_is_capped() -> None:
    contents = {
        "f%d.py" % index: 'X = "C:\\\\Data\\\\file%d"' % index
        for index in range(6)
    }
    paths = {path: len(text) for path, text in contents.items()}

    outcome = detect_hardcoded_path(snapshot(paths, contents, text_total=len(contents)))

    findings = [outcome.finding] + outcome.extra_findings
    assert len(findings) == 6
    assert outcome.penalty == 10


def test_r08_drive_escape_fixture() -> None:
    text = fixture("escape.py")
    outcome = detect_hardcoded_path(
        snapshot({"escape.py": len(text)}, {"escape.py": text}, text_total=1)
    )

    findings = [outcome.finding] + outcome.extra_findings
    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert findings[0].hit_count == 2
    evidence = findings[0].evidence_masked
    assert "c:\\Users\\real" in evidence
    assert "C:\\NuGetPackages" in evidence
    assert "s:\\next" not in evidence
    assert "e:\\temp" not in evidence


def test_r08_degrades_when_sampling_low_and_nothing_hit() -> None:
    outcome = detect_hardcoded_path(
        snapshot({"clean.py": 10}, {"clean.py": "x = 1\n"}, text_total=100)
    )

    assert outcome.status == "degraded"
    assert "抽样率过低" in outcome.basis
    assert outcome.finding is None


def test_r08_low_sampling_hit_uses_medium_confidence() -> None:
    text = 'X = "C:\\\\Data\\\\file.txt"\n'
    outcome = detect_hardcoded_path(snapshot({"a.py": 50}, {"a.py": text}, text_total=100))

    assert outcome.status == "fail"
    assert outcome.finding is not None
    assert outcome.finding.confidence == "medium"
    assert "非全仓结论" in outcome.finding.impact


def test_r08_ignores_escape_sequences_and_url_and_route() -> None:
    text = (
        'A = "s:\\\\n"\n'
        'U = "https://example.com/C:/Users/alice"\n'
        'P = "/api/users"\n'
    )

    outcome = detect_hardcoded_path(snapshot({"clean.py": 200}, {"clean.py": text}, text_total=1))

    assert outcome.status == "pass"
    assert "未命中硬编码路径" in outcome.basis


def test_r08_nested_test_file_is_info() -> None:
    text = 'X = "C:\\\\unrelated"\n'
    outcome = detect_hardcoded_path(
        snapshot(
            {"src/vs/platform/agentHost/test/provider.test.ts": 30},
            {"src/vs/platform/agentHost/test/provider.test.ts": text},
            text_total=1,
        )
    )

    assert outcome.status == "pass"
    assert outcome.extra_findings[0].severity == "info"


def test_r08_degrades_without_content() -> None:
    outcome = detect_hardcoded_path(snapshot({"README.md": 10}, text_total=1))

    assert outcome.status == "degraded"
    assert outcome.finding is None
