"""证据脱敏：所有 Finding 的 evidence 都经这里输出，后续密钥规则复用同一入口。"""

from __future__ import annotations

import re

MAX_EVIDENCE_CHARS = 240

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), r"\1_***"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github_pat_***"),
    (re.compile(r"sk-[A-Za-z0-9]{16,}"), "sk-***"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA***"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "xox*-***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._-]{8,}"), r"\1***"),
    (
        re.compile(
            r"(?i)((?:password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*)[^\s,;'\"]+"
        ),
        r"\1***",
    ),
    # 绝对路径只保留最后两级，避免泄露本机目录结构
    (re.compile(r"(?:/Users/|/home/|C:\\Users\\)[^\s]+"), "<local-path>"),
)


def mask_evidence(text: str, limit: int = MAX_EVIDENCE_CHARS) -> str:
    """脱敏 + 压平空白 + 截断；evidence 必须可安全落库与展示。"""
    result = (text or "").strip()
    for pattern, replacement in _RULES:
        result = pattern.sub(replacement, result)
    result = re.sub(r"\s+", " ", result)
    if len(result) > limit:
        result = result[: limit - 1] + "…"
    return result
