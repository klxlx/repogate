from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import TestClient as StarletteTestClient

from app import main
from app.content import ContentResult
from app.errors import not_found, rate_limited, timeout
from app.github import RepoBundle
from app.models import TimingInfo


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


def fake_bundle(fallback: bool = False, github_truncated: bool = False, tree_slow: bool = False) -> RepoBundle:
    return RepoBundle(
        repo={
            "full_name": "octocat/Hello-World",
            "stars": 4054,
            "language": "Python",
            "license": "MIT",
            "archived": False,
            "pushed_at": "2025-01-01T00:00:00Z",
        },
        ref="master",
        commit_sha="a" * 40,
        tree=make_tree(),
        tree_ref_fallback=fallback,
        tree_truncated_by_github=github_truncated,
        request_count=3,
        timing=TimingInfo(meta_ms=11, commit_ms=22, tree_ms=33, tree_slow=tree_slow),
    )


client = TestClient(main.app)


def test_healthz() -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_index_renders_form() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert 'name="repo_url"' in response.text
    assert "RepoGate" in response.text


def test_api_inspect_success(monkeypatch) -> None:
    async def fake_fetch(_: str) -> RepoBundle:
        return fake_bundle()

    monkeypatch.setattr(main, "fetch_repo_bundle", fake_fetch)

    response = client.post(
        "/api/inspect", json={"repo_url": "https://github.com/octocat/Hello-World"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["provider"] == "github"
    assert body["request_count"] == 3
    assert body["mode"] == "metadata"
    assert body["source"] == {
        "method": "trees_api",
        "tree_ref_fallback": False,
        "tree_truncated_by_github": False,
    }
    assert body["repo"]["full_name"] == "octocat/Hello-World"
    assert body["ref"] == "master"
    assert len(body["commit_sha"]) == 40
    assert body["scope"]["files_scanned"] == 2
    assert body["scope"]["files_skipped"] == 1
    assert body["scope"]["bytes_scanned"] == 15
    assert body["scope"]["truncated"] is False
    assert body["scope"]["truncate_reason"] == "none"
    assert body["stats"] == {
        "text_files": 2,
        "windows_script_count": 0,
        "total_blobs_in_tree": 3,
        "listed_blobs": 3,
        "bytes_total_listed": 20,
        "unlisted_blobs": 0,
        "content_requests": 0,
        "content_bytes": 0,
        "content_failed": 0,
        "content_files_available": 0,
        "path_scan_files": 0,
        "path_scan_bytes": 0,
        "content_total_text_files": 2,
        "secret_scan_files": 0,
        "secret_scan_bytes": 0,
        "secret_sampling_ratio": 0.0,
    }
    assert body["timing"] == {
        "meta_ms": 11,
        "commit_ms": 22,
        "tree_ms": 33,
        "tree_slow": False,
        "tree_attempts": 1,
    }
    assert [item["path"] for item in body["files"]] == ["README.md", "src/main.py"]


def test_api_identities_hold(monkeypatch) -> None:
    async def fake_fetch(_: str) -> RepoBundle:
        return fake_bundle()

    monkeypatch.setattr(main, "fetch_repo_bundle", fake_fetch)

    body = client.post("/api/inspect", json={"repo_url": "https://github.com/a/b"}).json()
    scope, stats = body["scope"], body["stats"]
    assert scope["files_scanned"] + scope["files_skipped"] == stats["listed_blobs"]
    assert stats["listed_blobs"] + stats["unlisted_blobs"] == stats["total_blobs_in_tree"]


def test_page_shows_slow_banner(monkeypatch) -> None:
    async def fake_fetch(_: str) -> RepoBundle:
        return fake_bundle(tree_slow=True)

    monkeypatch.setattr(main, "fetch_repo_bundle", fake_fetch)

    response = client.post("/", data={"repo_url": "https://github.com/a/b"})

    assert response.status_code == 200
    assert "超大仓库，本次耗时较长" in response.text


def test_api_rules_registry() -> None:
    response = client.get("/api/rules")

    assert response.status_code == 200
    rules = response.json()["rules"]
    assert [rule["id"] for rule in rules] == [
        "R01", "R02", "R03", "R04", "R05", "R06", "R07", "R08",
        "R09", "R10", "R11", "R12", "R13", "R14",
    ]
    assert len(rules) == 14
    for rule in rules:
        assert set(rule) == {"id", "dimension", "severity", "points", "needs_content"}
    assert rules[0]["points"] == -10
    assert rules[0]["needs_content"] is False
    assert rules[1]["needs_content"] is True


def test_api_findings_carry_required_fields(monkeypatch) -> None:
    async def fake_fetch(_: str) -> RepoBundle:
        return fake_bundle()

    monkeypatch.setattr(main, "fetch_repo_bundle", fake_fetch)

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
    async def fake_fetch(_: str) -> RepoBundle:
        return fake_bundle()

    monkeypatch.setattr(main, "fetch_repo_bundle", fake_fetch)

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


def test_page_embedded_json_matches_api(monkeypatch) -> None:
    import json
    import re

    async def fake_fetch(_: str) -> RepoBundle:
        return fake_bundle()

    monkeypatch.setattr(main, "fetch_repo_bundle", fake_fetch)

    page = client.post("/", data={"repo_url": "https://github.com/a/b"})
    match = re.search(
        r'<script type="application/json" id="result-json">(.*?)</script>',
        page.text,
        re.S,
    )
    assert match is not None
    page_data = json.loads(match.group(1))

    api_data = client.post("/api/inspect", json={"repo_url": "https://github.com/a/b"}).json()

    assert set(page_data) == set(api_data)
    for key in page_data:
        if key == "scanned_at":
            continue
        assert page_data[key] == api_data[key], key


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

    async def fake_fetch(_: str) -> RepoBundle:
        return bundle

    monkeypatch.setattr(main, "fetch_repo_bundle", fake_fetch)

    body = client.post("/api/inspect", json={"repo_url": "https://github.com/a/b"}).json()

    r06 = next(check for check in body["checks"] if check["rule_id"] == "R06")
    assert body["stats"]["windows_script_count"] == 1
    assert r06["basis"].startswith("共 1 个 Windows 脚本")


def test_api_passes_source_flags(monkeypatch) -> None:
    async def fake_fetch(_: str) -> RepoBundle:
        return fake_bundle(fallback=True, github_truncated=True)

    monkeypatch.setattr(main, "fetch_repo_bundle", fake_fetch)

    response = client.post("/api/inspect", json={"repo_url": "https://github.com/a/b"})

    assert response.status_code == 200
    source = response.json()["source"]
    assert source["tree_ref_fallback"] is True
    assert source["tree_truncated_by_github"] is True


def test_api_inspect_accepts_url_alias(monkeypatch) -> None:
    async def fake_fetch(_: str) -> RepoBundle:
        return fake_bundle()

    monkeypatch.setattr(main, "fetch_repo_bundle", fake_fetch)

    response = client.post("/api/inspect", json={"url": "https://github.com/octocat/Hello-World"})

    assert response.status_code == 200


def test_api_invalid_host_returns_invalid_url() -> None:
    response = client.post("/api/inspect", json={"repo_url": "https://gitlab.com/owner/repo"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_URL"
    assert "github.com" in response.json()["error"]["message"]


def test_api_not_found_returns_404(monkeypatch) -> None:
    async def fake_fetch(_: str) -> RepoBundle:
        raise not_found()

    monkeypatch.setattr(main, "fetch_repo_bundle", fake_fetch)

    response = client.post("/api/inspect", json={"repo_url": "https://github.com/no/such"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_api_rate_limited_returns_429(monkeypatch) -> None:
    async def fake_fetch(_: str) -> RepoBundle:
        raise rate_limited()

    monkeypatch.setattr(main, "fetch_repo_bundle", fake_fetch)

    response = client.post("/api/inspect", json={"repo_url": "https://github.com/a/b"})

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"


def test_api_timeout_returns_504(monkeypatch) -> None:
    async def fake_fetch(_: str) -> RepoBundle:
        raise timeout()

    monkeypatch.setattr(main, "fetch_repo_bundle", fake_fetch)

    response = client.post("/api/inspect", json={"repo_url": "https://github.com/a/b"})

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "TIMEOUT"


def test_page_submit_renders_result(monkeypatch) -> None:
    async def fake_fetch(_: str) -> RepoBundle:
        return fake_bundle()

    monkeypatch.setattr(main, "fetch_repo_bundle", fake_fetch)

    response = client.post("/", data={"repo_url": "https://github.com/octocat/Hello-World"})

    assert response.status_code == 200
    assert "octocat/Hello-World" in response.text
    assert 'class="score-ring"' in response.text
    assert "不建议接入" in response.text
    assert "导出 JSON" in response.text


def test_page_submit_renders_chinese_error(monkeypatch) -> None:
    async def fake_fetch(_: str) -> RepoBundle:
        raise not_found()

    monkeypatch.setattr(main, "fetch_repo_bundle", fake_fetch)

    response = client.post("/", data={"repo_url": "https://github.com/no/such"})

    assert response.status_code == 404
    assert "NOT_FOUND" in response.text
    assert "仓库不存在" in response.text


def test_unhandled_error_does_not_500(monkeypatch) -> None:
    async def fake_fetch(_: str) -> RepoBundle:
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "fetch_repo_bundle", fake_fetch)

    safe_client = StarletteTestClient(main.app, raise_server_exceptions=False)
    response = safe_client.post("/api/inspect", json={"repo_url": "https://github.com/a/b"})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
