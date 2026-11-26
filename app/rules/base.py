"""规则基类：声明元数据 + 纯函数 detect；规则层不得发起任何网络请求。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from ..masking import mask_evidence
from ..models import Finding, RuleInfo
from ..snapshot import Snapshot

OutcomeStatus = Literal["pass", "fail", "degraded"]


@dataclass
class RuleOutcome:
    """一条规则的判定结果：状态 + 可复核依据 + 可选 finding。"""

    status: OutcomeStatus
    basis: str
    finding: Finding | None = None
    file: str = ""
    line: int = 0


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
        evidence_masked=mask_evidence(evidence),
        impact=impact,
        fix=fix,
        hit_count=1,
        confidence=confidence,
    )


def passed(rule: Rule, basis: str, *, file: str = "", line: int = 0) -> RuleOutcome:
    return RuleOutcome(status="pass", basis=basis, file=file, line=line)


def failed(rule: Rule, basis: str, finding: Finding) -> RuleOutcome:
    """fail 必须携带 finding，保证 checks 与 findings 对得上。"""
    return RuleOutcome(
        status="fail",
        basis=basis,
        finding=finding,
        file=finding.file,
        line=finding.line,
    )


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
