"""响应模型：对外字段名即契约，改名视为破坏冻结契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from .version import ENGINE_VERSION, SCHEMA_VERSION


class RepoInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    full_name: str = ""
    stars: int = 0
    language: str | None = None
    license: str | None = None
    archived: bool = False
    pushed_at: str | None = None
    # v3.1 契约字段（对象内保留 full_name/stars 等既有兼容字段）
    owner: str = ""
    name: str = ""
    default_branch: str = ""
    head_sha: str = ""


class CommitInfo(BaseModel):
    sha: str = ""
    message: str = ""
    author: str = ""
    date: str = ""


class ScanInfo(BaseModel):
    engine_version: str = ENGINE_VERSION
    scanned_at: str = ""
    duration_ms: int = 0


class CacheInfo(BaseModel):
    hit: bool = False
    stale: bool = False
    cached_at: str | None = None


class WarningInfo(BaseModel):
    code: str
    message: str


class ErrorInfo(BaseModel):
    code: str
    message: str
    hint: str = ""
    retry_after: int | None = None


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
    path_scan_files: int = 0
    path_scan_bytes: int = 0
    content_total_text_files: int = 0
    secret_scan_files: int = 0
    secret_scan_bytes: int = 0
    secret_sampling_ratio: float = 0.0


class Finding(BaseModel):
    """一条规则命中结论；字段即最终 schema，后续步骤不再改。"""

    id: str
    rule_id: str
    category: str
    dimension: str
    severity: Literal["critical", "high", "medium", "low", "info"]
    title: str
    file: str = ""
    line: int = 0
    evidence_masked: str
    impact: str
    fix: str
    hit_count: int = 1
    confidence: Literal["high", "medium", "low"] = "high"
    cap_eligible: bool = False


class RuleInfo(BaseModel):
    id: str
    dimension: str
    severity: str
    points: int
    needs_content: bool


class Check(BaseModel):
    """每条规则的四态结论：pass / fail / degraded / n/a，basis 必须可复核。"""

    rule_id: str
    status: Literal["pass", "fail", "degraded", "n/a"]
    basis: str
    file: str = ""
    line: int = 0


class RulesResponse(BaseModel):
    rules: list[RuleInfo]


class AdviceCacheInfo(BaseModel):
    hit: bool = False
    cached_at: str | None = None


class AdviceAction(BaseModel):
    title: str
    why: str
    how: str
    effort: Literal["S", "M", "L"]
    rule_ids: list[str] = Field(default_factory=list)


class AdviceResponse(BaseModel):
    """AI 接入建议的稳定对外结构；模型和规则 fallback 共用同一 schema。"""

    schema_version: int = SCHEMA_VERSION
    ok: bool = True
    source: Literal["model", "rules_fallback"] = "rules_fallback"
    model: str | None = None
    prompt_version: str = "advice-v2"
    latency_ms: int = 0
    tokens: int = 0
    summary: str = ""
    actions: list[AdviceAction] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    cache: AdviceCacheInfo = Field(default_factory=AdviceCacheInfo)
    warnings: list[WarningInfo] = Field(default_factory=list)
    fallback_reason: str | None = None
    finding_count: int = 0
    sampling_ratio: float = 0.0
    repo: str = ""
    commit_sha: str = ""


class AdviceRequest(BaseModel):
    """POST /api/advice 请求体；repo 为主字段，兼容旧的 repo_url。"""

    repo: str = Field(
        validation_alias=AliasChoices("repo", "repo_url"),
        serialization_alias="repo",
    )
    refresh: int = Field(0, ge=0, le=1)


class TimingInfo(BaseModel):
    meta_ms: int = 0
    commit_ms: int = 0
    tree_ms: int = 0
    tree_slow: bool = False
    tree_attempts: int = 1


class CapInfo(BaseModel):
    type: str
    limit: int
    reason: str


class DimensionInfo(BaseModel):
    key: str
    label: str
    weight: int
    deduction: int
    score: int


class DeductionInfo(BaseModel):
    rule_id: str
    points: int
    reason: str
    file: str = ""


class ScoreInfo(BaseModel):
    total: int = 0
    raw_total: int = 0
    grade: Literal["A", "B", "C", "D"] = "D"
    verdict: str = "不建议接入"
    caps: list[CapInfo] = Field(default_factory=list)
    dimensions: list[DimensionInfo] = Field(default_factory=list)
    deductions: list[DeductionInfo] = Field(default_factory=list)


class InspectResponse(BaseModel):
    """v3.1 顶层契约；旧字段保留为兼容别名，页面逐步改用新字段。"""

    schema_version: int = SCHEMA_VERSION
    ok: bool = True
    provider: str = "github"
    request_count: int = 0
    source: SourceInfo
    mode: str = "metadata"
    repo: RepoInfo
    ref: str
    commit_sha: str
    commit: CommitInfo = Field(default_factory=CommitInfo)
    scan: ScanInfo = Field(default_factory=ScanInfo)
    scope: ScopeInfo
    stats: StatsInfo
    timing: TimingInfo
    files: list[FileEntry]
    findings: list[Finding] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)
    score: ScoreInfo = Field(default_factory=ScoreInfo)
    cache: CacheInfo = Field(default_factory=CacheInfo)
    warnings: list[WarningInfo] = Field(default_factory=list)
    error: ErrorInfo | None = None
    # 兼容第 5 步的顶层字段；新代码以 scan / commit 为准。
    engine_version: str = ENGINE_VERSION
    scanned_at: str = ""


class ErrorResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    ok: bool = False
    error: ErrorInfo


class HealthResponse(BaseModel):
    ok: bool = True
    engine_version: str = ENGINE_VERSION
    schema_version: int = SCHEMA_VERSION
    rules_count: int


class InspectRequest(BaseModel):
    """请求体：repo_url 为主字段，兼容 url 别名。"""

    repo_url: str = Field(
        validation_alias=AliasChoices("repo_url", "url"),
        serialization_alias="repo_url",
    )
