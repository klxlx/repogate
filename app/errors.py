"""对外错误码与 HTTP 映射：页面和 JSON 都必须走这里，禁止裸栈。"""

from __future__ import annotations


class ScanError(Exception):
    """可预期的业务错误，携带对外错误码与中文提示。"""

    def __init__(self, code: str, message: str, http_status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def invalid_url(message: str = "仅支持 https://github.com/{owner}/{repo} 形式的公开仓库地址") -> ScanError:
    return ScanError("INVALID_URL", message, 400)


def not_found(message: str = "仓库不存在或不是公开仓库（GitHub 返回 404）") -> ScanError:
    return ScanError("NOT_FOUND", message, 404)


def rate_limited(message: str = "GitHub 接口限流或拒绝访问（403/429），请稍后重试") -> ScanError:
    return ScanError("RATE_LIMITED", message, 429)


def timeout(message: str = "访问 GitHub 超时，请稍后重试") -> ScanError:
    return ScanError("TIMEOUT", message, 504)


def upstream(message: str = "访问 GitHub 失败，请稍后重试") -> ScanError:
    return ScanError("UPSTREAM_ERROR", message, 502)


def internal(message: str = "服务内部错误，请稍后重试") -> ScanError:
    return ScanError("INTERNAL_ERROR", message, 502)
