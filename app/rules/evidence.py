"""证据优先级：根目录 > 一层普通子目录 > 弱目录，禁止跨层"找到就算"。"""

from __future__ import annotations

from dataclasses import dataclass

from ..snapshot import BlobRef

LEVEL_ROOT = 0
LEVEL_CHILD = 1
LEVEL_WEAK = 2

WEAK_DIRS = frozenset(
    {
        "docs", "doc", "tools", "scripts", "examples", "test", "tests",
        "fixtures", ".devcontainer", ".github",
    }
)
LEVEL_LABEL = {LEVEL_ROOT: "根目录", LEVEL_CHILD: "一层子目录", LEVEL_WEAK: "弱目录"}


def level_of(path: str) -> int | None:
    """0 根目录 / 1 一层普通子目录 / 2 弱目录；超过一层返回 None（不参与）。"""
    depth = path.count("/")
    if depth == 0:
        return LEVEL_ROOT
    if depth == 1:
        return LEVEL_WEAK if path.split("/", 1)[0] in WEAK_DIRS else LEVEL_CHILD
    return None


@dataclass(frozen=True)
class Pick:
    path: str
    level: int
    count: int

    @property
    def weak(self) -> bool:
        return self.level == LEVEL_WEAK

    def basis(self) -> str:
        """统一文案：带层级；弱目录明确写"无法确认主项目"。"""
        if self.level == LEVEL_WEAK:
            return f"仅在弱目录 {self.path} 发现，无法确认主项目"
        note = f"（共 {self.count} 个候选）" if self.count > 1 else ""
        return f"{LEVEL_LABEL[self.level]} {self.path}{note}"


def select(blobs: tuple[BlobRef, ...], predicate) -> Pick | None:
    """只在最高非空层级里取第一个（同层按路径字典序），并记录候选总数。"""
    buckets: dict[int, list[str]] = {LEVEL_ROOT: [], LEVEL_CHILD: [], LEVEL_WEAK: []}
    for blob in blobs:
        level = level_of(blob.path)
        if level is not None and predicate(blob.path):
            buckets[level].append(blob.path)
    for level in (LEVEL_ROOT, LEVEL_CHILD, LEVEL_WEAK):
        if buckets[level]:
            ordered = sorted(buckets[level])
            return Pick(path=ordered[0], level=level, count=len(ordered))
    return None
