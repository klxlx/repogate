"""AI 接入建议：白名单上下文、强 schema 校验、规则 fallback 与按 commit 缓存。

设计原则：
- 模型只看结构化事实，不看文件正文、README 正文或 commit message；
- 模型输出必须过 schema，未知 rule_id 的动作整条丢弃；
- 没有模型密钥时不假装有模型，返回规则 fallback 并标记来源。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from .github import get_client
from .models import (
    AdviceAction,
    AdviceCacheInfo,
    AdviceResponse,
    InspectResponse,
    WarningInfo,
)
from .rules import RULES
from .version import SCHEMA_VERSION

ADVICE_PROMPT_VERSION = "advice-v2"
MAX_FINDINGS = 8
MAX_ACTIONS = 3
MAX_CAVEATS = 2
FORBIDDEN_TEXT_RE = re.compile(r"(?i)(https?://|sk-|AKIA)")
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


SYSTEM_PROMPT = """你是企业内网仓库准入审查的 AI 助手。只能依据 user 提供的 JSON 事实回答，
不得使用外部知识，不得猜测文件名、行号、规则或仓库事实。找不到依据就不说。

只输出一个 JSON 对象，不要 Markdown 代码块，不要额外文字：
{
  "summary": "一句话结论，最多 80 个中文字符",
  "actions": [
    {"title": "动作标题，最多 20 个中文字符",
     "why": "为什么做，最多 60 个中文字符，必须引用 rule_id",
     "how": "可执行步骤，最多 120 个中文字符",
     "effort": "S|M|L",
     "rule_ids": ["R09"]}
  ],
  "caveats": ["最多 2 条，每条最多 60 个中文字符，必须包含抽样率限制"]
}

要求：
1. actions 只能 1-3 条，按优先级排列；rule_ids 只能来自输入 checks 中 status 为 fail 或 degraded 的规则。
2. 不得输出密钥、令牌、URL 或任何原文；不得编造文件名/行号；证据只能引用输入的 evidence。
3. 建议必须可执行，但只针对给定 findings；信息不足时少说。
4. caveats 必须提到当前抽样率及其“不代表全仓结论”的限制。
5. 不得建议删除项目启动所必需的模板文件（尤其是模板里的 .env）；占位模板应保留，只处理真实凭据，并补 .env.example / .gitignore。"""


@dataclass
class AdviceContent:
    summary: str
    actions: list[AdviceAction]
    caveats: list[str]


class AdviceValidationError(ValueError):
    """模型输出不符合服务端 schema。"""


class AdviceCallError(ValueError):
    """模型调用失败；reason 用于区分鉴权、额度、限流、超时与坏响应。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class RuleAdvice:
    title: str
    why: str
    how: str
    effort: Literal["S", "M", "L"]


# 规则表映射：fallback 和模型都不需要重新编规则文案。
RULE_ADVICE: dict[str, RuleAdvice] = {
    "R01": RuleAdvice("补充 README", "仓库没有 README，用途与接入方式无法确认（R01）", "补充 README，覆盖用途、安装、运行与依赖", "S"),
    "R02": RuleAdvice("补齐 README 章节", "README 缺少安装/运行/配置/依赖说明（R02）", "补齐四类说明，再按文档走一遍接入流程", "S"),
    "R03": RuleAdvice("补充依赖清单", "没有可识别的依赖清单，供应链不可复核（R03）", "补充 requirements.txt、pyproject.toml 或 package.json 等", "M"),
    "R04": RuleAdvice("固定依赖版本", "依赖版本大面积浮动，构建结果不可复现（R04）", "固定版本并提交对应的 lockfile", "M"),
    "R05": RuleAdvice("补充启动入口", "缺少可识别的构建或启动入口（R05）", "补充 Dockerfile/Makefile，或声明 start/dev 脚本", "M"),
    "R06": RuleAdvice("补充跨平台脚本", "存在 Windows 专用脚本，迁移到其他平台有成本（R06）", "补充 .sh/Makefile，或说明仅在 Windows 使用", "S"),
    "R07": RuleAdvice("补跨平台入口", "只有 Windows 脚本，没有跨平台入口（R07）", "补充 .sh、Makefile 或 Dockerfile 入口", "M"),
    "R08": RuleAdvice("移除硬编码路径", "源码或脚本包含硬编码绝对路径（R08）", "改为相对路径、环境变量或可配置参数", "M"),
    "R09": RuleAdvice("处理 .env 入库", "存在 .env 入库或忽略规则缺失，可能暴露凭据（R09）", "先确认是否模板占位；保留启动所需模板，轮换真实值并加入 .gitignore", "S"),
    "R10": RuleAdvice("移除私钥材料", "仓库内容命中私钥材料，凭据可能已泄露（R10）", "立即吊销并轮换，从版本库移除私钥文件", "S"),
    "R11": RuleAdvice("吊销访问令牌", "仓库内容命中疑似访问令牌（R11）", "确认并吊销令牌，改用环境变量或密钥管理服务", "S"),
    "R12": RuleAdvice("补充许可证", "根目录缺少许可证，授权范围不明确（R12）", "补充 LICENSE（如 MIT/Apache-2.0）并与元信息一致", "S"),
    "R13": RuleAdvice("确认维护状态", "仓库超过 12 个月未推送，可能已停更（R13）", "确认上游维护状态，必要时锁定版本或寻找替代", "S"),
    "R14": RuleAdvice("评估归档替代", "仓库已归档，后续安全修复无法保证（R14）", "评估替代仓库；确需使用则锁定 commit 并自行维护", "M"),
}

