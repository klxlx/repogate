"""R03 / R04 / R05：依赖清单、版本浮动、构建/启动入口（全部按证据层级取）。"""

from __future__ import annotations

import fnmatch
import json
import re
import tomllib

from ..snapshot import Snapshot
from .base import (
    Rule,
    RuleOutcome,
    degraded,
    degraded_finding,
    failed,
    make_finding,
    na,
    passed,
)
from .evidence import LEVEL_CHILD, LEVEL_LABEL, LEVEL_ROOT, LEVEL_WEAK, level_of, select

MANIFEST_EXACT = frozenset(
    {
        "package.json", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile",
        "go.mod", "pom.xml", "build.gradle", "build.gradle.kts", "Cargo.toml",
        "composer.json", "Gemfile", "vcpkg.json", "conanfile.txt", "conanfile.py",
        "CMakeLists.txt",
    }
)
MANIFEST_GLOBS = ("requirements*.txt", "*.csproj", "*.sln", "*.slnx")

LOCKFILES = frozenset(
    {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock"}
)
PARSEABLE_MANIFESTS = frozenset({"package.json", "requirements.txt", "pyproject.toml"})

LAUNCH_FILES = frozenset({"Makefile", "Dockerfile"})
COMPOSE_GLOBS = ("docker-compose*.yml", "docker-compose*.yaml")
WINDOWS_BUILD_FILES = frozenset({"build.ps1", "build.bat"})
# 第 3 步 R06/R07 复用：Windows 专用入口在 Linux/容器环境需替代方案
WINDOWS_ONLY_IMPACT = "存在构建入口，但为 Windows 专用，Linux/容器环境需替代方案"
# v1.8 收口：只认 start/dev/serve/build/compile，不认 watch / test / lint
START_KEYS = ("start", "dev", "serve")
BUILD_KEYS = ("build", "compile")
SCRIPT_KEYS = START_KEYS + BUILD_KEYS
START_ENTRY_NOTE = "启动入口"
BUILD_ENTRY_NOTE = "构建入口，无启动命令"
START_ENTRY_IMPACT = "存在启动入口，可在 Linux/容器环境直接运行"
BUILD_ENTRY_IMPACT = "仅有构建入口，无启动命令，Linux/容器环境需补充启动方式"

_OPERATOR_RE = re.compile(r"(==|>=|<=|~=|!=|>|<)")
_FLOATING_PREFIXES = ("^", "~", "*", ">", "latest")

_ENTRY_ORDER = (
    "compose", "dockerfile", "makefile", "sln", "cmake", "build_win", "build_sh", "csproj",
)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def is_manifest(path: str) -> bool:
    name = _basename(path)
    return name in MANIFEST_EXACT or any(
        fnmatch.fnmatchcase(name, pattern) for pattern in MANIFEST_GLOBS
    )


def is_parseable_manifest(path: str) -> bool:
    return _basename(path) in PARSEABLE_MANIFESTS


def find_lockfile(snapshot: Snapshot, level: int) -> str | None:
    """锁文件只在清单所在层级或更上层找，避免被嵌套子包的 lock 误导。"""
    candidates: list[tuple[int, str]] = []
    for blob in snapshot.blobs:
        if _basename(blob.path) not in LOCKFILES:
            continue
        found = level_of(blob.path)
        if found is not None and found <= level:
            candidates.append((found, blob.path))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][1]


def _requirement_specs(text: str) -> list[str]:
    specs: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "[", "git+", "http:", "https:", "file:", ".")):
            continue
        parts = _OPERATOR_RE.split(line, maxsplit=1)
        specs.append("" if len(parts) == 1 else parts[1] + parts[2].strip())
    return specs


def _package_specs(text: str) -> list[str]:
    data = json.loads(text)
    specs: list[str] = []
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        deps = data.get(key)
        if isinstance(deps, dict):
            specs.extend(str(value) for value in deps.values())
    return specs


def _pyproject_specs(text: str) -> list[str]:
    data = tomllib.loads(text)
    specs: list[str] = []
    project = data.get("project") or {}
    for item in project.get("dependencies") or []:
        if isinstance(item, str):
            parts = _OPERATOR_RE.split(item, maxsplit=1)
            specs.append("" if len(parts) == 1 else parts[1] + parts[2].strip())
    poetry = (data.get("tool") or {}).get("poetry") or {}
    for name, value in (poetry.get("dependencies") or {}).items():
        if str(name).lower() == "python":
            continue
        if isinstance(value, dict):
            value = value.get("version", "")
        specs.append(str(value or ""))
    return specs


