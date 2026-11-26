"""R01 / R02：README 是否存在、是否覆盖安装/运行/配置/依赖四类说明。"""

from __future__ import annotations

import re

from ..snapshot import Snapshot
from .base import Rule, RuleOutcome, degraded, failed, make_finding, passed

README_PATTERN = re.compile(r"^readme(\.(md|markdown|rst|txt))?$", re.IGNORECASE)
README_HEAD_BYTES = 20 * 1024

CATEGORY_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("A 安装/构建", (r"\binstall", r"\bsetup", r"\bbuild", r"\bcompile", "安装", "构建", "编译")),
    ("B 运行/使用", (r"\brun", r"\busage", r"\bstart", r"\bexecute", "运行", "启动", "使用")),
    (
        "C 配置/环境",
        (r"\bconfig", r"\benvironment", r"\benv\s+var", r"\bsettings", "配置", "环境变量"),
    ),
    ("D 依赖/前置", (r"\brequirement", r"\bdependency", r"\bprerequisite", "依赖", "前置")),
)


def find_root_readme(snapshot: Snapshot) -> str | None:
    candidates = [blob.path for blob in snapshot.root_blobs() if README_PATTERN.match(blob.path)]
    if not candidates:
        return None
    # 有扩展名的优先（README.md 优于无扩展 README），再按字典序
    return sorted(candidates, key=lambda name: (0 if "." in name else 1, name.lower()))[0]


def _covers(text: str, markers: tuple[str, ...]) -> bool:
    return any(re.search(marker, text, re.IGNORECASE) for marker in markers)


def detect_readme_missing(snapshot: Snapshot) -> RuleOutcome:
    readme = find_root_readme(snapshot)
    if readme is not None:
        suffix = "（根目录，无扩展名）" if "." not in readme else "（根目录）"
        return passed(R01, f"命中 README{suffix}", file=readme)

    finding = make_finding(
        R01,
        title="缺少 README 说明文件",
        evidence="未找到 README.md / .markdown / .rst / .txt 或无扩展 README（已检查根目录）",
        impact="外部使用者无法快速了解仓库用途、安装与运行方式，接入评估缺少第一手信息",
        fix="在根目录补充 README.md，至少说明用途、安装、运行与依赖",
    )
    return failed(
        R01,
        "根目录无 README（已检查 .md/.markdown/.rst/.txt 与无扩展 README）",
        finding,
    )


def detect_readme_incomplete(snapshot: Snapshot) -> RuleOutcome:
    readme = find_root_readme(snapshot)
    if readme is None:
        return degraded(R02, "未评估：R01 未通过，缺少 README")

    if not snapshot.has_content(readme):
        return degraded(
            R02,
            f"{readme} 读取失败，缺 README 内容，结论不完整",
            file=readme,
        )

    text = snapshot.contents[readme][:README_HEAD_BYTES]
    covered = [name for name, markers in CATEGORY_MARKERS if _covers(text, markers)]
    if len(covered) >= 2:
        return passed(
            R02,
            f"README 覆盖 {len(covered)} 类说明：{'、'.join(covered)}",
            file=readme,
        )

    missing = [name for name, _ in CATEGORY_MARKERS if name not in covered]
    covered_text = "、".join(covered) if covered else "无"
    finding = make_finding(
        R02,
        title="README 说明不完整",
        evidence=(
            f"README 前 20KB 只覆盖 {len(covered)} 类说明（{covered_text}）；"
            f"缺少：{'、'.join(missing)}"
        ),
        impact="使用者难以按文档完成安装与启动，接入成本被低估",
        fix="补齐缺失的说明类别，至少覆盖安装、运行、配置与依赖",
        file=readme,
    )
    return failed(
        R02,
        f"README 缺 {'、'.join(missing)} 类章节（前 20KB 只覆盖 {len(covered)} 类）",
        finding,
    )


R01 = Rule(
    id="R01",
    slug="readme-missing",
    dimension="docs",
    severity="high",
    points=-10,
    needs_content=False,
    detect=detect_readme_missing,
)

R02 = Rule(
    id="R02",
    slug="readme-incomplete",
    dimension="docs",
    severity="medium",
    points=-4,
    needs_content=True,
    detect=detect_readme_incomplete,
)