RULE_DIMENSION = {rule.id: rule.dimension for rule in RULES}
DIMENSION_PRIORITY = {"secrets": 0, "portability": 1, "docs": 2, "deps": 3, "maintenance": 4}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _advice_root() -> Path:
    override = os.environ.get("REPOGATE_ADVICE_CACHE_DIR", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / ".cache" / "advice"


def _safe_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:100] or "unknown"


def _cache_path(owner: str, repo: str, sha: str, model: str) -> Path:
    return (
        _advice_root()
        / f"{_safe_part(owner)}__{_safe_part(repo)}"
        / f"{sha}__{_safe_part(model)}__{ADVICE_PROMPT_VERSION}.json"
    )


def _cache_report(advice: AdviceResponse) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "cached_at": _utc_now(),
        "source": advice.source,
        "model": advice.model,
        "prompt_version": advice.prompt_version,
        "latency_ms": advice.latency_ms,
        "tokens": advice.tokens,
        "summary": advice.summary,
        "actions": [action.model_dump(mode="json") for action in advice.actions],
        "caveats": advice.caveats,
        "warnings": [warning.model_dump(mode="json") for warning in advice.warnings],
        "fallback_reason": advice.fallback_reason,
        "finding_count": advice.finding_count,
        "sampling_ratio": advice.sampling_ratio,
        "repo": advice.repo,
        "commit_sha": advice.commit_sha,
    }


