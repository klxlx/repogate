"""RepoGate Web 入口：健康检查、JSON 检查接口、SSR 首页。"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import cache
from .advice import build_advice
from .content import fetch_contents
from .errors import ScanError, internal, invalid_url
from .github import (
    RepoHead,
    close_client,
    fetch_repo_bundle,
    parse_repo_url,
    resolve_head,
)
from .models import (
    AdviceRequest,
    AdviceResponse,
    CacheInfo,
    CommitInfo,
    HealthResponse,
    InspectRequest,
    InspectResponse,
    RepoInfo,
    RulesResponse,
    ScanInfo,
    SourceInfo,
    WarningInfo,
)
from .rules import RULES, evaluate, plan_content_paths
from .scoring import build_score, finding_deduction, score_line
from .snapshot import build_snapshot
from .treescan import scan_tree
from .version import ENGINE_VERSION, SCHEMA_VERSION

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger("repogate")

_HINT_SUFFIX_RE = re.compile(r"；(?:另)?(?:仅)?提示 \d+ 条(?:（-\d+）)?$")


def _configure_logging() -> None:
    """uvicorn 不配置业务 logger，这里挂独立 handler，保证终端能看到请求预算与扫描统计。"""
    if any(isinstance(handler, logging.StreamHandler) for handler in logger.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


@asynccontextmanager
async def lifespan(_: FastAPI):
    _configure_logging()
    try:
        yield
    finally:
        await close_client()


app = FastAPI(
    title="RepoGate",
    version=ENGINE_VERSION,
    description="公开 GitHub 仓库准入检查：规则、评分、commit 缓存与 stale 兜底。",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _filesize(value: int | None) -> str:
    if value is None:
        return "-"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


templates.env.filters["filesize"] = _filesize

RULE_DIMENSIONS = {rule.id: rule.dimension for rule in RULES}
STATE_LABELS = {
    "pass": "通过",
    "hint": "提示",
    "fail": "未通过",
    "na": "不适用",
}


def _check_rows(checks, findings) -> list[dict]:
    """页面矩阵：四色状态。pass + 有 low/info、degraded 统一显示「提示」，basis 说明原因。"""
    by_rule: dict[str, list] = {}
    for finding in findings:
        by_rule.setdefault(finding.rule_id, []).append(finding)

    rows = []
    for check in checks:
        if check.status == "fail":
            state = "fail"
        elif check.status == "n/a":
            state = "na"
        elif check.status == "degraded":
            state = "hint"
        else:
            state = "hint" if by_rule.get(check.rule_id) else "pass"
        rows.append(
            {
                "rule_id": check.rule_id,
                "state": state,
                "label": STATE_LABELS[state],
                "basis": check.basis,
                "file": check.file,
            }
        )
    return rows


def _dimension_rows(score) -> list[dict]:
    by_dimension: dict[str, list] = {}
    for deduction in score.deductions:
        dimension = RULE_DIMENSIONS.get(deduction.rule_id, "")
        by_dimension.setdefault(dimension, []).append(deduction)
    return [
        {"dimension": dimension, "deductions": by_dimension.get(dimension.key, [])}
        for dimension in score.dimensions
    ]


def _local_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone().isoformat(timespec="seconds")


def _cache_state(result: InspectResponse) -> str:
    if result.cache.stale:
        return "陈旧"
    if result.cache.hit:
        return "命中"
    return "新扫"


def _cap_rows(score) -> tuple[list[dict], str]:
    """门槛区：已检查 / 已生效 / 未生效（原始分更低）。"""
    if not score.caps:
        return [], "已检查，未触发"

    rows = []
    active = False
    for cap in score.caps:
        if score.raw_total < cap.limit:
            state = "未生效（原始分更低）"
        elif score.total < score.raw_total and cap.limit == score.total:
            state = "已生效"
            active = True
        else:
            state = "已检查"
        rows.append({"cap": cap, "state": state})

    if active:
        overall = "已生效"
    elif score.raw_total < min(cap.limit for cap in score.caps):
        overall = "未生效（原始分更低）"
    else:
        overall = "已检查"
    return rows, overall


def _annotate_check_basis(checks, findings, score, ratio: float) -> None:
    """把规则层「提示 N 条」补成「提示 N 条（-X）」；X 直接来自评分函数。"""
    by_rule: dict[str, list] = {}
    for finding in findings:
        by_rule.setdefault(finding.rule_id, []).append(finding)

    for check in checks:
        items = [
            item
            for item in by_rule.get(check.rule_id, [])
            if item.severity in ("low", "info")
        ]
        if not items:
            continue
        points = sum(abs(finding_deduction(item, ratio)) for item in items)
        count = len(items)
        if check.status == "pass":
            label = f"提示 {count} 条（-{points}）" if points else f"仅提示 {count} 条"
        else:
            label = f"另有提示 {count} 条（-{points}）" if points else f"另有提示 {count} 条"
        check.basis = f"{_HINT_SUFFIX_RE.sub('', check.basis).strip()}；{label}"


def _result_context(result: InspectResponse) -> dict:
    findings = result.findings
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    ordered = sorted(
        findings,
        key=lambda item: (severity_order[item.severity], item.rule_id, item.file),
    )
    cap_rows, cap_state = _cap_rows(result.score)
    stale_warning = result.warnings[0].message if result.warnings else ""
    return {
        "result": result,
        "result_data": result.model_dump(mode="json"),
        "check_rows": _check_rows(result.checks, findings),
        "dimension_rows": _dimension_rows(result.score),
        "risk_visible": [item for item in ordered if item.severity not in ("low", "info")],
        "risk_hidden": [item for item in ordered if item.severity in ("low", "info")],
        "blocking_count": sum(1 for item in findings if item.severity == "critical"),
        "high_count": sum(1 for item in findings if item.severity == "high"),
        "sampling_pct": f"{result.stats.secret_sampling_ratio * 100:.2f}%",
        "scan_time_local": _local_time(result.scan.scanned_at),
        "cache_time_local": _local_time(result.cache.cached_at),
        "cache_state": _cache_state(result),
        "stale_warning": stale_warning,
        "cap_rows": cap_rows,
        "cap_state": cap_state,
    }


def _cached_response(cached: cache.CachedReport, *, hit: bool, stale: bool) -> InspectResponse:
    result = InspectResponse.model_validate(cached.report)
    result.cache = CacheInfo(hit=hit, stale=stale, cached_at=cached.cached_at)
    result.warnings = []
    result.error = None
    if stale:
        result.warnings = [
            WarningInfo(
                code="STALE_CACHE",
                message=(
                    f"上游限流，以下为 {_local_time(cached.cached_at)} 的缓存结果"
                    f"（commit {result.commit.sha[:7] or result.commit_sha[:7]}），可能已过期"
                ),
            )
        ]
    return result


def _make_repo_info(bundle, owner: str, repo: str) -> RepoInfo:
    full_name = bundle.repo.get("full_name") or f"{owner}/{repo}"
    return RepoInfo(
        full_name=full_name,
        stars=int(bundle.repo.get("stars") or 0),
        language=bundle.repo.get("language"),
        license=bundle.repo.get("license"),
        archived=bool(bundle.repo.get("archived")),
        pushed_at=bundle.repo.get("pushed_at"),
        owner=owner,
        name=repo,
        default_branch=bundle.ref,
        head_sha=bundle.commit_sha,
    )


def _make_commit_info(bundle) -> CommitInfo:
    data = bundle.commit or {}
    return CommitInfo(
        sha=data.get("sha") or bundle.commit_sha,
        message=data.get("message") or "",
        author=data.get("author") or "",
        date=data.get("date") or "",
    )


async def _inspect(repo_url: str, *, refresh: bool = False) -> InspectResponse:
    started = time.monotonic()
    owner, repo = parse_repo_url(repo_url)

    # 缓存里已有 default_branch 时只用 atom 解析 HEAD，不消耗 API 元信息配额。
    cached_branch: str | None = None
    latest = cache.load_latest(owner, repo)
    if latest is not None:
        repo_payload = latest.report.get("repo")
        if isinstance(repo_payload, dict):
            branch = repo_payload.get("default_branch")
            if isinstance(branch, str) and branch:
                cached_branch = branch

    try:
        head: RepoHead = await resolve_head(repo_url, branch=cached_branch)

        if not refresh:
            cached = cache.load(owner, repo, head.commit_sha)
            if cached is not None:
                logger.info("cache hit %s", cache.cache_key(owner, repo, head.commit_sha))
                return _cached_response(cached, hit=True, stale=False)

        bundle = await fetch_repo_bundle(repo_url, head=head)
    except ScanError as error:
        if error.code == "RATE_LIMITED":
            stale = cache.load_latest(owner, repo)
            if stale is not None:
                logger.warning(
                    "rate limited, serving stale cache repo=%s/%s path=%s",
                    owner,
                    repo,
                    stale.path,
                )
                return _cached_response(stale, hit=False, stale=True)
        raise

    scan = scan_tree(bundle.tree, github_truncated=bundle.tree_truncated_by_github)

    snapshot = build_snapshot(bundle, owner, repo)
    snapshot.text_total = scan.scope.files_scanned
    snapshot.raw_windows_scripts = scan.raw_windows_script_count
    snapshot.excluded_windows_scripts = scan.excluded_windows_script_count
    snapshot.unlisted_windows_scripts = scan.unlisted_windows_script_count
    content = await fetch_contents(
        owner, repo, bundle.commit_sha, plan_content_paths(snapshot)
    )
    snapshot.contents = dict(content.contents)
    snapshot.failed_paths = content.failed
    evaluation = evaluate(snapshot)
    findings = evaluation.findings
    checks = evaluation.checks
    scope = scan.scope
    stats = scan.stats
    stats.content_requests = content.requests
    stats.content_bytes = content.bytes
    stats.content_failed = len(content.failed)
    stats.content_files_available = len(content.contents)
    stats.path_scan_files = len(content.contents)
    stats.path_scan_bytes = content.bytes
    stats.content_total_text_files = scope.files_scanned
    stats.secret_scan_files = len(content.contents)
    stats.secret_scan_bytes = sum(len(text.encode("utf-8")) for text in content.contents.values())
    stats.secret_sampling_ratio = (
        round(stats.secret_scan_files / scope.files_scanned, 4) if scope.files_scanned else 0.0
    )
    score = build_score(findings, stats)
    _annotate_check_basis(checks, findings, score, stats.secret_sampling_ratio)

    duration_ms = int(round((time.monotonic() - started) * 1000))
    scanned_at = snapshot.scanned_at.isoformat()

    report = InspectResponse(
        schema_version=SCHEMA_VERSION,
        ok=True,
        provider="github",
        request_count=bundle.request_count,
        source=SourceInfo(
            method="trees_api",
            tree_ref_fallback=bundle.tree_ref_fallback,
            tree_truncated_by_github=bundle.tree_truncated_by_github,
        ),
        mode="metadata",
        repo=_make_repo_info(bundle, owner, repo),
        ref=bundle.ref,
        commit_sha=bundle.commit_sha,
        commit=_make_commit_info(bundle),
        scan=ScanInfo(
            engine_version=ENGINE_VERSION,
            scanned_at=scanned_at,
            duration_ms=duration_ms,
        ),
        scope=scope,
        stats=stats,
        timing=bundle.timing,
        files=scan.files,
        findings=findings,
        checks=checks,
        score=score,
        cache=CacheInfo(hit=False, stale=False, cached_at=None),
        warnings=[],
        error=None,
        engine_version=ENGINE_VERSION,
        scanned_at=scanned_at,
    )

    try:
        cached_at = cache.save(
            owner, repo, bundle.commit_sha, report.model_dump(mode="json"), duration_ms
        )
        report.cache = CacheInfo(hit=False, stale=False, cached_at=cached_at)
    except (OSError, ValueError):
        logger.exception("cache save failed repo=%s/%s sha=%s", owner, repo, bundle.commit_sha)

    logger.info(
        "inspect ok repo=%s ref=%s sha=%s requests=%d content_requests=%d content_bytes=%d "
        "content_failed=%d content_ok=%d files=%d skipped=%d findings=%d scripts=%d "
        "timing=%d/%d/%d truncated=%s fallback=%s github_truncated=%s cache=%s",
        bundle.repo.get("full_name"),
        bundle.ref,
        bundle.commit_sha,
        bundle.request_count,
        stats.content_requests,
        stats.content_bytes,
        stats.content_failed,
        stats.content_files_available,
        scope.files_scanned,
        scope.files_skipped,
        len(findings),
        stats.windows_script_count,
        bundle.timing.meta_ms,
        bundle.timing.commit_ms,
        bundle.timing.tree_ms,
        scope.truncated,
        bundle.tree_ref_fallback,
        bundle.tree_truncated_by_github,
        "new" if report.cache.cached_at else "write-failed",
    )

    logger.info(
        "checks %s",
        " ".join(f"{check.rule_id}={check.status}" for check in checks),
    )
    logger.info("score %s", score_line(score))

    logger.info(
        "identities scanned=%d + skipped=%d == listed=%d ; listed=%d + unlisted=%d == total=%d",
        scope.files_scanned,
        scope.files_skipped,
        stats.listed_blobs,
        stats.listed_blobs,
        stats.unlisted_blobs,
        stats.total_blobs_in_tree,
    )

    return report


def _error_payload(error: ScanError) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "error": error.as_error().model_dump(mode="json"),
    }


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/api/health", response_model=HealthResponse)
async def api_health() -> HealthResponse:
    return HealthResponse(
        ok=True,
        engine_version=ENGINE_VERSION,
        schema_version=SCHEMA_VERSION,
        rules_count=len(RULES),
    )


@app.get("/api/inspect", response_model=InspectResponse)
async def api_inspect_get(
    repo: str = Query(..., description="https://github.com/{owner}/{repo}"),
    refresh: int = Query(0, ge=0, le=1, description="1 跳过缓存并重新扫描"),
) -> InspectResponse:
    return await _inspect(repo, refresh=bool(refresh))


@app.post("/api/inspect", response_model=InspectResponse)
async def api_inspect(
    payload: InspectRequest,
    refresh: int = Query(0, ge=0, le=1, description="1 跳过缓存并重新扫描"),
) -> InspectResponse:
    return await _inspect(payload.repo_url, refresh=bool(refresh))


@app.post("/api/advice", response_model=AdviceResponse)
async def api_advice(payload: AdviceRequest) -> AdviceResponse:
    result = await _inspect(payload.repo, refresh=bool(payload.refresh))
    return await build_advice(result, refresh=bool(payload.refresh))


@app.get("/api/rules", response_model=RulesResponse)
async def api_rules() -> RulesResponse:
    return RulesResponse(rules=[rule.info() for rule in RULES])


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {"result": None, "error": None, "repo_url": "", "advice": None},
    )


@app.post("/", response_class=HTMLResponse)
async def index_submit(request: Request, repo_url: str = Form(...)) -> HTMLResponse:
    try:
        result = await _inspect(repo_url)
    except ScanError as error:
        logger.info("inspect failed code=%s repo_url=%s", error.code, repo_url)
        return templates.TemplateResponse(
            request,
            "index.html",
            {"result": None, "error": error, "repo_url": repo_url},
            status_code=error.http_status,
        )
    context = _result_context(result)
    context["advice"] = await build_advice(result)
    context.update(
        {
            "error": None,
            "repo_url": repo_url,
            "tree_slow": result.timing.tree_slow,
        }
    )
    return templates.TemplateResponse(request, "index.html", context)


@app.exception_handler(ScanError)
async def scan_error_handler(request: Request, error: ScanError) -> JSONResponse | HTMLResponse:
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=error.http_status, content=_error_payload(error))
    return templates.TemplateResponse(
        request,
        "index.html",
        {"result": None, "error": error, "repo_url": ""},
        status_code=error.http_status,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, _: RequestValidationError
) -> JSONResponse | HTMLResponse:
    error = invalid_url("请求参数不合法，请检查仓库地址")
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=error.http_status, content=_error_payload(error))
    return templates.TemplateResponse(
        request,
        "index.html",
        {"result": None, "error": error, "repo_url": ""},
        status_code=error.http_status,
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse | HTMLResponse:
    logger.exception("unhandled error on %s", request.url.path)
    error = internal()
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=error.http_status, content=_error_payload(error))
    return templates.TemplateResponse(
        request,
        "index.html",
        {"result": None, "error": error, "repo_url": ""},
        status_code=error.http_status,
    )
