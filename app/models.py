"""响应模型：对外字段名即契约，改名视为破坏冻结契约。"""

from __future__ import annotations

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
    truncate_reason: str | None = None


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


class TimingInfo(BaseModel):
    meta_ms: int = 0
    commit_ms: int = 0
    tree_ms: int = 0


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
