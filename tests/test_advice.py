from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import advice, main
from app.models import (
    AdviceAction,
    AdviceResponse,
    CapInfo,
    Check,
    CommitInfo,
    DeductionInfo,
    DimensionInfo,
    FileEntry,
    Finding,
    InspectResponse,
    RepoInfo,
    ScanInfo,
    ScoreInfo,
    ScopeInfo,
    SourceInfo,
    StatsInfo,
    TimingInfo,
)

SHA = "a" * 40


@pytest.fixture(autouse=True)
def advice_env(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOGATE_ADVICE_CACHE_DIR", str(tmp_path / "advice"))
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)


def make_report() -> InspectResponse:
    finding = Finding(
        id="R09-env-committed",
        rule_id="R09",
        category="secrets",
        dimension="secrets",
        severity="critical",
        title=".env 随代码入库且含非占位值",
        file=".env",
        line=3,
        evidence_masked="OPENAI_API_KEY=sk-a***ij",
        impact="真实凭据可能已泄露",
        fix="移出版本库并轮换凭据",
    )
    checks = [
        Check(rule_id=f"R{index:02d}", status="fail" if index in {2, 3, 5, 9} else "pass", basis="测试")
        for index in range(1, 15)
    ]
    return InspectResponse(
        repo=RepoInfo(
            full_name="octocat/Hello-World",
            owner="octocat",
            name="Hello-World",
            default_branch="master",
            head_sha=SHA,
        ),
        ref="master",
        commit_sha=SHA,
        commit=CommitInfo(sha=SHA, message="secret raw commit message", author="octocat", date="2025-01-01"),
        scan=ScanInfo(scanned_at="2026-11-27T02:00:00+08:00", duration_ms=1200),
        source=SourceInfo(),
        scope=ScopeInfo(files_scanned=20),
        stats=StatsInfo(
            content_total_text_files=20,
            path_scan_files=10,
            secret_sampling_ratio=0.5,
        ),
        timing=TimingInfo(),
        files=[FileEntry(path="README.md", size=10, is_text=True)],
        findings=[finding],
        checks=checks,
        score=ScoreInfo(
            total=59,
            raw_total=78,
            grade="D",
            verdict="不建议接入",
            caps=[CapInfo(type="env_committed", limit=69, reason="R09 上限 69")],
            dimensions=[
                DimensionInfo(key="secrets", label="密钥与敏感信息", weight=30, deduction=10, score=20),
                DimensionInfo(key="portability", label="平台可移植", weight=25, deduction=0, score=25),
                DimensionInfo(key="docs", label="文档完备", weight=20, deduction=8, score=12),
                DimensionInfo(key="deps", label="依赖与入口", weight=15, deduction=12, score=3),
                DimensionInfo(key="maintenance", label="维护状态", weight=10, deduction=0, score=10),
            ],
            deductions=[
                DeductionInfo(rule_id="R09", points=-30, reason=".env 入库", file=".env"),
                DeductionInfo(rule_id="R03", points=-8, reason="缺少依赖清单", file=""),
                DeductionInfo(rule_id="R02", points=-4, reason="README 不完整", file="README.md"),
                DeductionInfo(rule_id="R05", points=-4, reason="缺少启动入口", file=""),
            ],
        ),
    )


def valid_model_json() -> str:
    return json.dumps(
        {
            "summary": "先处理 .env 入库风险",
            "actions": [
                {
                    "title": "处理 .env 入库",
                    "why": "R09 检测到 .env 中的真实凭据",
                    "how": "移出版本库并轮换凭据，再加入 .gitignore",
                    "effort": "S",
                    "rule_ids": ["R09"],
                }
            ],
            "caveats": ["抽样率 50.0%，不代表全仓结论"],
        },
        ensure_ascii=False,
    )


def test_prompt_contains_only_masked_evidence() -> None:
    report = make_report()
    context = advice.build_context(report)
    prompt = advice.build_prompt(context)
    assert "sk-a***ij" in prompt
    assert "sk-abcdefghij" not in prompt
    assert "secret raw commit message" not in prompt
    assert "README.md" not in prompt
    assert ".env" in prompt
    assert len(context["findings"]) <= 8


