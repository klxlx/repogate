"""规则引擎：注册表、内容预取、结果合并、checks 与 findings 一致性。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..masking import mask_evidence
from ..models import Check, Finding
from ..snapshot import Snapshot
from .base import Rule
from .deps import R03, R04, R05, is_parseable_manifest
from .docs import R01, R02, is_readme
from .evidence import level_of, select

RULES: tuple[Rule, ...] = (R01, R02, R03, R04, R05)


@dataclass
class Evaluation:
    findings: list[Finding] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)


def plan_content_paths(snapshot: Snapshot) -> list[tuple[str, int]]:
    """按证据层级排预取顺序：根目录优先，弱目录不取，避免浪费 12 个配额。"""
    candidates: list[tuple[int, str, int]] = []

    readme = select(snapshot.blobs, is_readme)
    if readme is not None and not readme.weak:
        size = next(blob.size for blob in snapshot.blobs if blob.path == readme.path)
        candidates.append((readme.level, readme.path, size))

    manifest = select(snapshot.blobs, is_parseable_manifest)
    if manifest is not None and not manifest.weak:
        for blob in snapshot.blobs:
            if level_of(blob.path) == manifest.level and is_parseable_manifest(blob.path):
                candidates.append((manifest.level, blob.path, blob.size))

    candidates.sort(key=lambda item: (item[0], item[1]))
    seen: set[str] = set()
    unique: list[tuple[str, int]] = []
    for _, path, size in candidates:
        if path in seen:
            continue
        seen.add(path)
        unique.append((path, size))
    return unique


def merge_findings(findings: list[Finding]) -> list[Finding]:
    """同一 rule_id + 同一 file 合并为一条，累加 hit_count。"""
    merged: dict[tuple[str, str], Finding] = {}
    order: list[tuple[str, str]] = []
    for finding in findings:
        key = (finding.rule_id, finding.file)
        if key in merged:
            merged[key].hit_count += finding.hit_count
        else:
            merged[key] = finding
            order.append(key)
    return [merged[key] for key in order]


def _assert_consistency(checks: list[Check], findings: list[Finding]) -> None:
    """硬约束：fail 必须有 finding；pass / n/a 不许有 finding；每规则只出现一次。"""
    rule_ids = [check.rule_id for check in checks]
    assert len(rule_ids) == len(set(rule_ids)) == len(RULES), "checks 必须每条规则一条"
    fail_rules = {check.rule_id for check in checks if check.status == "fail"}
    clean_rules = {check.rule_id for check in checks if check.status in ("pass", "n/a")}
    finding_rules = {finding.rule_id for finding in findings}
    assert fail_rules <= finding_rules, "fail 规则缺少对应 finding"
    assert not (clean_rules & finding_rules), "pass / n/a 规则不应有 finding"
    for check in checks:
        assert check.basis.strip(), f"{check.rule_id} 的 basis 不能为空"


def evaluate(snapshot: Snapshot) -> Evaluation:
    findings: list[Finding] = []
    checks: list[Check] = []
    for rule in RULES:
        outcome = rule.detect(snapshot)
        checks.append(
            Check(
                rule_id=rule.id,
                status=outcome.status,
                basis=mask_evidence(outcome.basis),
                file=outcome.file,
                line=outcome.line,
            )
        )
        if outcome.finding is not None:
            findings.append(outcome.finding)

    merged = merge_findings(findings)
    _assert_consistency(checks, merged)
    return Evaluation(findings=merged, checks=checks)
