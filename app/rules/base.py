"""规则基类：声明元数据 + 纯函数 detect；规则层不得发起任何网络请求。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal

from ..masking import mask_evidence
from ..models import Finding, RuleInfo
from ..snapshot import Snapshot

OutcomeStatus = Literal["pass", "fail", "degraded", "n/a"]

SEVERITY_RANK: dict[str, int] = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
MEDIUM_RANK = SEVERITY_RANK["medium"]


@dataclass
class RuleOutcome:
    """一条规则的判定结果：状态 + 可复核依据 + 可选 finding。"""

    status: OutcomeStatus
    basis: str
    finding: Finding | None = None
    file: str = ""
    line: int = 0
    penalty: int = 0
    extra_findings: list[Finding] = field(default_factory=list)


@dataclass(frozen=True)
class Rule:
    id: str
    slug: str
    dimension: str
    severity: str
    points: int
    needs_content: bool
    detect: Callable[[Snapshot], RuleOutcome]

    def info(self) -> RuleInfo:
        return RuleInfo(
            id=self.id,
            dimension=self.dimension,
            severity=self.severity,
            points=self.points,
            needs_content=self.needs_content,
        )


def make_finding(
    rule: Rule,
    *,
    title: str,
    evidence: str,
    impact: str,
    fix: str,
    file: str = "",
    line: int = 0,
    severity: str | None = None,
    confidence: str = "high",
    keep_paths: bool = False,
    hit_count: int = 1,
    cap_eligible: bool = False,
) -> Finding:
    return Finding(
        id=f"{rule.id}-{rule.slug}",
        rule_id=rule.id,
        category=rule.dimension,
        dimension=rule.dimension,
        severity=severity or rule.severity,
        title=title,
        file=file,
        line=line,
        evidence_masked=mask_evidence(evidence, keep_paths=keep_paths),
        impact=impact,
        fix=fix,
        hit_count=hit_count,
        confidence=confidence,
        cap_eligible=cap_eligible,
    )


def passed(rule: Rule, basis: str, *, file: str = "", line: int = 0) -> RuleOutcome:
    return RuleOutcome(status="pass", basis=basis, file=file, line=line)


def failed(rule: Rule, basis: str, finding: Finding, *, penalty: int = 0) -> RuleOutcome:
    """fail 必须携带 finding，保证 checks 与 findings 对得上。"""
    return RuleOutcome(
        status="fail",
        basis=basis,
        finding=finding,
        file=finding.file,
        line=finding.line,
        penalty=penalty,
    )


def conclusion(
    rule: Rule, basis: str, findings: list[Finding], *, penalty: int = 0
) -> RuleOutcome:
    """v2.0 状态语义：按最高 severity 判定，medium 及以上才 fail。

    只有 low / info 时判 pass，但 finding 保留，basis 明确「仅提示 N 条」；
    这些提示是否计分由第 5 步 score.deductions 统一给出，不在规则层承诺「不扣分」。
    """
    if not findings:
        return passed(rule, basis)

    ordered = sorted(findings, key=lambda item: SEVERITY_RANK[item.severity], reverse=True)
    top = ordered[0]
    if SEVERITY_RANK[top.severity] >= MEDIUM_RANK:
        return RuleOutcome(
            status="fail",
            basis=basis,
            finding=top,
            file=top.file,
            line=top.line,
            penalty=penalty,
            extra_findings=ordered[1:],
        )
    return RuleOutcome(
        status="pass",
        basis=f"{basis}；仅提示 {len(ordered)} 条",
        finding=None,
        file=top.file,
        line=top.line,
        extra_findings=ordered,
    )


def na(rule: Rule, basis: str, *, file: str = "", line: int = 0) -> RuleOutcome:
    """不适用：既不是通过也不是失败，评分时不计入分母（接口预留）。"""
    return RuleOutcome(status="n/a", basis=basis, file=file, line=line)


def degraded(
    rule: Rule,
    basis: str,
    *,
    finding: Finding | None = None,
    file: str = "",
    line: int = 0,
) -> RuleOutcome:
    """结论不完整：不许当 pass；依据文件全失败时可以不带 finding。"""
    return RuleOutcome(
        status="degraded",
        basis=basis,
        finding=finding,
        file=file or (finding.file if finding else ""),
        line=line or (finding.line if finding else 0),
    )


def degraded_finding(
    rule: Rule,
    *,
    reason: str,
    title: str,
    impact: str,
    fix: str,
    file: str = "",
    line: int = 0,
    detail: str = "",
) -> Finding:
    """部分依据可用时仍输出 info Finding，显式标注结论不完整。"""
    evidence = f"内容不可用（{reason}），无法完整判定"
    if detail:
        evidence = f"{evidence}：{detail}"
    return make_finding(
        rule,
        title=title,
        evidence=evidence,
        impact=impact,
        fix=fix,
        file=file,
        line=line,
        severity="info",
        confidence="low",
    )
