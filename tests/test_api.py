from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import time

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import TestClient as StarletteTestClient

from app import cache, main
from app.content import ContentResult
from app.errors import not_found, rate_limited, timeout
from app.github import RepoBundle, RepoHead
from app.models import InspectResponse, TimingInfo

SHA = "a" * 40


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """每个测试独立缓存目录，避免真实 .cache 干扰。"""
    monkeypatch.setenv("REPOGATE_CACHE_DIR", str(tmp_path / "reports"))


@pytest.fixture(autouse=True)
def stub_content(monkeypatch):
    """所有接口测试都不打 raw：内容层返回空，规则按降级路径走。"""

    async def fake_contents(owner, repo, sha, wanted, transport=None):
        return ContentResult(contents={}, failed=frozenset(), requests=0, bytes=0)

    monkeypatch.setattr(main, "fetch_contents", fake_contents)


def make_tree() -> dict:
    return {
        "sha": "root",
        "truncated": False,
        "tree": [
            {"path": "README.md", "type": "blob", "size": 6, "sha": "x"},
            {"path": "node_modules/x.js", "type": "blob", "size": 5, "sha": "x"},
            {"path": "src/main.py", "type": "blob", "size": 9, "sha": "x"},
        ],
    }


def repo_meta() -> dict:
    return {
        "full_name": "octocat/Hello-World",
        "stars": 4054,
        "language": "Python",
        "license": "MIT",
        "archived": False,
        "pushed_at": "2025-01-01T00:00:00Z",
    }


def fake_head(sha: str = SHA) -> RepoHead:
    return RepoHead(
        owner="octocat",
        repo="Hello-World",
        repo_meta=repo_meta(),
        ref="master",
        commit_sha=sha,
        commit={
            "sha": sha,
            "message": "initial commit",
            "author": "octocat",
            "date": "2025-01-01T00:00:00Z",
        },
        request_count=2,
        timing=TimingInfo(meta_ms=11, commit_ms=22, tree_ms=0),
    )


def fake_bundle(
    fallback: bool = False,
    github_truncated: bool = False,
    tree_slow: bool = False,
    sha: str = SHA,
) -> RepoBundle:
    return RepoBundle(
        repo=repo_meta(),
        ref="master",
        commit_sha=sha,
        tree=make_tree(),
        tree_ref_fallback=fallback,
        tree_truncated_by_github=github_truncated,
        request_count=2,
        timing=TimingInfo(meta_ms=11, commit_ms=22, tree_ms=33, tree_slow=tree_slow),
        commit={
            "sha": sha,
            "message": "initial commit",
            "author": "octocat",
            "date": "2025-01-01T00:00:00Z",
        },
        owner="octocat",
        repo_name="Hello-World",
    )


def install_fakes(
    monkeypatch,
    *,
    bundle: RepoBundle | None = None,
    head_error: Exception | None = None,
    fetch_error: Exception | None = None,
    fetch_calls: list[int] | None = None,
    resolve_calls: list[int] | None = None,
):
    calls = fetch_calls if fetch_calls is not None else [0]
    resolves = resolve_calls if resolve_calls is not None else [0]

    async def fake_resolve(_: str, *, branch: str | None = None) -> RepoHead:
        resolves[0] += 1
        if head_error:
            raise head_error
        return fake_head(bundle.commit_sha if bundle else SHA)

    async def fake_fetch(_: str, head: RepoHead | None = None) -> RepoBundle:
        calls[0] += 1
        if fetch_error:
            raise fetch_error
        assert head is not None
        return bundle or fake_bundle()

    monkeypatch.setattr(main, "resolve_head", fake_resolve)
    monkeypatch.setattr(main, "fetch_repo_bundle", fake_fetch)
    return calls, resolves


client: TestClient | None = None


@pytest.fixture(autouse=True)
def fresh_client():
    """每个用例独立 TestClient，避免上一个用例关闭 portal 后留下已关闭的事件循环。"""
    global client
    client = TestClient(main.app)
    yield
    client.close()


def test_healthz() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_api_health_exposes_version_and_rule_count() -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["engine_version"] == "repogate-3.2"
    assert body["schema_version"] == 1
    assert body["rules_count"] == 14


def test_docs_and_openapi_cover_both_inspect_methods() -> None:
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "RepoGate" in docs.text

    schema = client.get("/openapi.json").json()
    assert "/api/inspect" in schema["paths"]
    assert "get" in schema["paths"]["/api/inspect"]
    assert "post" in schema["paths"]["/api/inspect"]
    assert schema["paths"]["/api/inspect"]["get"]["responses"]["200"]


def test_index_renders_form() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert 'name="repo_url"' in response.text
    assert "RepoGate" in response.text


