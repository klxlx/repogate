"""规则引擎：注册表、内容预取计划、结果合并、checks 与 findings 一致性。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..masking import mask_evidence
from ..models import Check, Finding
from ..snapshot import Snapshot
from .base import Rule
from .deps import PARSEABLE_MANIFESTS, R03, R04, R05, _basename
from .docs import R01, R02, find_root_readme

RULES: tuple[Rule, ...] = (R01, R02, R03, R04, R05)


@dataclass
class Evaluation:
    findings: list[Finding] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)


def plan_content_paths(snapshot: Snapshot) -> list[tuple[str, int]]:
    """本步只为 README 与三类可解析清单取内容；锁文件只查存在性。"""
    wanted: list[tuple[str, int]] = []
    readme = find_root_readme(snapshot)
    if readme is not None:
        size = next(blob.size for blob in snapshot.blobs if blob.path == readme)
        wanted.append((readme, size))
    for blob in snapshot.shallow_blobs():
        if _basename(blob.path) in PARSEABLE_MANIFESTS:
            wanted.append((blob.path, blob.size))

    seen: set[str] = set()
    unique: list[tuple[str, int]] = []
    for path, size in wanted:
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
    """硬约束：fail 必须有 finding；pass 不许有 finding；每规则只出现一次。"""
    rule_ids = [check.rule_id for check in checks]
    assert len(rule_ids) == len(set(rule_ids)) == len(RULES), "checks 必须每条规则一条"
    fail_rules = {check.rule_id for check in checks if check.status == "fail"}
    pass_rules = {check.rule_id for check in checks if check.status == "pass"}
    finding_rules = {finding.rule_id for finding in findings}
    assert fail_rules <= finding_rules, "fail 规则缺少对应 finding"
    assert not (pass_rules & finding_rules), "pass 规则不应有 finding"
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