def _manifest_specs(name: str, text: str) -> list[str]:
    if name == "package.json":
        return _package_specs(text)
    if name == "requirements.txt":
        return _requirement_specs(text)
    if name == "pyproject.toml":
        return _pyproject_specs(text)
    return []


def _is_floating(spec: str) -> bool:
    text = (spec or "").strip().lower()
    if not text:
        return True
    return text.startswith(_FLOATING_PREFIXES)


def detect_deps_missing(snapshot: Snapshot) -> RuleOutcome:
    pick = select(snapshot.blobs, is_manifest)
    if pick is None:
        finding = make_finding(
            R03,
            title="缺少依赖清单",
            evidence=(
                "未找到任何依赖清单（已检查根目录与一层子目录）：package.json、requirements*.txt、"
                "pyproject.toml、setup.py、setup.cfg、Pipfile、go.mod、pom.xml、build.gradle(.kts)、"
                "Cargo.toml、composer.json、Gemfile、*.csproj、*.sln/.slnx、vcpkg.json、"
                "conanfile.txt/.py、CMakeLists.txt"
            ),
            impact="无法判断依赖来源与版本，供应链风险不可评估",
            fix="补充标准依赖清单（requirements.txt / pyproject.toml / package.json 等）",
        )
        return failed(R03, "根目录与一层普通子目录均无依赖清单", finding)

    if pick.weak:
        return degraded(R03, pick.basis(), file=pick.path)
    return passed(R03, pick.basis(), file=pick.path)


def detect_deps_unpinned(snapshot: Snapshot) -> RuleOutcome:
    pick = select(snapshot.blobs, is_parseable_manifest)
    if pick is None:
        return na(R04, "无可解析清单，规则不适用")
    if pick.weak:
        return degraded(R04, pick.basis(), file=pick.path)

    lockfile = find_lockfile(snapshot, pick.level)
    if lockfile is not None:
        lock_level = level_of(lockfile)
        label = LEVEL_LABEL.get(lock_level or 0, "子目录")
        return passed(R04, f"{label} {lockfile}", file=lockfile)

    level = pick.level
    parseable = [
        blob
        for blob in snapshot.blobs
        if level_of(blob.path) == level and is_parseable_manifest(blob.path)
    ]

    specs: list[str] = []
    unavailable: list[str] = []
    primary: str | None = None
    for blob in parseable:
        text = snapshot.contents.get(blob.path)
        if text is None:
            unavailable.append(blob.path)
            continue
        try:
            parsed = _manifest_specs(_basename(blob.path), text)
        except (json.JSONDecodeError, tomllib.TOMLDecodeError, ValueError, TypeError):
            unavailable.append(blob.path)
            continue
        if primary is None:
            primary = blob.path
        specs.extend(parsed)

    if primary is None:
        return degraded(
            R04,
            f"{LEVEL_LABEL[level]}依赖清单全部读取失败（{'、'.join(unavailable)}），结论缺失",
            file=unavailable[0] if unavailable else "",
        )

    if unavailable:
        finding = degraded_finding(
            R04,
            reason=f"{'、'.join(unavailable)} 读取失败",
            title="依赖版本浮动无法完整判定",
            impact="缺失部分清单，浮动比例可能被低估",
            fix="检查 raw 内容是否可达，或人工复核依赖清单",
            file=primary,
        )
        return degraded(
            R04,
            f"{'、'.join(unavailable)} 读取失败，仅按 {primary} 判断，结论不完整",
            finding=finding,
            file=primary,
        )

    total = len(specs)
    if total == 0:
        return passed(R04, f"{pick.basis()} 未声明任何依赖，无需判定浮动", file=primary)

    floating = sum(1 for spec in specs if _is_floating(spec))
    if floating / total <= 0.5:
        return passed(
            R04,
            f"{pick.basis()} 浮动 {floating} 个 / 总计 {total} 个（≤50%），无 lockfile",
            file=primary,
        )

    finding = make_finding(
        R04,
        title="依赖版本大面积浮动",
        evidence=(
            f"浮动 {floating} 个 / 总计 {total} 个；未发现 lockfile"
            f"（package-lock.json / yarn.lock / pnpm-lock.yaml / poetry.lock / Pipfile.lock）"
        ),
        impact="构建结果不可复现，依赖漂移可能引入不可控风险",
        fix="固定依赖版本，并提交对应 lockfile",
        file=primary,
    )
    return failed(
        R04,
        f"{pick.basis()} 浮动 {floating} 个 / 总计 {total} 个（>50%），无 lockfile",
        finding,
    )


