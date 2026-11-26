from __future__ import annotations

from app.models import Finding, StatsInfo
from app.scoring import build_score, finding_deduction


def finding(
    rule_id: str,
    dimension: str,
    severity: str,
    *,
    file: str = "",
    cap_eligible: bool = False,
) -> Finding:
    return Finding(
        id=f"{rule_id}-test",
        rule_id=rule_id,
        category=dimension,
        dimension=dimension,
        severity=severity,
        title=f"{rule_id} 命中",
        file=file,
        evidence_masked="MASKED",
        impact="impact",
        fix="fix",
        cap_eligible=cap_eligible,
    )


def stats(ratio: float = 0.8) -> StatsInfo:
    return StatsInfo(secret_sampling_ratio=ratio)


def test_empty_repo_scores_100_a() -> None:
    score = build_score([], stats())
    assert score.total == 100
    assert score.raw_total == 100
    assert score.grade == "A"
    assert score.verdict == "可直接接入"
    assert [item.weight for item in score.dimensions] == [30, 25, 20, 15, 10]
    assert sum(item.score for item in score.dimensions) == 100
    assert score.caps == []


def test_r09_medium_caps_at_79_b() -> None:
    score = build_score([finding("R09", "secrets", "medium", file=".env")], stats())
    assert score.raw_total == 90
    assert score.total == 79
    assert score.grade == "B"
    assert score.verdict == "小修后接入"
    assert [(cap.type, cap.limit) for cap in score.caps] == [("env_committed_placeholder", 79)]


def test_r09_critical_caps_at_69_c() -> None:
    score = build_score(
        [finding("R09", "secrets", "critical", file=".env", cap_eligible=True)], stats()
    )
    assert score.total == 69
    assert score.grade == "C"
    assert score.verdict == "需整改"


def test_r10_critical_caps_at_59_d() -> None:
    score = build_score(
        [finding("R10", "secrets", "critical", file="id_rsa", cap_eligible=True)], stats()
    )
    assert score.raw_total == 70
    assert score.total == 59
    assert score.grade == "D"
    assert score.verdict == "不建议接入"


def test_r11_critical_caps_only_when_cap_eligible() -> None:
    capped = build_score(
        [finding("R11", "secrets", "critical", file="config.py", cap_eligible=True)], stats()
    )
    uncapped = build_score(
        [finding("R11", "secrets", "critical", file="config.py", cap_eligible=False)], stats()
    )
    assert capped.total == 59
    assert uncapped.total == 70
    assert uncapped.caps == []


def test_r07_has_no_cap() -> None:
    score = build_score([finding("R07", "portability", "medium")], stats())
    assert score.total == 96
    assert score.grade == "A"
    assert score.caps == []


def test_docs_deps_12_caps_at_59() -> None:
    score = build_score(
        [
            finding("R01", "docs", "high"),
            finding("R03", "deps", "high"),
        ],
        stats(),
    )
    assert score.dimensions[2].deduction == 10
    assert score.dimensions[3].deduction == 8
    assert score.total == 59
    assert score.grade == "D"


def test_docs_deps_8_caps_at_69() -> None:
    score = build_score(
        [
            finding("R02", "docs", "medium"),
            finding("R05", "deps", "medium"),
        ],
        stats(),
    )
    assert score.total == 69
    assert score.grade == "C"
    assert score.caps[0].limit == 69


def test_r08_halves_below_20_percent_sampling() -> None:
    full = finding_deduction(finding("R08", "portability", "high", file="a.py"), 0.8)
    half = finding_deduction(finding("R08", "portability", "high", file="a.py"), 0.05)
    assert full == -2
    assert half == -1


def test_r10_low_halves_below_20_percent_sampling() -> None:
    assert finding_deduction(finding("R10", "secrets", "low"), 0.8) == -3
    assert finding_deduction(finding("R10", "secrets", "low"), 0.05) == -2


def test_secret_low_in_test_dir_does_not_deduct() -> None:
    assert (
        finding_deduction(
            finding("R11", "secrets", "low", file="tests/fixtures/fake_key.py"), 0.8
        )
        == 0
    )
    assert (
        finding_deduction(finding("R09", "secrets", "low", file="testdata/.env"), 0.8)
        == 0
    )


def test_dimension_score_never_below_zero() -> None:
    score = build_score(
        [finding("R01", "docs", "high"), finding("R02", "docs", "medium")], stats()
    )
    assert score.dimensions[2].score == 6
    assert score.raw_total == 86
    assert score.total == 59


def test_total_cannot_exceed_raw_total() -> None:
    score = build_score([], stats())
    assert score.total == score.raw_total
