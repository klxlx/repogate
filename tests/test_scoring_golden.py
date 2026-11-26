"""评分纯函数 golden：固定 findings 输入逐项比对 total/grade/caps/deductions。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models import Finding, StatsInfo
from app.scoring import build_score

GOLDEN = Path(__file__).parent / "golden" / "scoring_cases.json"


def _finding(data: dict) -> Finding:
    rule_id = data["rule_id"]
    return Finding(
        id=f"{rule_id}-golden",
        rule_id=rule_id,
        category=data["dimension"],
        dimension=data["dimension"],
        severity=data["severity"],
        title=f"{rule_id} 命中",
        file=data.get("file", ""),
        evidence_masked="MASKED",
        impact="impact",
        fix="fix",
        cap_eligible=bool(data.get("cap_eligible", False)),
    )


def _cases() -> list[dict]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["name"])
def test_scoring_golden(case: dict) -> None:
    score = build_score(
        [_finding(item) for item in case["findings"]],
        StatsInfo(secret_sampling_ratio=case["sampling_ratio"]),
    )
    expected = case["expected"]

    assert score.total == expected["total"]
    assert score.raw_total == expected["raw_total"]
    assert score.grade == expected["grade"]
    assert [{"type": item.type, "limit": item.limit} for item in score.caps] == expected["caps"]
    assert [
        {"rule_id": item.rule_id, "points": item.points, "file": item.file}
        for item in score.deductions
    ] == expected["deductions"]


def test_scoring_golden_has_at_least_eight_cases() -> None:
    assert len(_cases()) >= 8
