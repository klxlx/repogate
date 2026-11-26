"""R03 / R04 / R05：依赖清单存在性、版本浮动、启动入口。"""

from __future__ import annotations

import fnmatch
import json
import re
import tomllib

from ..snapshot import BlobRef, Snapshot
from .base import Rule, degraded_finding, make_finding

MANIFEST_EXACT = frozenset(
    {
        "package.json", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile",
        "go.mod", "pom.xml", "build.gradle", "build.gradle.kts", "Cargo.toml",
        "composer.json", "Gemfile", "vcpkg.json", "conanfile.txt", "conanfile.py",
        "CMakeLists.txt",
    }
)
MANIFEST_GLOBS = ("requirements*.txt", "*.csproj", "*.sln")
MANIFEST_LABELS = (
    "package.json、requirements*.txt、pyproject.toml、setup.py、setup.cfg、Pipfile、"
    "go.mod、pom.xml、build.gradle(.kts)、Cargo.toml、composer.json、Gemfile、"
    "*.csproj、*.sln、vcpkg.json、conanfile.txt/.py、CMakeLists.txt"
)

LOCKFILES = frozenset(
    {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock"}
)
PARSEABLE_MANIFESTS = frozenset({"package.json", "requirements.txt", "pyproject.toml"})

LAUNCH_EXACT = frozenset({"Makefile", "Dockerfile"})
LAUNCH_GLOBS = ("docker-compose*.yml", "docker-compose*.yaml")

_OPERATOR_RE = re.compile(r"(==|>=|<=|~=|!=|>|<)")
_FLOATING_PREFIXES = ("^", "~", "*", ">", "latest")


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def find_manifests(snapshot: Snapshot) -> list[BlobRef]:
    found: list[BlobRef] = []
    for blob in snapshot.shallow_blobs():
        name = _basename(blob.path)
        if name in MANIFEST_EXACT or any(
            fnmatch.fnmatchcase(name, pattern) for pattern in MANIFEST_GLOBS
        ):
            found.append(blob)
    return found


def find_lockfile(snapshot: Snapshot) -> str | None:
    for blob in snapshot.shallow_blobs():
        if _basename(blob.path) in LOCKFILES:
            return blob.path
    return None


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


def detect_deps_missing(snapshot: Snapshot):
    if find_manifests(snapshot):
        return None
    return make_finding(
        R03,
        title="缺少依赖清单",
        evidence=f"未找到以下候选清单文件（已检查根目录与一层子目录）：{MANIFEST_LABELS}",
        impact="无法判断依赖来源与版本，供应链风险不可评估",
        fix="补充标准依赖清单（requirements.txt / pyproject.toml / package.json 等）",
    )


def detect_deps_unpinned(snapshot: Snapshot):
    if find_lockfile(snapshot) is not None:
        return None

    parseable = [
        blob
        for blob in snapshot.shallow_blobs()
        if _basename(blob.path) in PARSEABLE_MANIFESTS
    ]
    if not parseable:
        return None  # 没有可解析清单，交给 R03

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
        return degraded_finding(
            R04,
            reason="依赖清单内容拉取失败或解析失败",
            title="依赖版本浮动无法判定",
            impact="无法判断是否存在大面积浮动版本",
            fix="检查 raw 内容是否可达，或人工复核依赖清单",
            file=unavailable[0] if unavailable else "",
            detail=f"涉及 {len(unavailable)} 个清单文件",
        )

    total = len(specs)
    if total == 0:
        return None
    floating = sum(1 for spec in specs if _is_floating(spec))
    if floating / total <= 0.5:
        return None

    note = f"；{len(unavailable)} 个清单内容不可用、未计入" if unavailable else ""
    return make_finding(
        R04,
        title="依赖版本大面积浮动",
        evidence=(
            f"浮动 {floating} 个 / 总计 {total} 个；未发现 lockfile"
            f"（package-lock.json / yarn.lock / pnpm-lock.yaml / poetry.lock / Pipfile.lock）{note}"
        ),
        impact="构建结果不可复现，依赖漂移可能引入不可控风险",
        fix="固定依赖版本，并提交对应 lockfile",
        file=primary,
    )


def detect_start_missing(snapshot: Snapshot):
    for blob in snapshot.shallow_blobs():
        name = _basename(blob.path)
        if name in LAUNCH_EXACT or any(
            fnmatch.fnmatchcase(name, pattern) for pattern in LAUNCH_GLOBS
        ):
            return None

    package_blobs = [
        blob for blob in snapshot.shallow_blobs() if _basename(blob.path) == "package.json"
    ]
    degraded: list[str] = []
    for blob in package_blobs:
        text = snapshot.contents.get(blob.path)
        if text is None:
            degraded.append(blob.path)
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            degraded.append(blob.path)
            continue
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if isinstance(scripts, dict) and any(
            key in scripts for key in ("start", "dev", "serve")
        ):
            return None

    if degraded:
        return degraded_finding(
            R05,
            reason="package.json 内容拉取失败",
            title="启动入口无法判定",
            impact="无法确认服务如何启动，部署与验证成本不确定",
            fix="检查 raw 内容是否可达，或人工确认启动方式",
            file=degraded[0],
            detail="package.json 的 scripts 未参与判定",
        )

    has_package = bool(package_blobs)
    package_note = (
        "package.json 未声明 start/dev/serve"
        if has_package
        else "没有 package.json 可提供 scripts"
    )
    return make_finding(
        R05,
        title="缺少可识别的启动入口",
        evidence=(
            f"未找到 Makefile / Dockerfile / docker-compose*.yml/*.yaml；"
            f"{package_note}（已检查根目录与一层子目录）"
        ),
        impact="无法确认服务如何启动，部署与验证成本高",
        fix="补充 Makefile 或 Dockerfile，或在 package.json 声明 start/dev/serve",
    )


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
