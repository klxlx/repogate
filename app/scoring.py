"""评分引擎：把 14 条规则的 findings 合成 0–100 分、等级、裁决与封顶。

口径（README 同步公开）：
- 维度权重 secrets 30 / portability 25 / docs 20 / deps 15 / maintenance 10。
- R01–R08 保留既有规则扣分（R08 按每个 high 文件 -2）；R09–R14 按 severity：
  critical -30 / high -15 / medium -10 / low -3 / info 0。
- R08 / R10 / R11 在内容抽样率 <20% 时扣分减半。
- 封顶按最严一条生效；R07 不设封顶。
"""

from __future__ import annotations

from .models import CapInfo, DeductionInfo, DimensionInfo, Finding, ScoreInfo, StatsInfo
from .rules import RULES
from .rules.secrets import in_low_dir

DIMENSION_META: tuple[tuple[str, str, int], ...] = (
    ("secrets", "密钥与敏感信息", 30),
    ("portability", "平台可移植", 25),
    ("docs", "文档完备", 20),
    ("deps", "依赖与入口", 15),
    ("maintenance", "维护状态", 10),
)
SEVERITY_POINTS = {
    "critical": 30,
    "high": 15,
    "medium": 10,
    "low": 3,
    "info": 0,
}
LEGACY_POINTS = {rule.id: rule.points for rule in RULES if rule.id <= "R08"}
HALF_RULES = frozenset({"R08", "R10", "R11"})
GRADE_THRESHOLDS = (("A", 85), ("B", 70), ("C", 60))
VERDICTS = {
    "A": "可直接接入",
    "B": "小修后接入",
    "C": "需整改",
    "D": "不建议接入",
}


def _half(points: int) -> int:
    magnitude = abs(points)
    if magnitude == 0:
        return 0
    return max(1, int(round(magnitude * 0.5)))


def finding_deduction(finding: Finding, sampling_ratio: float) -> int:
    """返回负分；info 为 0。"""
    if finding.severity == "info":
        return 0

    rule_id = finding.rule_id
    if rule_id <= "R08":
        if rule_id == "R08":
            points = -2 if finding.severity == "high" else 0
        else:
            points = LEGACY_POINTS.get(rule_id, 0)
    else:
        # 测试/样例目录里的敏感命中只提示、不扣分，避免把假密钥夹具当事故。
        if (
            rule_id in {"R09", "R10", "R11"}
            and finding.severity == "low"
            and in_low_dir(finding.file)
        ):
            return 0
        points = -SEVERITY_POINTS[finding.severity]

    if points == 0:
        return 0
    if rule_id in HALF_RULES and sampling_ratio < 0.2:
        points = -_half(points)
    return points


def _grade_for(total: int) -> str:
    for grade, threshold in GRADE_THRESHOLDS:
        if total >= threshold:
            return grade
    return "D"


def build_score(
    findings: list[Finding],
    stats: StatsInfo,
    *,
    sampling_ratio: float | None = None,
) -> ScoreInfo:
    ratio = stats.secret_sampling_ratio if sampling_ratio is None else sampling_ratio

    dimensions = []
    dimension_deductions: dict[str, int] = {key: 0 for key, _, _ in DIMENSION_META}
    deductions: list[DeductionInfo] = []

    ordered_findings = sorted(
        findings,
        key=lambda item: (
            {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}[item.severity],
            item.rule_id,
            item.file,
        ),
    )
    for finding in ordered_findings:
        points = finding_deduction(finding, ratio)
        if points:
            dimension_deductions[finding.dimension] = (
                dimension_deductions.get(finding.dimension, 0) + abs(points)
            )
            deductions.append(
                DeductionInfo(
                    rule_id=finding.rule_id,
                    points=points,
                    reason=finding.title,
                    file=finding.file,
                )
            )

    for key, label, weight in DIMENSION_META:
        deduction = dimension_deductions.get(key, 0)
        dimensions.append(
            DimensionInfo(
                key=key,
                label=label,
                weight=weight,
                deduction=deduction,
                score=max(0, weight - deduction),
            )
        )

    raw_total = sum(item.score for item in dimensions)
    caps = _caps(findings, dimension_deductions)
    strictest = min((cap.limit for cap in caps), default=raw_total)
    total = min(raw_total, strictest)
    grade = _grade_for(total)

    return ScoreInfo(
        total=total,
        raw_total=raw_total,
        grade=grade,
        verdict=VERDICTS[grade],
        caps=sorted(caps, key=lambda item: (item.limit, item.type)),
        dimensions=dimensions,
        deductions=deductions,
    )


def _caps(findings: list[Finding], dimension_deductions: dict[str, int]) -> list[CapInfo]:
    caps: list[CapInfo] = []
    seen: set[str] = set()

    def add(kind: str, limit: int, reason: str) -> None:
        if kind in seen:
            return
        seen.add(kind)
        caps.append(CapInfo(type=kind, limit=limit, reason=reason))

    for finding in findings:
        if (
            finding.rule_id in {"R10", "R11"}
            and finding.severity == "critical"
            and finding.cap_eligible
        ):
            label = "私钥材料" if finding.rule_id == "R10" else "访问令牌"
            add(
                "private_key" if finding.rule_id == "R10" else "secret_token",
                59,
                f"{finding.rule_id} 检测到{label}（{finding.file or '未知文件'}），总分上限 59",
            )
        if finding.rule_id == "R09" and finding.severity == "critical":
            add("env_committed", 69, f"R09 检测到真实值入库的 .env（{finding.file}），总分上限 69")
        if finding.rule_id == "R09" and finding.severity == "medium":
            add(
                "env_committed_placeholder",
                79,
                f"R09 检测到 .env 入库（{finding.file}，值为占位或空），总分上限 79",
            )

    docs_deps = dimension_deductions.get("docs", 0) + dimension_deductions.get("deps", 0)
    if docs_deps >= 12:
        add(
            "docs_deps",
            59,
            f"文档 + 依赖维度合计扣 {docs_deps} 分（≥12），总分上限 59",
        )
    elif docs_deps >= 8:
        add(
            "docs_deps",
            69,
            f"文档 + 依赖维度合计扣 {docs_deps} 分（8–11），总分上限 69",
        )

    return caps


def score_line(score: ScoreInfo) -> str:
    caps = "、".join(f"{cap.type}={cap.limit}" for cap in score.caps) or "无"
    return (
        f"total={score.total} raw={score.raw_total} grade={score.grade} "
        f"verdict={score.verdict} caps={caps}"
    )
