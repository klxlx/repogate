"""RepoGate Web 入口：健康检查、JSON 检查接口、SSR 首页。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .errors import ScanError, internal
from .github import fetch_repo_bundle
from .models import InspectRequest, InspectResponse, RepoInfo, ScopeInfo
from .zipscan import ScanResult, scan_zip

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger("repogate")


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
    yield


app = FastAPI(
    title="RepoGate",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
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


async def _inspect(repo_url: str) -> InspectResponse:
    bundle = await fetch_repo_bundle(repo_url)

    if bundle.zip_bytes is None:
        scan = ScanResult(
            scope=ScopeInfo(truncated=True, truncate_reason=bundle.truncate_reason),
            files=[],
        )
    else:
        scan = scan_zip(bundle.zip_bytes)

    scope = scan.scope
    logger.info(
        "inspect ok repo=%s ref=%s sha=%s requests=%d files=%d skipped=%d truncated=%s",
        bundle.repo.get("full_name"),
        bundle.ref,
        bundle.commit_sha,
        bundle.requests_used,
        scope.files_scanned,
        scope.files_skipped,
        scope.truncated,
    )

    return InspectResponse(
        ok=True,
        repo=RepoInfo(**bundle.repo),
        ref=bundle.ref,
        commit_sha=bundle.commit_sha,
        scope=scope,
        files=scan.files,
    )


def _error_payload(error: ScanError) -> dict:
    return {"ok": False, "error": {"code": error.code, "message": error.message}}


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.post("/api/inspect", response_model=InspectResponse)
async def api_inspect(payload: InspectRequest) -> InspectResponse:
    return await _inspect(payload.repo_url)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {"result": None, "error": None, "repo_url": ""},
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
    return templates.TemplateResponse(
        request,
        "index.html",
        {"result": result, "error": None, "repo_url": repo_url},
    )


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
async def validation_error_handler(request: Request, _: RequestValidationError) -> JSONResponse | HTMLResponse:
    error = ScanError("INVALID_REQUEST", "请求参数不合法，请检查仓库地址", 400)
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
