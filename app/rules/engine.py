"""规则引擎：注册表、内容预取、结果合并、checks 与 findings 一致性。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..masking import mask_evidence
from ..models import Check, Finding
from ..snapshot import Snapshot
from ..treescan import is_text_file, sample_priority
from .base import Rule
from .deps import R03, R04, R05, is_parseable_manifest
from .docs import R01, R02, is_readme
from .evidence import level_of, select
from .portability import R06, R07, R08

RULES: tuple[Rule, ...] = (R01, R02, R03, R04, R05, R06, R07, R08)


@dataclass
class Evaluation:
    findings: list[Finding] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    penalties: dict[str, int] = field(default_factory=dict)


def plan_content_paths(snapshot: Snapshot) -> list[tuple[str, int]]:
    """预取顺序：README + 根层清单在最前，其余文本按 P1–P5 抽样优先级排。"""
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
    ordered: list[tuple[str, int]] = []
    for _, path, size in candidates:
        if path in seen:
            continue
        seen.add(path)
        ordered.append((path, size))

    pool = [
        (sample_priority(blob.path), blob.path, blob.size)
        for blob in snapshot.blobs
        if is_text_file(blob.path, blob.size)
    ]
    pool.sort(key=lambda item: (item[0], item[1]))
    for _, path, size in pool:
        if path in seen:
            continue
        seen.add(path)
        ordered.append((path, size))
    return ordered


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
    penalties: dict[str, int] = {}
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
        if outcome.extra_findings:
            findings.extend(outcome.extra_findings)
        if outcome.penalty:
            penalties[rule.id] = outcome.penalty

    merged = merge_findings(findings)
    _assert_consistency(checks, merged)
    return Evaluation(findings=merged, checks=checks, penalties=penalties)