def test_context_caps_findings_at_eight() -> None:
    report = make_report()
    report.findings = [
        report.findings[0].model_copy(update={"id": f"finding-{index}"}) for index in range(12)
    ]
    context = advice.build_context(report)
    assert len(context["findings"]) == 8


def test_unknown_rule_action_is_dropped() -> None:
    raw = json.dumps(
        {
            "summary": "建议",
            "actions": [
                {
                    "title": "合法动作",
                    "why": "R09 检测到风险",
                    "how": "处理 .env",
                    "effort": "S",
                    "rule_ids": ["R09"],
                },
                {
                    "title": "非法动作",
                    "why": "R99 不存在",
                    "how": "编造",
                    "effort": "M",
                    "rule_ids": ["R99"],
                },
            ],
            "caveats": [],
        },
        ensure_ascii=False,
    )
    parsed = advice.validate_model_output(raw, {"R09"})
    assert [action.title for action in parsed.actions] == ["合法动作"]


def test_all_actions_unknown_falls_back() -> None:
    raw = json.dumps(
        {
            "summary": "建议",
            "actions": [
                {"title": "非法", "why": "R99", "how": "编造", "effort": "M", "rule_ids": ["R99"]}
            ],
            "caveats": [],
        },
        ensure_ascii=False,
    )
    with pytest.raises(advice.AdviceValidationError):
        advice.validate_model_output(raw, {"R09"})


def test_delete_env_action_is_rejected() -> None:
    raw = json.dumps(
        {
            "summary": "删除 .env 即可",
            "actions": [
                {
                    "title": "删除 .env 文件",
                    "why": "R09 检测到 .env 入库",
                    "how": "直接删除该文件并重新提交",
                    "effort": "S",
                    "rule_ids": ["R09"],
                }
            ],
            "caveats": ["抽样率 50.0%，不代表全仓结论"],
        },
        ensure_ascii=False,
    )
    with pytest.raises(advice.AdviceValidationError):
        advice.validate_model_output(raw, {"R09"})


