"""R06 / R07 / R08：Windows 脚本依赖、跨平台入口、硬编码路径。"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from dataclasses import dataclass

from ..snapshot import Snapshot
from .base import Rule, RuleOutcome, degraded, failed, make_finding, na, passed

WIN_EXTENSIONS = frozenset({".bat", ".cmd", ".ps1", ".vbs"})
SHELL_EXTENSIONS = frozenset({".sh", ".bash"})
DOC_EXTENSIONS = frozenset({".md", ".rst", ".txt"})

LOW_USERNAMES = frozenset(
    {
        "username", "user", "yourname", "name", "xxx", "example",
        "runner", "runneradmin", "vagrant", "ubuntu",
        "someone", "alice", "bob", "jane", "john", "doe", "dev", "developer", "test",
    }
)
INFO_DIR_PREFIXES = ("tests/", "test/", "fixtures/", "examples/", "docs/", "samples/")
PLACEHOLDER_SEGMENTS = frozenset(
    {"foo", "bar", "baz", "example", "sample", "demo", "test", "your-path", "path-to"}
)

URL_RE = re.compile(r'(?:https?|ftp|file)://[^\s"\']+')
# 大小写盘符 + 前边界 + 转义前瞻（只排除 n/t/r 三种转义前缀，避免误伤 C:\foo）
DRIVE_RE = re.compile(
    r'(?<![A-Za-z0-9_])[A-Za-z]:\\(?!(?:n|t|r))'
    r'(?:[^\\\s"\'<>|*?]+\\)*[^\\\s"\'<>|*?]+'
)
UNC_RE = re.compile(r'\\\\[A-Za-z0-9._-]+\\[^\s"\']+')
HOME_RE = re.compile(r'/(?:Users|home)/([A-Za-z0-9._-]+)(?:/[^\s"\']*)?')
USER_IN_PATH_RE = re.compile(r'(?i)[\\/](?:Users|home)[\\/]([A-Za-z0-9._-]+)')
MOUNT_RE = re.compile(r'/mnt/[a-z]/[^\s"\']*')
PLACEHOLDER_RE = re.compile(r"%USERPROFILE%|\$HOME|\$\{HOME\}|<[^<>\s]+>")

PATH_PATTERNS = (("盘符", DRIVE_RE), ("UNC", UNC_RE), ("用户目录", HOME_RE), ("容器挂载", MOUNT_RE))
SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}


def _ext(path: str) -> str:
    return posixpath.splitext(path)[1].lower()


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


# ---------- R06 windows-scripts ----------


def _windows_script_paths(snapshot: Snapshot) -> dict[str, list[str]]:
    buckets = {ext: [] for ext in WIN_EXTENSIONS}
    for blob in snapshot.blobs:
        ext = _ext(blob.path)
        if ext in buckets:
            buckets[ext].append(blob.path)
    return buckets


def _shell_script_count(snapshot: Snapshot) -> int:
    return sum(1 for blob in snapshot.blobs if _ext(blob.path) in SHELL_EXTENSIONS)


def detect_windows_scripts(snapshot: Snapshot) -> RuleOutcome:
    buckets = _windows_script_paths(snapshot)
    paths = sorted(path for group in buckets.values() for path in group)
    count = len(paths)
    shells = _shell_script_count(snapshot)
    total = count + shells
    share = count / total if total else 1.0

    distribution = " / ".join(
        f"{ext} {len(group)}"
        for ext, group in sorted(buckets.items(), key=lambda item: (-len(item[1]), item[0]))
        if group
    ) or "无"
    basis = f"共 {count} 个 Windows 脚本（{distribution}），占脚本总量 {count}/{total}"

    raw = snapshot.raw_windows_scripts
    if raw > count:
        basis += f"；全树 {raw}，其中 {raw - count} 个位于 build/ 等排除目录"

    if count == 0:
        return passed(R06, basis)

    if count == 1:
        severity, penalty = "info", 0
    elif count <= 4:
        severity, penalty = "medium", 3
    elif share < 0.5:
        severity, penalty = "medium", 3
    else:
        severity, penalty = "high", 6

    preview = "、".join(paths[:10])
    finding = make_finding(
        R06,
        title="存在 Windows 专用脚本",
        evidence=f"Windows 脚本 {count} 个（前 10）：{preview}",
        impact=(
            "脚本只在 Windows 上可执行，Linux/容器环境需要替代方案"
            if severity in ("high", "medium")
            else "仅个别 Windows 脚本，影响有限，供人工确认"
        ),
        fix="补充 .sh / .bash 或跨平台脚本，并说明 Windows 专用脚本的用途",
        file=paths[0],
        severity=severity,
        hit_count=count,
    )
    return failed(R06, basis, finding, penalty=penalty)


# ---------- R07 no-crossplatform ----------


def _cross_platform_entry(snapshot: Snapshot) -> tuple[str, str] | None:
    """任意位置存在即可（R07 不套用根目录/弱目录的证据层级）。"""
    checks = (
        ("Makefile", lambda path: _basename(path) == "Makefile"),
        ("Dockerfile", lambda path: _basename(path) == "Dockerfile"),
        (
            "docker-compose",
            lambda path: fnmatch.fnmatchcase(_basename(path), "docker-compose*.yml")
            or fnmatch.fnmatchcase(_basename(path), "docker-compose*.yaml"),
        ),
        (".sh / .bash", lambda path: _ext(path) in SHELL_EXTENSIONS),
    )
    for label, predicate in checks:
        matches = sorted(blob.path for blob in snapshot.blobs if predicate(blob.path))
        if matches:
            return label, matches[0]
    return None


def detect_no_crossplatform(snapshot: Snapshot) -> RuleOutcome:
    count = sum(len(group) for group in _windows_script_paths(snapshot).values())
    if count == 0:
        return na(R07, "无 Windows 脚本，规则不适用")

    entry = _cross_platform_entry(snapshot)
    if entry is not None:
        label, path = entry
        return passed(R07, f"存在跨平台入口：{path}（{label}）", file=path)

    finding = make_finding(
        R07,
        title="只有 Windows 脚本，没有跨平台入口",
        evidence=(
            f"共 {count} 个 Windows 脚本（.bat/.cmd/.ps1/.vbs），"
            "但未找到 .sh / .bash / Makefile / Dockerfile / docker-compose*.yml"
        ),
        impact="Linux/容器环境无法直接构建或启动，接入需要额外改造",
        fix="补充 .sh 启动脚本或 Makefile / Dockerfile",
    )
    return failed(
        R07,
        f"共 {count} 个 Windows 脚本，无任何跨平台入口",
        finding,
        penalty=4,
    )


# ---------- R08 hardcoded-path ----------


@dataclass(frozen=True)
class _Hit:
    line: int
    text: str
    severity: str


def _has_placeholder_segment(matched: str) -> bool:
    body = re.sub(r"^[A-Za-z]:", "", matched)
    segments = re.split(r"[\\/]+", body)
    return any(segment.lower() in PLACEHOLDER_SEGMENTS for segment in segments if segment)


def _classify(path: str, line: str, matched: str) -> str:
    if PLACEHOLDER_RE.search(line):
        return "low"
    home = USER_IN_PATH_RE.search(matched)
    if home and home.group(1).lower() in LOW_USERNAMES:
        return "low"
    if path.startswith(".github/workflows/") and "/home/runner/" in matched:
        return "low"
    if _has_placeholder_segment(matched):
        return "low"
    if path.startswith(INFO_DIR_PREFIXES):
        return "info"
    if _ext(path) in DOC_EXTENSIONS:
        return "info"
    return "high"


def _scan_file(path: str, text: str) -> list[_Hit]:
    hits: list[_Hit] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = URL_RE.sub("", raw).replace("\\\\", "\\")
        for _, pattern in PATH_PATTERNS:
            for match in pattern.finditer(line):
                if len(match.group(0)) <= 2:
                    continue
                hits.append(
                    _Hit(lineno, match.group(0), _classify(path, line, match.group(0)))
                )
    return hits


def detect_hardcoded_path(snapshot: Snapshot) -> RuleOutcome:
    scanned = len(snapshot.contents)
    total = snapshot.text_total or scanned or 1
    ratio = scanned / total if total else 1.0
    pct = f"{ratio * 100:.2f}%"
    prefix = f"抽样 {scanned}/{total}（{pct}）"

    if scanned == 0:
        return degraded(R08, "未读取到任何文本内容，无法判定硬编码路径")

    per_file: list[tuple[str, list[_Hit]]] = []
    for path in sorted(snapshot.contents):
        hits = _scan_file(path, snapshot.contents[path])
        if hits:
            per_file.append((path, hits))

    if not per_file:
        if ratio < 0.2:
            return degraded(
                R08, f"{prefix} 抽样率过低（{pct}），未发现不代表不存在"
            )
        return passed(R08, f"{prefix}，未命中硬编码路径")

    findings = []
    high_files = 0
    low_files = 0
    info_files = 0
    for path, hits in per_file:
        severity = max((hit.severity for hit in hits), key=lambda item: SEVERITY_RANK[item])
        confidence = "medium" if severity in ("low", "info") or ratio < 0.2 else "high"
        if severity == "high":
            high_files += 1
        elif severity == "low":
            low_files += 1
        else:
            info_files += 1

        preview = "；".join(f"{path}:{hit.line} {hit.text}" for hit in hits[:3])
        impact = (
            "路径绑定本机环境，迁移到容器或 CI 会直接失败"
            if severity == "high"
            else "路径是示例/占位符或文档示例，风险较低"
        )
        if ratio < 0.2:
            impact += "；仅基于抽样文件，非全仓结论"
        finding = make_finding(
            R08,
            title=f"硬编码路径（{severity}）",
            evidence=(
                f"{prefix}，{path} 命中 {len(hits)} 处；前 3 处：{preview}"
            ),
            impact=impact,
            fix="改为相对路径、环境变量或可配置参数",
            file=path,
            line=hits[0].line,
            severity=severity,
            confidence=confidence,
            keep_paths=True,
            hit_count=len(hits),
        )
        findings.append(finding)

    # TODO(第 5 步): 抽样率 <20% 时 R08 扣分减半
    penalty = min(2 * high_files, 10)
    basis = (
        f"{prefix}，命中 {len(findings)} 个文件"
        f"（high {high_files} / low {low_files} / info {info_files}）"
    )
    return RuleOutcome(
        status="fail",
        basis=basis,
        finding=findings[0],
        file=findings[0].file,
        line=findings[0].line,
        penalty=penalty,
        extra_findings=findings[1:],
    )


R06 = Rule(
    id="R06",
    slug="windows-scripts",
    dimension="portability",
    severity="high",
    points=-6,
    needs_content=False,
    detect=detect_windows_scripts,
)

R07 = Rule(
    id="R07",
    slug="no-crossplatform",
    dimension="portability",
    severity="medium",
    points=-4,
    needs_content=False,
    detect=detect_no_crossplatform,
)

R08 = Rule(
    id="R08",
    slug="hardcoded-path",
    dimension="portability",
    severity="high",
    points=-10,
    needs_content=True,
    detect=detect_hardcoded_path,
)
