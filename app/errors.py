"""对外错误码与 HTTP 映射：页面和 JSON 都必须走这里，禁止裸栈。"""

from __future__ import annotations

from .models import ErrorInfo


class ScanError(Exception):
    """可预期的业务错误，携带对外错误码、中文提示与恢复建议。"""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int,
        *,
        hint: str = "",
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.hint = hint
        self.retry_after = retry_after

    def as_error(self) -> ErrorInfo:
        return ErrorInfo(
            code=self.code,
            message=self.message,
            hint=self.hint,
            retry_after=self.retry_after,
        )


def invalid_url(message: str = "仅支持 https://github.com/{owner}/{repo} 形式的公开仓库地址") -> ScanError:
    return ScanError(
        "INVALID_URL",
        message,
        400,
        hint="示例：https://github.com/psf/requests",
    )


def not_found(message: str = "仓库不存在或不是公开仓库（GitHub 返回 404）") -> ScanError:
    return ScanError(
        "REPO_NOT_FOUND",
        message,
        404,
        hint="确认仓库地址正确、仓库为公开仓库，且当前网络可访问 GitHub",
    )


def rate_limited(reset_hhmm: str | None = None, retry_after: int | None = None) -> ScanError:
    if reset_hhmm:
        message = (
            f"GitHub 配额已耗尽，预计 {reset_hhmm} 恢复；"
            "本地可设置 GITHUB_TOKEN 提升到 5000 次/时"
        )
    else:
        message = "GitHub 接口限流或拒绝访问（403/429），请稍后重试"
    return ScanError(
        "RATE_LIMITED",
        message,
        429,
        hint="设置 GITHUB_TOKEN 可提升配额；有缓存时会返回带 stale 标记的旧报告",
        retry_after=retry_after,
    )


def timeout(message: str = "访问 GitHub 超时，请稍后重试") -> ScanError:
    return ScanError(
        "TIMEOUT",
        message,
        504,
        hint="稍后重试；超大仓库的文件树可能接近 90 秒硬上限",
    )


def upstream(message: str = "访问 GitHub 失败，请稍后重试") -> ScanError:
    return ScanError(
        "FETCH_FAILED",
        message,
        502,
        hint="检查网络与 GitHub 状态后重试；重复失败请查看服务日志",
    )


def fetch_failed(message: str = "获取仓库文件树失败，请稍后重试") -> ScanError:
    return ScanError(
        "FETCH_FAILED",
        message,
        502,
        hint="检查网络与 GitHub 状态后重试；重复失败请查看服务日志",
    )


def internal(message: str = "服务内部错误，请稍后重试") -> ScanError:
    return ScanError(
        "INTERNAL",
        message,
        500,
        hint="这是服务端未预期错误，请查看日志；不要把它当作仓库结论",
    )
