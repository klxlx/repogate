"""规则引擎：注册表、内容预取计划、结果合并。"""

from __future__ import annotations

from ..models import Finding
from ..snapshot import Snapshot
from .base import Rule
from .deps import PARSEABLE_MANIFESTS, R03, R04, R05, _basename
from .docs import R01, R02, find_root_readme

RULES: tuple[Rule, ...] = (R01, R02, R03, R04, R05)


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


def evaluate(snapshot: Snapshot) -> list[Finding]:
    collected: list[Finding] = []
    for rule in RULES:
        finding = rule.detect(snapshot)
        if finding is not None:
            collected.append(finding)
    return merge_findings(collected)