def test_api_without_key_returns_rules_fallback(monkeypatch) -> None:
    report = make_report()

    async def fake_inspect(_: str, *, refresh: bool = False) -> InspectResponse:
        return report

    monkeypatch.setattr(main, "_inspect", fake_inspect)
    client = TestClient(main.app)
    response = client.post("/api/advice", json={"repo": "https://github.com/octocat/Hello-World"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["source"] == "rules_fallback"
    assert body["model"] is None
    assert body["actions"]
    assert body["fallback_reason"] == "no_api_key"
    assert "抽样率" in body["caveats"][0]


def test_advice_cache_avoids_second_model_call(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": valid_model_json()}}],
                "usage": {"total_tokens": 42},
            },
        )

    transport = httpx.MockTransport(handler)
    report = make_report()
    first = asyncio.run(advice.build_advice(report, transport=transport))
    accessor = asyncio.run(advice.build_advice(report, transport=transport))
    assert first.source == "model"
    assert first.model == "test-model"
    assert first.tokens == 42
    assert first.cache.hit is False
    assert accessor.cache.hit is True
    assert calls[0] == 1
    assert accessor.summary == first.summary


def test_timeout_falls_back_with_warning(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")

    report = make_report()
    result = asyncio.run(advice.build_advice(report, transport=httpx.MockTransport(handler)))
    assert result.source == "rules_fallback"
    assert result.cache.hit is False
    assert result.warnings
    assert result.warnings[0].code == "ADVICE_FALLBACK"
    assert result.model == "test-model"


def test_bad_json_falls_back_with_warning(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    report = make_report()
    result = asyncio.run(advice.build_advice(report, transport=httpx.MockTransport(handler)))
    assert result.source == "rules_fallback"
    assert result.warnings[0].code == "ADVICE_FALLBACK"
    assert result.warnings[0].message


def test_page_shows_rules_fallback_card(monkeypatch) -> None:
    report = make_report()

    async def fake_inspect(_: str, *, refresh: bool = False) -> InspectResponse:
        return report

    monkeypatch.setattr(main, "_inspect", fake_inspect)
    client = TestClient(main.app)
    response = client.post("/", data={"repo_url": "https://github.com/octocat/Hello-World"})
    assert response.status_code == 200
    html = response.text
    assert "接入建议（规则生成）" in html
    assert "未配置模型" in html
    assert "抽样率" in html
    assert "不代表全仓结论" in html


def test_page_escapes_model_text(monkeypatch) -> None:
    report = make_report()

    async def fake_inspect(_: str, *, refresh: bool = False) -> InspectResponse:
        return report

    async def fake_advice(_: InspectResponse, *, refresh: bool = False) -> AdviceResponse:
        return AdviceResponse(
            source="model",
            model="<b>model</b>",
            summary="<script>alert('x')</script>",
            actions=[
                AdviceAction(
                    title="<img src=x>",
                    why="R09 <b>why</b>",
                    how="保留模板",
                    effort="S",
                    rule_ids=["R09"],
                )
            ],
            caveats=["<script>caveat</script>"],
            finding_count=1,
            sampling_ratio=1.0,
        )

    monkeypatch.setattr(main, "_inspect", fake_inspect)
    monkeypatch.setattr(main, "build_advice", fake_advice)
    client = TestClient(main.app)
    response = client.post("/", data={"repo_url": "https://github.com/octocat/Hello-World"})
    assert response.status_code == 200
    html = response.text
    assert "&lt;script&gt;alert" in html
    assert "<script>alert" not in html
    assert "&lt;img src=x&gt;" in html
    assert "<img src=x>" not in html


def test_allowed_rules_include_low_finding_rules() -> None:
    """R09 low（.gitignore 未含 .env）也允许模型引用，否则合理建议会被整条丢弃。"""
    report = make_report()
    report.findings = [report.findings[0].model_copy(update={"severity": "low"})]
    report.checks = [check.model_copy(update={"status": "pass"}) for check in report.checks]
    allowed = advice._allowed_rules(report)
    assert "R09" in allowed


def test_quota_error_is_classified(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": "insufficient balance"})

    report = make_report()
    result = asyncio.run(advice.build_advice(report, transport=httpx.MockTransport(handler)))
    assert result.source == "rules_fallback"
    assert result.fallback_reason == "quota_exceeded"
    assert "402" in result.warnings[0].message


def test_timeout_error_is_classified(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")

    report = make_report()
    result = asyncio.run(advice.build_advice(report, transport=httpx.MockTransport(handler)))
    assert result.fallback_reason == "timeout"


def test_refresh_button_passes_flag(monkeypatch) -> None:
    calls: dict[str, bool] = {}

    async def fake_inspect(_: str, *, refresh: bool = False) -> InspectResponse:
        calls["refresh"] = refresh
        return make_report()

    monkeypatch.setattr(main, "_inspect", fake_inspect)
    client = TestClient(main.app)
    response = client.post(
        "/", data={"repo_url": "https://github.com/octocat/Hello-World", "refresh": "1"}
    )
    assert response.status_code == 200
    assert calls["refresh"] is True
    assert "重新扫描" in response.text


def test_get_with_repo_renders_shared_report(monkeypatch) -> None:
    async def fake_inspect(_: str, *, refresh: bool = False) -> InspectResponse:
        return make_report()

    monkeypatch.setattr(main, "_inspect", fake_inspect)
    client = TestClient(main.app)
    response = client.get("/?repo=https://github.com/octocat/Hello-World")
    assert response.status_code == 200
    assert "59" in response.text
    assert "不建议接入" in response.text


def test_repo_without_findings_skips_model_call(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    calls = [0]

    def handler(_: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": valid_model_json()}}],
                "usage": {"total_tokens": 1},
            },
        )

    report = make_report()
    report.findings = []
    result = asyncio.run(advice.build_advice(report, transport=httpx.MockTransport(handler)))
    assert result.source == "rules_fallback"
    assert result.fallback_reason == "no_findings"
    assert result.actions
    assert calls[0] == 0


def test_page_shows_no_findings_label(monkeypatch) -> None:
    report = make_report()
    report.findings = []

    async def fake_inspect(_: str, *, refresh: bool = False) -> InspectResponse:
        return report

    monkeypatch.setattr(main, "_inspect", fake_inspect)
    client = TestClient(main.app)
    response = client.post("/", data={"repo_url": "https://github.com/octocat/Hello-World"})
    assert response.status_code == 200
    assert "接入检查结论" in response.text
    assert "无需整改（无阻断项）" in response.text
