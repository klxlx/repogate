"""规则基类：声明元数据 + 纯函数 detect；规则层不得发起任何网络请求。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..masking import mask_evidence
from ..models import Finding, RuleInfo
from ..snapshot import Snapshot


@dataclass(frozen=True)
class Rule:
    id: str
    slug: str
    dimension: str
    severity: str
    points: int
    needs_content: bool
    detect: Callable[[Snapshot], Finding | None]

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
    """内容拉取失败：结论降级为 info，保留规则位置但显式标注不可判定。"""
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
