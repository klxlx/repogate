"""R12 / R13 / R14：维护状态（只看仓库元信息与根目录文件名，不读内容）。"""

from __future__ import annotations

import datetime as dt
import re

from ..snapshot import Snapshot
from .base import Rule, RuleOutcome, conclusion, degraded, make_finding, passed

_ROOT_LICENSE_RE = re.compile(r"^(?:licen[cs]e|copying)(?:\.(?:md|txt))?$")


def _root_license(snapshot: Snapshot) -> str | None:
    for blob in snapshot.blobs:
        if "/" in blob.path:
            continue
        if _ROOT_LICENSE_RE.match(blob.path.lower()):
            return blob.path
    return None


def detect_license_missing(snapshot: Snapshot) -> RuleOutcome:
    license_path = _root_license(snapshot)
    if license_path:
        return passed(R12, f"根目录存在许可证文件 {license_path}", file=license_path)

    finding = make_finding(
        R12,
        title="根目录缺少许可证文件",
        evidence=(
            "根目录未找到 LICENSE / LICENCE / COPYING（允许 .md / .txt 后缀）；"
            f"GitHub 元信息 license={snapshot.license or '未标注'}"
        ),
        impact="使用者无法确认授权范围，企业内部接入存在法律与合规不确定性",
        fix="在根目录补充 LICENSE（如 MIT / Apache-2.0），并与仓库元信息保持一致",
        file="",
        severity="low",
        confidence="high",
    )
    return conclusion(R12, "根目录无 LICENSE / LICENCE / COPYING", [finding], penalty=0)


def _parse_pushed_at(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def detect_stale_repo(snapshot: Snapshot) -> RuleOutcome:
    pushed = _parse_pushed_at(snapshot.pushed_at)
    if pushed is None:
        return degraded(
            R13,
            f"pushed_at 缺失或无法解析（{snapshot.pushed_at or '空'}），无法判断是否超过 12 个月",
        )

    now = snapshot.scanned_at
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    age = now - pushed
    days = age.days
    if days > 365:
        finding = make_finding(
            R13,
            title="仓库超过 12 个月没有推送",
            evidence=f"pushed_at={snapshot.pushed_at}，距扫描时约 {days} 天",
            impact="依赖可能已停止维护，安全修复与兼容性更新无法保证",
            fix="确认上游是否仍在维护；否则锁定版本或寻找替代方案",
            file="",
            severity="low",
            confidence="high",
        )
        return conclusion(
            R13, f"最近推送距今约 {days} 天（> 12 个月）", [finding], penalty=0
        )

    return passed(R13, f"最近推送距今约 {max(days, 0)} 天（≤ 12 个月）")


def detect_archived(snapshot: Snapshot) -> RuleOutcome:
    if snapshot.archived:
        finding = make_finding(
            R14,
            title="仓库已归档",
            evidence="GitHub 元信息 archived=true",
            impact="仓库已停止接受更新，长期使用会累积无法修复的安全风险",
            fix="优先选择仍在维护的替代仓库；确需使用时锁定 commit 并自行维护",
            file="",
            severity="low",
            confidence="high",
        )
        return conclusion(R14, "仓库 archived=true", [finding], penalty=0)
    return passed(R14, "仓库未归档（archived=false）")


R12 = Rule(
    id="R12",
    slug="license-missing",
    dimension="maintenance",
    severity="low",
    points=-4,
    needs_content=False,
    detect=detect_license_missing,
)

R13 = Rule(
    id="R13",
    slug="stale-repo",
    dimension="maintenance",
    severity="low",
    points=-3,
    needs_content=False,
    detect=detect_stale_repo,
)

R14 = Rule(
    id="R14",
    slug="archived",
    dimension="maintenance",
    severity="low",
    points=-3,
    needs_content=False,
    detect=detect_archived,
)