def _tree_entry(paths: list[str]) -> tuple[str, str] | None:
    buckets: dict[str, list[str]] = {key: [] for key in _ENTRY_ORDER}
    for path in paths:
        name = _basename(path)
        root = "/" not in path
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in COMPOSE_GLOBS):
            buckets["compose"].append(path)
        elif name == "Dockerfile":
            buckets["dockerfile"].append(path)
        elif name == "Makefile":
            buckets["makefile"].append(path)
        elif fnmatch.fnmatchcase(name, "*.sln") or fnmatch.fnmatchcase(name, "*.slnx"):
            buckets["sln"].append(path)
        elif name == "CMakeLists.txt" and root:
            buckets["cmake"].append(path)
        elif root and name in WINDOWS_BUILD_FILES:
            buckets["build_win"].append(path)
        elif root and name == "build.sh":
            buckets["build_sh"].append(path)
        elif root and fnmatch.fnmatchcase(name, "*.csproj"):
            buckets["csproj"].append(path)
    for kind in _ENTRY_ORDER:
        if buckets[kind]:
            return kind, sorted(buckets[kind])[0]
    return None


def _entry_basis(level: int, kind: str, path: str) -> str:
    basis = f"{LEVEL_LABEL[level]} {path}"
    if kind in {"sln", "build_win", "csproj"}:
        return f"{basis}（Windows 专用构建入口，Linux/容器环境需替代方案）"
    if kind == "cmake":
        return f"{basis}（根目录 CMake 构建）"
    if kind == "build_sh":
        return f"{basis}（根目录 shell 构建脚本）"
    if kind == "compose":
        return f"{basis}（docker-compose）"
    return basis


def detect_start_missing(snapshot: Snapshot) -> RuleOutcome:
    for level in (LEVEL_ROOT, LEVEL_CHILD, LEVEL_WEAK):
        paths = [blob.path for blob in snapshot.blobs if level_of(blob.path) == level]
        if not paths:
            continue

        entry = _tree_entry(paths)
        if entry is not None:
            kind, path = entry
            if level == LEVEL_WEAK:
                return degraded(R05, f"仅在弱目录 {path} 发现，无法确认主项目", file=path)
            return passed(R05, _entry_basis(level, kind, path), file=path)

        packages = sorted(path for path in paths if _basename(path) == "package.json")
        unreadable: list[str] = []
        for package in packages:
            text = snapshot.contents.get(package)
            if text is None:
                unreadable.append(package)
                continue
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                unreadable.append(package)
                continue
            scripts = data.get("scripts") if isinstance(data, dict) else None
            if isinstance(scripts, dict):
                for key in SCRIPT_KEYS:
                    if key in scripts:
                        note = START_ENTRY_NOTE if key in START_KEYS else BUILD_ENTRY_NOTE
                        if level == LEVEL_WEAK:
                            return degraded(
                                R05,
                                f"仅在弱目录 {package} 发现{note}，无法确认主项目",
                                file=package,
                            )
                        return passed(
                            R05,
                            f"{LEVEL_LABEL[level]} {package}:scripts.{key}（{note}）",
                            file=package,
                        )

        if level == LEVEL_WEAK:
            continue
        if unreadable:
            return degraded(
                R05,
                f"{LEVEL_LABEL[level]} {unreadable[0]} 读取失败，缺启动脚本信息，结论不完整",
                file=unreadable[0],
            )

    finding = make_finding(
        R05,
        title="缺少可识别的构建/启动入口",
        evidence=(
            "未找到 Makefile / Dockerfile / docker-compose* / *.sln(.slnx) / CMakeLists.txt / "
            "build.ps1 / build.bat / build.sh / 根目录 *.csproj，"
            "package.json 也未声明 start/dev/serve/build/compile（已检查根目录与一层普通子目录）"
        ),
        impact="无法确认服务如何构建与启动，部署与验证成本高",
        fix="补充 Dockerfile / Makefile 或构建脚本，或在 package.json 声明 start/dev/serve",
    )
    return failed(R05, "根目录与一层普通子目录均无构建/启动入口", finding)


R03 = Rule(
    id="R03",
    slug="deps-missing",
    dimension="deps",
    severity="high",
    points=-8,
    needs_content=False,
    detect=detect_deps_missing,
)

R04 = Rule(
    id="R04",
    slug="deps-unpinned",
    dimension="deps",
    severity="low",
    points=-2,
    needs_content=True,
    detect=detect_deps_unpinned,
)

R05 = Rule(
    id="R05",
    slug="start-missing",
    dimension="deps",
    severity="medium",
    points=-4,
    needs_content=True,
    detect=detect_start_missing,
)
