"""响应模型：对外字段名即契约，改名视为破坏冻结契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class RepoInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    full_name: str
    stars: int
    language: str | None = None
    license: str | None = None
    archived: bool = False
    pushed_at: str | None = None


class ScopeInfo(BaseModel):
    files_scanned: int = 0
    files_skipped: int = 0
    bytes_scanned: int = 0
    truncated: bool = False
    truncate_reason: Literal["none", "github_tree_limit", "blob_cap"] = "none"


class FileEntry(BaseModel):
    path: str
    size: int
    is_text: bool


class SourceInfo(BaseModel):
    method: str = "trees_api"
    tree_ref_fallback: bool = False
    tree_truncated_by_github: bool = False


class StatsInfo(BaseModel):
    text_files: int = 0
    windows_script_count: int = 0
    total_blobs_in_tree: int = 0
    listed_blobs: int = 0
    bytes_total_listed: int = 0
    unlisted_blobs: int = 0
    content_requests: int = 0
    content_bytes: int = 0
    content_failed: int = 0
    content_files_available: int = 0


class Finding(BaseModel):
    """一条规则命中结论；字段即最终 schema，后续步骤不再改。"""

    id: str
    rule_id: str
    category: str
    dimension: str
    severity: Literal["high", "medium", "low", "info"]
    title: str
    file: str = ""
    line: int = 0
    evidence_masked: str
    impact: str
    fix: str
    hit_count: int = 1
    confidence: Literal["high", "medium", "low"] = "high"


class RuleInfo(BaseModel):
    id: str
    dimension: str
    severity: str
    points: int
    needs_content: bool


class Check(BaseModel):
    """每条规则的三态结论：pass / fail / degraded，basis 必须可复核。"""

    rule_id: str
    status: Literal["pass", "fail", "degraded"]
    basis: str
    file: str = ""
    line: int = 0


class RulesResponse(BaseModel):
    rules: list[RuleInfo]


class TimingInfo(BaseModel):
    meta_ms: int = 0
    commit_ms: int = 0
    tree_ms: int = 0
    tree_slow: bool = False


class InspectResponse(BaseModel):
    ok: bool = True
    provider: str = "github"
    request_count: int = 0
    source: SourceInfo
    mode: str = "metadata"
    repo: RepoInfo
    ref: str
    commit_sha: str
    scope: ScopeInfo
    stats: StatsInfo
    timing: TimingInfo
    files: list[FileEntry]
    findings: list[Finding] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    ok: bool = False
    error: ErrorBody


class InspectRequest(BaseModel):
    """请求体：repo_url 为主字段，兼容 url 别名。"""

    repo_url: str = Field(
        validation_alias=AliasChoices("repo_url", "url"),
        serialization_alias="repo_url",
    )
