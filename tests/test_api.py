from __future__ import annotations

from fastapi.testclient import TestClient
from starlette.testclient import TestClient as StarletteTestClient

from app import main
from app.errors import not_found, rate_limited, timeout
from app.github import RepoBundle
from app.models import TimingInfo


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


def fake_bundle(fallback: bool = False, github_truncated: bool = False) -> RepoBundle:
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
        timing=TimingInfo(meta_ms=11, commit_ms=22, tree_ms=33),
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
    assert body["scope"]["files_scanned"] == 3
    assert body["scope"]["files_skipped"] == 0
    assert body["scope"]["truncated"] is False
    assert body["scope"]["truncate_reason"] == "none"
    assert body["stats"] == {
        "text_files": 3,
        "windows_script_count": 0,
        "total_blobs_in_tree": 3,
        "listed_blobs": 3,
    }
    assert body["timing"] == {"meta_ms": 11, "commit_ms": 22, "tree_ms": 33}
    assert [item["path"] for item in body["files"]] == ["README.md", "src/main.py"]


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
    assert "src/main.py" in response.text


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