def test_api_inspect_success_contract(monkeypatch) -> None:
    install_fakes(monkeypatch)

    response = client.post(
        "/api/inspect", json={"repo_url": "https://github.com/octocat/Hello-World"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["ok"] is True
    assert body["provider"] == "github"
    assert body["request_count"] == 2
    assert body["mode"] == "metadata"
    assert body["source"] == {
        "method": "trees_api",
        "tree_ref_fallback": False,
        "tree_truncated_by_github": False,
    }
    assert body["repo"]["full_name"] == "octocat/Hello-World"
    assert body["repo"]["owner"] == "octocat"
    assert body["repo"]["name"] == "Hello-World"
    assert body["repo"]["default_branch"] == "master"
    assert body["repo"]["head_sha"] == SHA
    assert body["repo"]["pushed_at"] == "2025-01-01T00:00:00Z"
    assert body["ref"] == "master"
    assert body["commit"]["sha"] == SHA
    assert body["commit"]["message"] == "initial commit"
    assert body["scan"]["engine_version"] == "repogate-3.2"
    assert body["scan"]["scanned_at"]
    assert body["scan"]["duration_ms"] >= 0
    assert body["scope"]["files_scanned"] == 2
    assert body["scope"]["files_skipped"] == 1
    assert body["stats"]["secret_scan_files"] == 0
    assert body["cache"]["hit"] is False
    assert body["cache"]["stale"] is False
    assert body["cache"]["cached_at"]
    assert body["warnings"] == []
    assert body["error"] is None
    assert body["engine_version"] == "repogate-3.2"
    assert body["scanned_at"] == body["scan"]["scanned_at"]


def test_api_identities_hold(monkeypatch) -> None:
    install_fakes(monkeypatch)
    body = client.post("/api/inspect", json={"repo_url": "https://github.com/a/b"}).json()
    scope, stats = body["scope"], body["stats"]
    assert scope["files_scanned"] + scope["files_skipped"] == stats["listed_blobs"]
    assert stats["listed_blobs"] + stats["unlisted_blobs"] == stats["total_blobs_in_tree"]


def test_api_findings_carry_required_fields(monkeypatch) -> None:
    install_fakes(monkeypatch)
    body = client.post("/api/inspect", json={"repo_url": "https://github.com/a/b"}).json()
    assert len(body["findings"]) == 5
    for finding in body["findings"]:
        assert finding["rule_id"]
        assert finding["evidence_masked"]
        assert finding["severity"] in {"critical", "high", "medium", "low", "info"}
        assert finding["confidence"] in {"high", "medium", "low"}
        assert "file" in finding and "line" in finding
        assert finding["cap_eligible"] in (True, False)


def test_api_checks_cover_every_rule_and_match_findings(monkeypatch) -> None:
    install_fakes(monkeypatch)
    body = client.post("/api/inspect", json={"repo_url": "https://github.com/a/b"}).json()

    checks = body["checks"]
    assert [check["rule_id"] for check in checks] == [
        "R01", "R02", "R03", "R04", "R05", "R06", "R07", "R08",
        "R09", "R10", "R11", "R12", "R13", "R14",
    ]
    assert len(checks) == 14
    for check in checks:
        assert check["status"] in {"pass", "fail", "degraded", "n/a"}
        assert check["basis"].strip()
        assert "file" in check and "line" in check

    by_rule: dict[str, list] = {}
    for finding in body["findings"]:
        by_rule.setdefault(finding["rule_id"], []).append(finding)

    for check in checks:
        items = by_rule.get(check["rule_id"], [])
        if check["status"] == "fail":
            assert any(item["severity"] in ("medium", "high", "critical") for item in items)
        elif check["status"] == "pass":
            assert all(item["severity"] in ("low", "info") for item in items)
        elif check["status"] == "n/a":
            assert not items


def test_basis_contains_actual_deduction_points(monkeypatch) -> None:
    install_fakes(monkeypatch)
    body = client.post("/api/inspect", json={"repo_url": "https://github.com/a/b"}).json()
    checks = {item["rule_id"]: item for item in body["checks"]}

    assert "提示 1 条（-3）" in checks["R09"]["basis"]
    assert "提示 1 条（-3）" in checks["R12"]["basis"]
    assert "提示 1 条（-3）" in checks["R13"]["basis"]


def test_get_inspect_accepts_repo_query(monkeypatch) -> None:
    install_fakes(monkeypatch)
    response = client.get(
        "/api/inspect", params={"repo": "https://github.com/octocat/Hello-World"}
    )
    assert response.status_code == 200
    assert response.json()["commit"]["sha"] == SHA


def test_cache_hit_skips_tree_and_content_and_is_fast(monkeypatch) -> None:
    calls, resolves = install_fakes(monkeypatch)

    first = client.post(
        "/api/inspect", json={"repo_url": "https://github.com/octocat/Hello-World"}
    )
    assert first.status_code == 200
    assert first.json()["cache"]["hit"] is False
    assert calls[0] == 1

    started = time.perf_counter()
    second = client.get(
        "/api/inspect", params={"repo": "https://github.com/octocat/Hello-World"}
    )
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert second.status_code == 200
    second_body = second.json()
    assert second_body["cache"]["hit"] is True
    assert second_body["cache"]["stale"] is False
    assert calls[0] == 1, "缓存命中不得再拉树"
    assert resolves[0] == 2, "每次访问仍解析 HEAD"
    assert elapsed_ms < 300
    assert second_body["score"] == first.json()["score"]
    assert second_body["checks"] == first.json()["checks"]


def test_refresh_forces_rescan_and_keeps_same_score(monkeypatch) -> None:
    calls, _ = install_fakes(monkeypatch)
    url = "https://github.com/octocat/Hello-World"
    first = client.get("/api/inspect", params={"repo": url}).json()
    refresh = client.get("/api/inspect", params={"repo": url, "refresh": 1})
    assert refresh.status_code == 200
    body = refresh.json()
    assert body["cache"]["hit"] is False
    assert calls[0] == 2
    assert body["score"] == first["score"]


def test_same_commit_score_is_byte_stable(monkeypatch) -> None:
    install_fakes(monkeypatch)
    url = "https://github.com/octocat/Hello-World"
    first = client.get("/api/inspect", params={"repo": url}).json()
    second = client.get("/api/inspect", params={"repo": url}).json()
    assert json.dumps(first["score"], ensure_ascii=False, sort_keys=True) == json.dumps(
        second["score"], ensure_ascii=False, sort_keys=True
    )
    assert json.dumps(first["checks"], ensure_ascii=False, sort_keys=True) == json.dumps(
        second["checks"], ensure_ascii=False, sort_keys=True
    )


def test_rate_limited_with_cache_returns_stale_200(monkeypatch) -> None:
    install_fakes(monkeypatch)
    url = "https://github.com/octocat/Hello-World"
    first = client.get("/api/inspect", params={"repo": url})
    assert first.status_code == 200

    async def fake_resolve(_: str, *, branch: str | None = None):
        raise rate_limited("03:00", 321)

    monkeypatch.setattr(main, "resolve_head", fake_resolve)

    stale = client.get("/api/inspect", params={"repo": url})
    assert stale.status_code == 200
    body = stale.json()
    assert body["cache"]["stale"] is True
    assert body["cache"]["hit"] is False
    assert body["commit"]["sha"] == SHA
    assert body["warnings"][0]["code"] == "STALE_CACHE"
    assert "上游限流" in body["warnings"][0]["message"]
    assert body["error"] is None


def test_rate_limited_without_cache_returns_429_with_retry_after(monkeypatch) -> None:
    install_fakes(monkeypatch, head_error=rate_limited("03:00", 321))
    response = client.get(
        "/api/inspect", params={"repo": "https://github.com/octocat/Hello-World"}
    )
    assert response.status_code == 429
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "RATE_LIMITED"
    assert body["error"]["retry_after"] == 321
    assert body["error"]["hint"]


def test_stale_page_shows_warning_without_hiding_report(monkeypatch) -> None:
    install_fakes(monkeypatch)
    url = "https://github.com/octocat/Hello-World"
    assert client.post("/", data={"repo_url": url}).status_code == 200

    async def fake_resolve(_: str, *, branch: str | None = None):
        raise rate_limited("03:00", 321)

    monkeypatch.setattr(main, "resolve_head", fake_resolve)
    page = client.post("/", data={"repo_url": url})
    assert page.status_code == 200
    assert "上游限流" in page.text
    assert 'class="score-ring"' in page.text
    assert "不建议接入" in page.text
    assert "缓存" in page.text and "陈旧" in page.text


def test_no_cache_rate_limited_page_is_not_blank(monkeypatch) -> None:
    install_fakes(monkeypatch, head_error=rate_limited("03:00", 321))
    page = client.post("/", data={"repo_url": "https://github.com/octocat/Hello-World"})
    assert page.status_code == 429
    assert "RATE_LIMITED" in page.text
    assert "retry_after=321" in page.text


def test_page_embedded_json_matches_fixed_api_payload(monkeypatch) -> None:
    install_fakes(monkeypatch)
    fixed = asyncio.run(main._inspect("https://github.com/octocat/Hello-World", refresh=True))

    async def fake_inspect(repo_url: str, *, refresh: bool = False) -> InspectResponse:
        assert refresh is False
        return fixed

    monkeypatch.setattr(main, "_inspect", fake_inspect)
    page = client.post("/", data={"repo_url": "https://github.com/octocat/Hello-World"})
    match = re.search(
        r'<script type="application/json" id="result-json">(.*?)</script>',
        page.text,
        re.S,
    )
    assert match is not None
    page_data = json.loads(match.group(1))
    api_data = client.post(
        "/api/inspect", json={"repo_url": "https://github.com/octocat/Hello-World"}
    ).json()
    assert page_data == api_data


def test_page_shows_slow_banner(monkeypatch) -> None:
    install_fakes(monkeypatch, bundle=fake_bundle(tree_slow=True))
    response = client.post("/", data={"repo_url": "https://github.com/a/b"})
    assert response.status_code == 200
    assert "超大仓库，本次耗时较长" in response.text


def test_api_stats_windows_script_count_matches_r06_basis(monkeypatch) -> None:
    tree = {
        "sha": "root",
        "truncated": False,
        "tree": [
            {"path": "README.md", "type": "blob", "size": 6, "sha": "x"},
            {"path": "tools/a.ps1", "type": "blob", "size": 5, "sha": "x"},
            {"path": "node_modules/b.bat", "type": "blob", "size": 5, "sha": "x"},
        ],
    }
    bundle = dataclasses.replace(fake_bundle(), tree=tree)
    install_fakes(monkeypatch, bundle=bundle)
    body = client.post("/api/inspect", json={"repo_url": "https://github.com/a/b"}).json()
    r06 = next(check for check in body["checks"] if check["rule_id"] == "R06")
    assert body["stats"]["windows_script_count"] == 1
    assert r06["basis"].startswith("共 1 个 Windows 脚本")


def test_api_passes_source_flags(monkeypatch) -> None:
    install_fakes(monkeypatch, bundle=fake_bundle(fallback=True, github_truncated=True))
    response = client.post("/api/inspect", json={"repo_url": "https://github.com/a/b"})
    assert response.status_code == 200
    source = response.json()["source"]
    assert source["tree_ref_fallback"] is True
    assert source["tree_truncated_by_github"] is True


def test_api_inspect_accepts_url_alias(monkeypatch) -> None:
    install_fakes(monkeypatch)
    response = client.post(
        "/api/inspect", json={"url": "https://github.com/octocat/Hello-World"}
    )
    assert response.status_code == 200


def test_api_invalid_host_returns_invalid_url() -> None:
    response = client.post("/api/inspect", json={"repo_url": "https://gitlab.com/owner/repo"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_URL"
    assert "github.com" in response.json()["error"]["message"]


def test_api_not_found_returns_404(monkeypatch) -> None:
    install_fakes(monkeypatch, head_error=not_found())
    response = client.post("/api/inspect", json={"repo_url": "https://github.com/no/such"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REPO_NOT_FOUND"


def test_api_timeout_returns_504(monkeypatch) -> None:
    install_fakes(monkeypatch, head_error=timeout())
    response = client.post("/api/inspect", json={"repo_url": "https://github.com/a/b"})
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "TIMEOUT"


def test_page_submit_renders_result(monkeypatch) -> None:
    install_fakes(monkeypatch)
    response = client.post("/", data={"repo_url": "https://github.com/octocat/Hello-World"})
    assert response.status_code == 200
    assert "octocat/Hello-World" in response.text
    assert 'class="score-ring"' in response.text
    assert "不建议接入" in response.text
    assert "导出 JSON" in response.text
    assert "v1" in response.text


def test_page_submit_renders_chinese_error(monkeypatch) -> None:
    install_fakes(monkeypatch, head_error=not_found())
    response = client.post("/", data={"repo_url": "https://github.com/no/such"})
    assert response.status_code == 404
    assert "REPO_NOT_FOUND" in response.text
    assert "仓库不存在" in response.text


def test_cache_save_failure_leaves_no_half_file(monkeypatch, tmp_path) -> None:
    install_fakes(monkeypatch)

    async def boom(*args, **kwargs):
        raise RuntimeError("content down")

    monkeypatch.setattr(main, "fetch_contents", boom)

    with StarletteTestClient(main.app, raise_server_exceptions=False) as safe_client:
        response = safe_client.post(
            "/api/inspect", json={"repo_url": "https://github.com/octocat/Hello-World"}
        )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL"
    cache_dir = tmp_path / "reports"
    if cache_dir.exists():
        assert not list(cache_dir.rglob("*.json"))
        assert not list(cache_dir.rglob("*.tmp"))
