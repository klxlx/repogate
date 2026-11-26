"""R09 / R10 / R11：入库 .env、私钥、令牌与带账密连接串（第 4 步）。"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from ..masking import mask_secret
from ..snapshot import Snapshot
from .base import (
    Rule,
    RuleOutcome,
    conclusion,
    degraded,
    make_finding,
    passed,
)

ENV_TEMPLATE_SUFFIXES = frozenset({"example", "sample", "template", "dist"})
DOC_EXTENSIONS = frozenset({".md", ".rst", ".txt"})
LOW_DIR_SEGMENTS = frozenset(
    {"tests", "test", "fixtures", "examples", "docs", "samples", "testdata", "test-data"}
)
KEY_EXTENSIONS = frozenset({".pem", ".key", ".p12", ".pfx", ".jks"})
READABLE_KEY_EXTENSIONS = frozenset({".pem", ".key"})
ROOT_CONFIG_EXTENSIONS = frozenset(
    {".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".properties", ".env"}
)

PLACEHOLDER_WORDS = (
    "xxx",
    "your",
    "example",
    "sample",
    "demo",
    "test",
    "dummy",
    "fake",
    "placeholder",
    "changeme",
    "change-me",
    "changethis",
    "change_me",
    "replaceme",
    "replace-me",
    "redacted",
    "todo",
)
PLACEHOLDER_MARKERS = ("<", "${", "{{", "%s", "***")

PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP |ENCRYPTED )?PRIVATE KEY-----"
)

TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("阿里云 AccessKey", re.compile(r"LTAI[A-Za-z0-9]{12,}")),
    ("阿里云 AccessKeyId", re.compile(r"AKID[A-Za-z0-9]{13,}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36}")),
    ("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("Bearer token", re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{20,}=*")),
    (
        "数据库连接串",
        re.compile(
            r"(?i)(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)"
            r"://[^\s:@/]+:[^\s@/]{4,}@"
        ),
    ),
)

_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
# 冻结关键词表（README 同步）：secret|token|password|passwd|pwd|api[_-]?key|
# access[_-]?key|credential|auth|private。按词边界匹配，避免 AUTHOR 命中 auth。
_SECRET_KEY_RE = re.compile(
    r"(?<![a-z])(?:secret|token|password|passwd|pwd|api[_-]?key|access[_-]?key|"
    r"credential(?:s)?|auth|private)(?![a-z])"
)


def _is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(key.lower()))
_TEST_FILE_RE = re.compile(
    r"(?i)(^test_.*\.py$|.*_test\.(py|go)$|.*\.(test|spec)\.(js|jsx|ts|tsx)$)"
)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _extension(path: str) -> str:
    return posixpath.splitext(path)[1].lower()


def is_env_template(path: str) -> bool:
    name = _basename(path).lower()
    if not name.startswith(".env."):
        return False
    return name[len(".env.") :].split(".")[0] in ENV_TEMPLATE_SUFFIXES


def is_env_file(path: str) -> bool:
    name = _basename(path).lower()
    if name == ".env":
        return True
    return name.startswith(".env.") and not is_env_template(path)


def is_private_key_path(path: str) -> bool:
    name = _basename(path).lower()
    if name.endswith(".pub"):
        return False
    return _extension(path) in KEY_EXTENSIONS or name.startswith("id_rsa")


def is_secret_candidate(path: str) -> bool:
    """进内容预取前排的密钥候选：.env、.gitignore、可读私钥、凭据/密钥命名。"""
    name = _basename(path).lower()
    if is_env_file(path):
        return True
    if path == ".gitignore" or name == ".gitignore":
        return True
    if _extension(path) in READABLE_KEY_EXTENSIONS or name.startswith("id_rsa"):
        return True
    return "credential" in name or "secret" in name


def _in_low_dir(path: str) -> bool:
    if _TEST_FILE_RE.match(_basename(path)):
        return True
    return any(segment.lower() in LOW_DIR_SEGMENTS for segment in path.split("/")[:-1])


def in_low_dir(path: str) -> bool:
    """公开给评分层：测试/样例目录的 low 命中不扣分。"""
    return _in_low_dir(path)


def _is_doc(path: str) -> bool:
    return _extension(path) in DOC_EXTENSIONS


def _is_root_config(path: str) -> bool:
    if "/" in path:
        return False
    name = _basename(path).lower()
    if name in {"config", "settings", "secrets", ".env", "config.py", "settings.py"}:
        return True
    return _extension(path) in ROOT_CONFIG_EXTENSIONS


def _is_high_risk_path(path: str) -> bool:
    name = _basename(path).lower()
    if is_env_file(path) or is_private_key_path(path):
        return True
    if "credential" in name or "secret" in name:
        return True
    return _is_root_config(path)


def sampling_ratio(snapshot: Snapshot) -> float:
    total = snapshot.text_total or len(snapshot.contents) or 1
    return len(snapshot.contents) / total


def cap_eligible(path: str, ratio: float) -> bool:
    """封顶资格：抽样率 ≥50%，或命中文件属于高风险路径。"""
    return ratio >= 0.5 or _is_high_risk_path(path)


def _pct(ratio: float) -> str:
    return f"{ratio * 100:.2f}%"


def _is_placeholder_value(value: str) -> bool:
    text = value.strip().strip('"').strip("'").strip()
    if not text:
        return True
    lowered = text.lower()
    if any(word in lowered for word in PLACEHOLDER_WORDS):
        return True
    if any(marker in text for marker in PLACEHOLDER_MARKERS):
        return True
    return len(set(text)) == 1


def _looks_real(value: str) -> bool:
    return not _is_placeholder_value(value)


def _is_placeholder_token(value: str) -> bool:
    if _is_placeholder_value(value):
        return True
    tail = re.sub(
        r"^(sk-ant-|sk-|AKIA|LTAI|AKID|gh[pousr]_|xox[baprs]-|AIza|bearer\s+|Bearer\s+)",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return bool(tail) and len(set(tail)) == 1


def _gitignore_covers_env(text: str) -> bool:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        token = line[2:] if line.startswith("./") else line
        token = token.strip("/")
        if token == ".env" or token.startswith(".env") or token.endswith("/.env"):
            return True
    return False


def _env_summary(text: str) -> tuple[list[str], int]:
    """解析前 4KB：返回（含真实值的密钥类 KEY 列表，KEY=VALUE 总数）。"""
    keys: list[str] = []
    total = 0
    for raw in text[:4096].splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY_RE.fullmatch(key):
            continue
        total += 1
        if _is_secret_key(key) and _looks_real(value):
            keys.append(key)
    return keys, total


@dataclass(frozen=True)
class _Hit:
    line: int
    value: str
    label: str
    context: str = ""


_RULE_DEF_CHARS = frozenset("[]{}()|\\*+?")
_RULE_DEF_WORD_RE = re.compile(
    r"(?i)\b(?:regex|regexp|pattern|re\.compile|gitleaks|allowlist|rule)\b"
)


def _is_rule_path(path: str) -> bool:
    """规则/模式定义目录，或 gitleaks.toml 这类规则文件。"""
    segments = [segment.lower() for segment in path.split("/")[:-1]]
    if any(segment in {"rules", "patterns"} for segment in segments):
        return True
    name = _basename(path).lower()
    return name == "gitleaks.toml" or "gitleaks" in name or "rule" in name


def _has_rule_chars(line: str) -> bool:
    return any(char in line for char in _RULE_DEF_CHARS)


def _is_rule_definition(path: str, line: str) -> bool:
    """规则文件，或同时含正则元字符与 regex/pattern 等关键字的定义行。

    普通配置字典（如 {"Authorization": "Bearer ..."}）不降级，避免把真实配置误判成规则样例。
    """
    if _is_rule_path(path):
        return True
    return _has_rule_chars(line) and bool(_RULE_DEF_WORD_RE.search(line))


def _severity_for(path: str, ratio: float) -> tuple[str, str, bool]:
    """返回 (severity, confidence, cap_eligible)。抽样 <20% 时不封顶。"""
    if _is_doc(path):
        return "info", "medium", False
    if _in_low_dir(path):
        return "low", "medium", False
    if cap_eligible(path, ratio):
        if ratio < 0.2:
            return "high", "low", False
        return "critical", "high", True
    return "high", "low", False


# ---------- R09 env-committed ----------


def detect_env_committed(snapshot: Snapshot) -> RuleOutcome:
    envs = sorted(blob.path for blob in snapshot.blobs if is_env_file(blob.path))
    if not envs:
        has_gitignore = any(blob.path == ".gitignore" for blob in snapshot.blobs)
        gitignore = snapshot.contents.get(".gitignore")
        if gitignore is None:
            if not has_gitignore:
                finding = make_finding(
                    R09,
                    title="未发现 .env，但仓库没有 .gitignore",
                    evidence="未发现 .env 入库；仓库根目录也没有 .gitignore",
                    impact="后续本地 .env 很容易被误提交，建议提前忽略",
                    fix="在根目录补充 .gitignore，加入 .env 与 .env.*",
                    severity="low",
                    confidence="medium",
                )
                return conclusion(R09, "未发现 .env 入库；未找到 .gitignore", [finding])
            return degraded(
                R09,
                "未发现 .env 入库，但 .gitignore 读取失败，无法判断是否忽略 .env",
                file=".gitignore",
            )
        if _gitignore_covers_env(gitignore):
            return passed(R09, "未发现 .env 入库，.gitignore 已包含 .env", file=".gitignore")
        finding = make_finding(
            R09,
            title=".gitignore 未包含 .env",
            evidence="未发现 .env 入库，但 .gitignore 中没有 .env / .env.* 规则",
            impact="本地 .env 存在被误提交的风险",
            fix="在 .gitignore 增加 .env 与 .env.*",
            file=".gitignore",
            severity="low",
            confidence="medium",
        )
        return conclusion(R09, "未发现 .env 入库，但 .gitignore 未包含 .env", [finding])

    findings = []
    unreadable: list[str] = []
    for path in envs:
        text = snapshot.contents.get(path)
        if text is None:
            unreadable.append(path)
            continue
        keys, total = _env_summary(text)
        low_dir = _in_low_dir(path)
        if keys and not low_dir:
            preview = "、".join(f"{key}=***" for key in keys[:3])
            finding = make_finding(
                R09,
                title=".env 随代码入库且含非占位值",
                evidence=f"{path} 前 4KB 解析 {total} 项，{len(keys)} 项为非占位值（{preview}）",
                impact="明文环境变量随代码公开，API Key / 数据库口令可直接被使用",
                fix="立即轮换已泄露的凭据，把 .env 移出版本库并加入 .gitignore",
                file=path,
                severity="critical",
                confidence="high",
                cap_eligible=True,
                hit_count=len(keys),
            )
        elif keys:
            finding = make_finding(
                R09,
                title="测试/示例目录中的 .env",
                evidence=f"{path} 前 4KB 解析 {total} 项，{len(keys)} 项为非占位值（测试/示例夹具）",
                impact="测试/示例目录中的环境变量样例，风险较低",
                fix="确认是否为测试夹具；真实凭据不应进入版本库",
                file=path,
                severity="low",
                confidence="medium",
                cap_eligible=False,
                hit_count=len(keys),
            )
        else:
            finding = make_finding(
                R09,
                title=".env 随代码入库但密钥类值均为空或占位",
                evidence=f"{path} 前 4KB 解析 {total} 项，密钥类 KEY 均为空值或占位值",
                impact="文件本身入库，后续填入真实值时容易再次被提交",
                fix="把 .env 移出版本库，仅保留 .env.example",
                file=path,
                severity="medium",
                confidence="medium",
                cap_eligible=False,
            )
        findings.append(finding)

    if not findings:
        return degraded(
            R09,
            f"{'、'.join(unreadable)} 内容不可读，无法判断是否含非占位值",
            file=unreadable[0],
        )

    basis = f"发现 {len(envs)} 个 .env 文件，命中 {len(findings)} 个"
    if unreadable:
        basis += f"；{'、'.join(unreadable)} 内容不可读，结论不完整"
    penalty = 0 if any(item.severity == "critical" for item in findings) else 10
    return conclusion(R09, basis, findings, penalty=penalty)


# ---------- R10 private-key ----------


def detect_private_key(snapshot: Snapshot) -> RuleOutcome:
    ratio = sampling_ratio(snapshot)
    findings = []
    header_hits: dict[str, list[int]] = {}
    for path in sorted(snapshot.contents):
        for lineno, line in enumerate(snapshot.contents[path].splitlines(), start=1):
            if PRIVATE_KEY_RE.search(line):
                header_hits.setdefault(path, []).append(lineno)

    for path, lines in header_hits.items():
        if _in_low_dir(path):
            severity, confidence, capped = "low", "medium", False
        elif cap_eligible(path, ratio):
            if ratio < 0.2:
                severity, confidence, capped = "high", "low", False
            else:
                severity, confidence, capped = "critical", "high", True
        else:
            severity, confidence, capped = "high", "low", False
        impact = "私钥随代码公开，可被直接用于冒充服务或解密流量"
        if severity == "high":
            impact = f"抽样 {len(snapshot.contents)}/{snapshot.text_total}（{_pct(ratio)}）范围内发现，未构成全仓结论"
        finding = make_finding(
            R10,
            title="私钥材料出现在仓库内容中",
            evidence=f"{path}:{lines[0]} 命中私钥头；同文件共 {len(lines)} 处",
            impact=impact,
            fix="立即吊销并重新生成密钥，从版本库移除私钥材料",
            file=path,
            line=lines[0],
            severity=severity,
            confidence=confidence,
            cap_eligible=capped,
            hit_count=len(lines),
        )
        findings.append(finding)

    unreadable = []
    for blob in snapshot.blobs:
        if not is_private_key_path(blob.path):
            continue
        if blob.path in snapshot.contents or blob.path in header_hits:
            continue
        if _in_low_dir(blob.path) or _is_doc(blob.path):
            severity, confidence = "low", "medium"
        else:
            severity, confidence = "high", "low"
        unreadable.append(blob.path)
        finding = make_finding(
            R10,
            title="私钥类文件存在但内容不可读",
            evidence=f"{blob.path} 扩展名命中私钥/证书类型，本次未读取到内容，无法确认是否为私钥",
            impact="内容不可读，按高风险处理；抽样范围内发现，未构成全仓结论",
            fix="确认该文件是否为私钥材料；如是，移出版本库并轮换",
            file=blob.path,
            severity=severity,
            confidence=confidence,
            cap_eligible=False,
        )
        findings.append(finding)

    if not findings:
        return passed(R10, f"抽样 {len(snapshot.contents)}/{snapshot.text_total}（{_pct(ratio)}），未命中私钥材料")

    basis = f"抽样 {len(snapshot.contents)}/{snapshot.text_total}（{_pct(ratio)}），命中 {len(findings)} 个文件"
    penalty = 15 if any(item.severity in ("critical", "high") for item in findings) else 0
    return conclusion(R10, basis, findings, penalty=penalty)


# ---------- R11 secret-token ----------


def _scan_tokens(path: str, text: str) -> list[_Hit]:
    hits: list[_Hit] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        spans: list[tuple[int, int]] = []
        for label, pattern in TOKEN_PATTERNS:
            for match in pattern.finditer(line):
                start, end = match.span()
                if any(start < used_end and end > used_start for used_start, used_end in spans):
                    continue
                spans.append((start, end))
                hits.append(_Hit(lineno, match.group(0), label, line))
    return hits


def detect_secret_token(snapshot: Snapshot) -> RuleOutcome:
    ratio = sampling_ratio(snapshot)
    per_file: list[tuple[str, list[_Hit]]] = []
    for path in sorted(snapshot.contents):
        hits = _scan_tokens(path, snapshot.contents[path])
        if hits:
            per_file.append((path, hits))

    if not per_file:
        return passed(
            R11,
            f"抽样 {len(snapshot.contents)}/{snapshot.text_total}（{_pct(ratio)}），未命中令牌或带账密连接串",
        )

    findings = []
    critical_count = 0
    for path, hits in per_file:
        real_hits = [hit for hit in hits if not _is_placeholder_token(hit.value)]
        rule_definition = any(_is_rule_definition(path, hit.context) for hit in hits)
        if rule_definition:
            severity, confidence, capped = "info", "medium", False
        elif _is_doc(path):
            severity, confidence, capped = "info", "medium", False
        elif not real_hits:
            severity, confidence, capped = "low", "medium", False
        elif _in_low_dir(path):
            severity, confidence, capped = "low", "medium", False
        else:
            severity, confidence, capped = _severity_for(path, ratio)
        if severity == "critical":
            critical_count += 1
        preview = "；".join(
            f"{path}:{hit.line} {hit.label}（{mask_secret(hit.value)}）" for hit in hits[:3]
        )
        impact = "令牌可直接用于访问对应服务，属于可被立即利用的泄露"
        if severity == "high":
            impact = f"抽样 {len(snapshot.contents)}/{snapshot.text_total}（{_pct(ratio)}）范围内发现，未构成全仓结论"
        elif severity == "low":
            impact = "测试/示例目录中的样例令牌，风险较低"
        elif severity == "info":
            impact = "规则定义或文档中的令牌形态示例，非真实凭据"
        finding = make_finding(
            R11,
            title="仓库内容包含疑似访问令牌",
            evidence=f"{preview}；同文件共 {len(hits)} 处",
            impact=impact,
            fix="确认是否为真实令牌；如是，立即吊销并改为环境变量注入",
            file=path,
            line=hits[0].line,
            severity=severity,
            confidence=confidence,
            cap_eligible=capped,
            hit_count=len(hits),
        )
        findings.append(finding)

    basis = (
        f"抽样 {len(snapshot.contents)}/{snapshot.text_total}（{_pct(ratio)}），"
        f"命中 {len(findings)} 个文件（critical {critical_count}）"
    )
    penalty = 15 if any(item.severity == "high" for item in findings) else 0
    return conclusion(R11, basis, findings, penalty=penalty)


R09 = Rule(
    id="R09",
    slug="env-committed",
    dimension="secrets",
    severity="critical",
    points=-10,
    needs_content=True,
    detect=detect_env_committed,
)

R10 = Rule(
    id="R10",
    slug="private-key",
    dimension="secrets",
    severity="critical",
    points=-15,
    needs_content=True,
    detect=detect_private_key,
)

R11 = Rule(
    id="R11",
    slug="secret-token",
    dimension="secrets",
    severity="critical",
    points=-15,
    needs_content=True,
    detect=detect_secret_token,
)