def _load_cache(path: Path) -> AdviceResponse | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != SCHEMA_VERSION:
            return None
        if not isinstance(payload.get("actions"), list) or not payload["actions"]:
            return None
        if not isinstance(payload.get("caveats"), list):
            return None
        return AdviceResponse(
            schema_version=SCHEMA_VERSION,
            source=payload.get("source", "rules_fallback"),
            model=payload.get("model"),
            prompt_version=payload.get("prompt_version", ADVICE_PROMPT_VERSION),
            latency_ms=int(payload.get("latency_ms") or 0),
            tokens=int(payload.get("tokens") or 0),
            summary=str(payload.get("summary") or ""),
            actions=[AdviceAction.model_validate(item) for item in payload["actions"]],
            caveats=[str(item) for item in payload["caveats"]],
            cache=AdviceCacheInfo(hit=True, cached_at=str(payload.get("cached_at") or "")),
            warnings=[WarningInfo.model_validate(item) for item in payload.get("warnings", [])],
            fallback_reason=payload.get("fallback_reason"),
            finding_count=int(payload.get("finding_count") or 0),
            sampling_ratio=float(payload.get("sampling_ratio") or 0.0),
            repo=str(payload.get("repo") or ""),
            commit_sha=str(payload.get("commit_sha") or ""),
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _save_cache(path: Path, advice: AdviceResponse) -> str | None:
    cached_at = _utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    wrapper = _cache_report(advice)
    wrapper["cached_at"] = cached_at
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(path.parent), prefix=".advice.", suffix=".tmp", delete=False
        ) as handle:
            tmp_name = handle.name
            json.dump(wrapper, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
        return cached_at
    except OSError:
        return None
    finally:
        if tmp_name:
            try:
                Path(tmp_name).unlink()
            except OSError:
                pass


def build_context(result: InspectResponse) -> dict[str, Any]:
    """只构造白名单事实；不读文件正文、README、commit message 或原始证据。"""
    ordered = sorted(
        result.findings,
        key=lambda item: (SEVERITY_ORDER.get(item.severity, 9), item.rule_id, item.file),
    )[:MAX_FINDINGS]
    findings = [
        {
            "rule_id": finding.rule_id,
            "severity": finding.severity,
            "path": finding.file,
            "line": finding.line,
            "evidence": finding.evidence_masked,
            "impact": finding.impact,
        }
        for finding in ordered
    ]
    return {
        "repo": {
            "owner": result.repo.owner,
            "name": result.repo.name,
            "head_sha": result.commit.sha,
            "grade": result.score.grade,
            "total": result.score.total,
        },
        "score": {
            "dimensions": [
                {
                    "key": dim.key,
                    "label": dim.label,
                    "weight": dim.weight,
                    "deduction": dim.deduction,
                    "score": dim.score,
                }
                for dim in result.score.dimensions
            ],
            "caps": [
                {"type": cap.type, "limit": cap.limit, "reason": cap.reason}
                for cap in result.score.caps
            ],
        },
        "checks": [
            {"rule_id": check.rule_id, "status": check.status} for check in result.checks
        ],
        "findings": findings,
        "stats": {
            "text_files": result.stats.content_total_text_files,
            "sampled": result.stats.path_scan_files,
            "sampling_ratio": result.stats.secret_sampling_ratio,
        },
    }


def build_prompt(context: dict[str, Any]) -> str:
    return json.dumps(context, ensure_ascii=False, separators=(",", ":"))


def _allowed_rules(result: InspectResponse) -> set[str]:
    """允许模型引用的规则：失败/降级规则，以及任何有 finding 的规则。

    只放 fail 会误伤「R09 low（.gitignore 未含 .env）」这类提示项：
    模型给出合理建议却因规则不在白名单被整条丢弃，最后退化成 rules fallback。
    """
    allowed = {
        check.rule_id
        for check in result.checks
        if check.status in {"fail", "degraded", "warn"}
    }
    allowed |= {finding.rule_id for finding in result.findings}
    return allowed


def _extract_json(text: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise AdviceValidationError("模型内容不是文本")
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise AdviceValidationError("模型没有返回 JSON")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AdviceValidationError("模型 JSON 解析失败") from exc
    if not isinstance(payload, dict):
        raise AdviceValidationError("模型 JSON 不是对象")
    return payload


def _safe_text(value: str, limit: int, field: str) -> str:
    text = value.strip()
    if not text:
        raise AdviceValidationError(f"{field} 不能为空")
    if len(text) > limit:
        raise AdviceValidationError(f"{field} 超过 {limit} 字")
    if FORBIDDEN_TEXT_RE.search(text):
        raise AdviceValidationError(f"{field} 包含禁止的 URL/密钥形态")
    return text


def _ensure_sampling_caveat(caveats: list[str], ratio: float) -> list[str]:
    if any("抽样率" in item for item in caveats):
        return caveats[:MAX_CAVEATS]
    generated = f"抽样率 {ratio * 100:.1f}%，不代表全仓结论"
    if len(caveats) < MAX_CAVEATS:
        return caveats + [generated]
    return caveats[: MAX_CAVEATS - 1] + [generated]


def validate_model_output(text: str, allowed_rules: set[str]) -> AdviceContent:
    payload = _extract_json(text)
    summary = _safe_text(str(payload.get("summary") or ""), 80, "summary")
    caveats_raw = payload.get("caveats")
    if caveats_raw is None:
        caveats_raw = []
    if not isinstance(caveats_raw, list) or len(caveats_raw) > MAX_CAVEATS:
        raise AdviceValidationError("caveats 必须是 0-2 条")
    caveats = [_safe_text(str(item), 60, "caveat") for item in caveats_raw]

    actions_raw = payload.get("actions")
    if not isinstance(actions_raw, list) or not 1 <= len(actions_raw) <= MAX_ACTIONS:
        raise AdviceValidationError("actions 必须是 1-3 条")

    actions: list[AdviceAction] = []
    for item in actions_raw:
        if not isinstance(item, dict):
            continue
        rule_ids = item.get("rule_ids")
        if not isinstance(rule_ids, list) or not rule_ids:
            continue
        if any(str(rule_id) not in allowed_rules for rule_id in rule_ids):
            # 未知/非 fail 的规则：整条动作丢弃。
            continue
        try:
            title = _safe_text(str(item.get("title") or ""), 20, "title")
            why = _safe_text(str(item.get("why") or ""), 60, "why")
            how = _safe_text(str(item.get("how") or ""), 120, "how")
            effort = item.get("effort")
            if effort not in {"S", "M", "L"}:
                raise AdviceValidationError("effort 必须是 S/M/L")
        except AdviceValidationError:
            continue
        combined = f"{title} {why} {how}"
        if "R09" in {str(rule_id) for rule_id in rule_ids} and "删除" in combined and (
            ".env" in combined or "该文件" in combined
        ):
            # 模板 .env 是启动所需文件；删除建议安全上不可接受，整条丢弃。
            continue
        actions.append(
            AdviceAction(
                title=title,
                why=why,
                how=how,
                effort=effort,
                rule_ids=[str(rule_id) for rule_id in rule_ids],
            )
        )

    if not actions:
        raise AdviceValidationError("过滤后 actions 为空")

    return AdviceContent(summary=summary, actions=actions[:MAX_ACTIONS], caveats=caveats)


def _rule_points(result: InspectResponse) -> dict[str, int]:
    points: dict[str, int] = {}
    for deduction in result.score.deductions:
        points[deduction.rule_id] = points.get(deduction.rule_id, 0) + abs(deduction.points)
    return points


def build_rules_fallback(result: InspectResponse) -> AdviceResponse:
    """按维度扣分从高到低取 Top 3；没有扣分时给一条保守接入动作。"""
    points = _rule_points(result)
    picks: list[str] = []
    for dimension in sorted(
        result.score.dimensions,
        key=lambda item: (-item.deduction, DIMENSION_PRIORITY.get(item.key, 99)),
    ):
        if dimension.deduction <= 0:
            continue
        current = [
            (rule_id, points.get(rule_id, 0))
            for rule_id in points
            if RULE_DIMENSION.get(rule_id) == dimension.key
            and rule_id in RULE_ADVICE
            and rule_id not in picks
        ]
        if current:
            picks.append(max(current, key=lambda pair: pair[1])[0])
        if len(picks) >= MAX_ACTIONS:
            break

    if not picks:
        picks = [
            check.rule_id
            for check in result.checks
            if check.status in {"fail", "degraded"} and check.rule_id in RULE_ADVICE
        ][:MAX_ACTIONS]

    actions: list[AdviceAction] = []
    for rule_id in picks:
        rule_advice = RULE_ADVICE[rule_id]
        actions.append(
            AdviceAction(
                title=rule_advice.title,
                why=rule_advice.why,
                how=rule_advice.how,
                effort=rule_advice.effort,
                rule_ids=[rule_id],
            )
        )

    if not actions:
        actions = [
            AdviceAction(
                title="保持当前接入流程",
                why="未发现扣分项，按现有流程接入",
                how="保留抽样率与缓存状态，接入后继续关注上游更新",
                effort="S",
                rule_ids=[],
            )
        ]

    ratio = result.stats.secret_sampling_ratio
    return AdviceResponse(
        source="rules_fallback",
        model=None,
        prompt_version=ADVICE_PROMPT_VERSION,
        latency_ms=0,
        tokens=0,
        summary=f"总分 {result.score.total}（{result.score.grade}）：{result.score.verdict}",
        actions=actions,
        caveats=[f"抽样率 {ratio * 100:.1f}%，不代表全仓结论"],
        finding_count=len(result.findings),
        sampling_ratio=ratio,
        repo=result.repo.full_name,
        commit_sha=result.commit.sha,
    )


def _llm_config() -> tuple[str, str, str] | None:
    base = os.environ.get("LLM_BASE_URL", "").strip().rstrip("/")
    key = os.environ.get("LLM_API_KEY", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    if not (base and key and model):
        return None
    return base, key, model


def _llm_url(base: str) -> str:
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


async def _call_model(
    context: dict[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, int, int]:
    config = _llm_config()
    if config is None:
        raise AdviceCallError("missing_config", "未配置 LLM")
    base, key, model = config
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(context)},
        ],
        "temperature": 0.2,
        "max_tokens": 900,
    }
    # DeepSeek V4 默认把 token 预算全部用在 reasoning_content，content 为空。
    # 仅对显式配置或 DeepSeek 端点关闭 thinking，保持其他 OpenAI 兼容端点不受影响。
    if os.environ.get("LLM_DISABLE_THINKING", "").strip() == "1" or "deepseek" in base.lower():
        payload["thinking"] = {"type": "disabled"}
    started = time.monotonic()
    last_error: AdviceCallError | None = None
    for attempt in range(2):
        try:
            if transport is not None:
                client = httpx.AsyncClient(transport=transport, timeout=20.0)
                close = True
            else:
                client = await get_client()
                close = False
            try:
                response = await client.post(
                    _llm_url(base), headers=headers, json=payload, timeout=20.0
                )
            finally:
                if close:
                    await client.aclose()
            if response.status_code >= 400:
                raise AdviceCallError(_http_reason(response.status_code), f"LLM HTTP {response.status_code}")
            try:
                body = response.json()
            except ValueError as exc:
                raise AdviceCallError("bad_json", "LLM 响应不是合法 JSON") from exc
            choices = body.get("choices") if isinstance(body, dict) else None
            if not isinstance(choices, list) or not choices:
                raise AdviceCallError("bad_response", "LLM 响应缺少 choices")
            content = choices[0].get("message", {}).get("content")
            if not isinstance(content, str):
                raise AdviceCallError("bad_response", "LLM 响应缺少 content")
            usage = body.get("usage") if isinstance(body, dict) else {}
            tokens = int(usage.get("total_tokens") or 0) if isinstance(usage, dict) else 0
            return content, tokens, int(round((time.monotonic() - started) * 1000))
        except AdviceCallError as exc:
            last_error = exc
        except httpx.TimeoutException as exc:
            last_error = AdviceCallError("timeout", f"LLM 超时：{exc}")
        except httpx.HTTPError as exc:
            last_error = AdviceCallError("network_error", f"LLM 网络错误：{exc}")
        except (ValueError, TypeError) as exc:
            last_error = AdviceCallError("llm_error", f"LLM 调用失败：{exc}")
        if attempt == 0:
            await asyncio.sleep(0.5)
    assert last_error is not None
    raise last_error


def _http_reason(status: int) -> str:
    if status in {401, 403}:
        return "auth_error"
    if status == 402:
        return "quota_exceeded"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "upstream_error"
    return "http_error"


async def build_advice(
    result: InspectResponse, *, refresh: bool = False, transport: httpx.AsyncBaseTransport | None = None
) -> AdviceResponse:
    owner = result.repo.owner or (result.repo.full_name.split("/")[0] if "/" in result.repo.full_name else "unknown")
    repo = result.repo.name or (result.repo.full_name.split("/")[-1] if "/" in result.repo.full_name else "unknown")
    sha = result.commit.sha or result.commit_sha
    configured = _llm_config()
    model_key = configured[2] if configured else "rules"
    path = _cache_path(owner, repo, sha, model_key)

    if not refresh:
        cached = _load_cache(path)
        if cached is not None:
            cached.cache = AdviceCacheInfo(hit=True, cached_at=cached.cache.cached_at)
            return cached

    if configured is None:
        advice = build_rules_fallback(result)
        advice.fallback_reason = "no_api_key" if not os.environ.get("LLM_API_KEY", "").strip() else "missing_config"
        cached_at = _save_cache(path, advice)
        if cached_at:
            advice.cache = AdviceCacheInfo(hit=False, cached_at=cached_at)
        return advice

    context = build_context(result)
    try:
        content, tokens, latency_ms = await _call_model(context, transport=transport)
        parsed = validate_model_output(content, _allowed_rules(result))
        advice = AdviceResponse(
            source="model",
            model=configured[2],
            prompt_version=ADVICE_PROMPT_VERSION,
            latency_ms=latency_ms,
            tokens=tokens,
            summary=parsed.summary,
            actions=parsed.actions,
            caveats=_ensure_sampling_caveat(parsed.caveats, result.stats.secret_sampling_ratio),
            finding_count=len(result.findings),
            sampling_ratio=result.stats.secret_sampling_ratio,
            repo=result.repo.full_name,
            commit_sha=sha,
        )
    except (AdviceValidationError, AdviceCallError, ValueError, TypeError) as exc:
        advice = build_rules_fallback(result)
        advice.model = configured[2]
        if isinstance(exc, AdviceCallError):
            advice.fallback_reason = exc.reason
        elif isinstance(exc, AdviceValidationError):
            reason = "empty_actions" if "actions 为空" in str(exc) else "schema_invalid"
            advice.fallback_reason = reason
        else:
            advice.fallback_reason = "llm_error"
        advice.warnings = [
            WarningInfo(
                code="ADVICE_FALLBACK",
                message=f"模型输出不可用，已回退规则引擎：{exc}",
            )
        ]

    cached_at = _save_cache(path, advice)
    if cached_at:
        advice.cache = AdviceCacheInfo(hit=False, cached_at=cached_at)
    return advice
