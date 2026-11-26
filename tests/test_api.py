from __future__ import annotations

import io
import zipfile

from fastapi.testclient import TestClient
from starlette.testclient import TestClient as StarletteTestClient

from app import main
from app.errors import not_found, rate_limited, timeout
from app.github import RepoBundle


def make_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("octocat-Hello-World-abc123/README.md", b"hello\n")
        archive.writestr("octocat-Hello-World-abc123/node_modules/x.js", b"skip\n")
        archive.writestr("octocat-Hello-World-abc123/src/main.py", b"print(1)\n")
    return buffer.getvalue()


def fake_bundle() -> RepoBundle:
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
        zip_bytes=make_zip(),
        truncated=False,
        truncate_reason=None,
        requests_used=3,
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
    assert body["repo"]["full_name"] == "octocat/Hello-World"
    assert body["ref"] == "master"
    assert len(body["commit_sha"]) == 40
    assert body["scope"]["files_scanned"] == 2
    assert body["scope"]["files_skipped"] == 1
    assert body["scope"]["truncated"] is False
    assert [item["path"] for item in body["files"]] == ["README.md", "src/main.py"]


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
