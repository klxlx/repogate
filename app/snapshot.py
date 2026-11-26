"""扫描快照：规则层唯一输入，规则只读这里，不得发起网络请求。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .treescan import is_excluded_dir, is_safe_path


@dataclass(frozen=True)
class BlobRef:
    path: str
    size: int


@dataclass
class Snapshot:
    owner: str
    repo: str
    commit_sha: str
    ref: str
    blobs: tuple[BlobRef, ...]
    tree_ref_fallback: bool = False
    tree_truncated_by_github: bool = False
    contents: dict[str, str] = field(default_factory=dict)
    failed_paths: frozenset[str] = frozenset()
    text_total: int = 0

    def shallow_blobs(self) -> list[BlobRef]:
        """根目录与一层子目录：path 中最多一个斜杠。"""
        return [blob for blob in self.blobs if blob.path.count("/") <= 1]

    def root_blobs(self) -> list[BlobRef]:
        return [blob for blob in self.blobs if "/" not in blob.path]

    def has_content(self, path: str) -> bool:
        return path in self.contents

    def content_failed(self, path: str) -> bool:
        return path in self.failed_paths


def build_snapshot(bundle, owner: str, repo: str) -> Snapshot:
    """把 GitHub tree 归一成规则可消费的 blob 列表；排除目录在入口处就剔掉。"""
    entries = bundle.tree.get("tree") or []
    blobs = tuple(
        BlobRef(path=item["path"], size=int(item.get("size") or 0))
        for item in entries
        if isinstance(item, dict)
        and item.get("type") == "blob"
        and isinstance(item.get("path"), str)
        and is_safe_path(item["path"])
        and not is_excluded_dir(item["path"])
    )
    return Snapshot(
        owner=owner,
        repo=repo,
        commit_sha=bundle.commit_sha,
        ref=bundle.ref,
        blobs=blobs,
        tree_ref_fallback=bundle.tree_ref_fallback,
        tree_truncated_by_github=bundle.tree_truncated_by_github,
    )
