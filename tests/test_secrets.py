"""R09 / R10 / R11 口径与夹具：入库 .env、私钥、令牌，含掩码与封顶资格。"""

from __future__ import annotations

import json
from pathlib import Path

from app.masking import mask_secret
from app.rules.secrets import (
    detect_env_committed,
    detect_private_key,
    detect_secret_token,
    is_env_file,
    is_env_template,
)
from app.snapshot import BlobRef, Snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "secrets"


def fixture(name: str) -> str:
    # 夹具文件名不能叫 .env，否则公开仓库会真的出现 .env；虚拟路径仍按 .env 测。
    if name == ".env":
        name = "env.fixture"
    return (FIXTURES / name).read_text(encoding="utf-8")


def snapshot(
    paths: dict[str, int],
    contents: dict[str, str] | None = None,
    text_total: int | None = None,
    failed: tuple[str, ...] = (),
) -> Snapshot:
    return Snapshot(
        owner="owner",
        repo="repo",
        commit_sha="a" * 40,
        ref="main",
        blobs=tuple(BlobRef(path=path, size=size) for path, size in paths.items()),
        contents=contents or {},
        failed_paths=frozenset(failed),
        text_total=text_total if text_total is not None else len(paths),
    )


def _findings(outcome) -> list:
    return ([outcome.finding] if outcome.finding else []) + list(outcome.extra_findings)


def test_env_filename_matching_excludes_templates() -> None:
    assert is_env_file(".env")
    assert is_env_file("app/.env.local")
    assert is_env_file(".env.production")
    assert not is_env_file(".env.example")
    assert not is_env_file(".env.sample")
    assert not is_env_file(".env.template")
    assert not is_env_file(".env.dist")
    assert is_env_template(".env.example")


def test_secret_fixtures_matrix() -> None:
    contents = {
        ".env": fixture(".env"),
        "id_rsa": fixture("id_rsa"),
        "config.py": fixture("config.py"),
        "docs/example.md": fixture("docs/example.md"),
        "tests/test_dummy.py": fixture("test_dummy.py"),
    }
    paths = {path: len(text) for path, text in contents.items()}
    snap = snapshot(paths, contents)

    r09 = detect_env_committed(snap)
    r10 = detect_private_key(snap)
    r11 = detect_secret_token(snap)

    findings = _findings(r09) + _findings(r10) + _findings(r11)
    critical_files = {item.file for item in findings if item.severity == "critical"}
    assert critical_files == {".env", "id_rsa", "config.py"}
    assert len(critical_files) == 3
    assert all(item.cap_eligible for item in findings if item.severity == "critical")

    docs = [item for item in findings if item.file == "docs/example.md"]
    tests = [item for item in findings if item.file == "tests/test_dummy.py"]
    assert docs and all(item.severity == "info" and not item.cap_eligible for item in docs)
    assert tests and all(item.severity == "low" and not item.cap_eligible for item in tests)

    assert r09.status == "fail" and r09.finding is not None
    assert r09.finding.severity == "critical"
    assert "OPENAI_API_KEY=***" in r09.finding.evidence_masked

    dump = json.dumps([item.model_dump() for item in findings], ensure_ascii=False)
    for raw in ("sk-real-looking-32chars", "abcdefghijklmnopqrstuvwxyz012345", "S3cretPass"):
        assert raw not in dump


def test_r09_env_in_testdata_is_low_not_capped() -> None:
    text = "DB_PASSWORD=RealLookingPass\nDB_PORT=5432\n"
    outcome = detect_env_committed(
        snapshot({"testdata/archives/files/.env.prod": 40}, {"testdata/archives/files/.env.prod": text})
    )

    assert outcome.status == "pass"
    assert outcome.finding is None
    assert outcome.extra_findings[0].severity == "low"
    assert outcome.extra_findings[0].cap_eligible is False


def test_r09_non_secret_values_do_not_trigger_critical() -> None:
    text = 'PROJECT_NAME="Real Project"\nFASTAPI_ENV=production\nSECRET_KEY=changethis\n'
    outcome = detect_env_committed(snapshot({".env": 40}, {".env": text}))

    assert outcome.status == "fail"
    assert outcome.finding is not None
    assert outcome.finding.severity == "medium"
    assert outcome.finding.cap_eligible is False


def test_r09_all_placeholder_is_medium_without_cap() -> None:
    text = "API_KEY=changeme\nEMPTY=\nTOKEN=xxx\n"
    outcome = detect_env_committed(snapshot({".env": 30}, {".env": text}))

    assert outcome.status == "fail"
    assert outcome.finding is not None
    assert outcome.finding.severity == "medium"
    assert outcome.finding.cap_eligible is False
    assert "均为空值或占位值" in outcome.finding.evidence_masked


def test_r09_gitignore_covered_passes() -> None:
    outcome = detect_env_committed(
        snapshot({".gitignore": 20, "README.md": 10}, {".gitignore": ".env\n.env.*\n"})
    )

    assert outcome.status == "pass"
    assert "已包含 .env" in outcome.basis
    assert outcome.finding is None


def test_r09_gitignore_missing_is_pass_with_hint() -> None:
    outcome = detect_env_committed(snapshot({"README.md": 10}, {}, text_total=1))

    assert outcome.status == "pass"
    assert outcome.finding is None
    assert outcome.extra_findings[0].severity == "low"
    assert "仅提示 1 条" in outcome.basis


def test_r09_missing_content_degrades() -> None:
    outcome = detect_env_committed(snapshot({".env": 30}, {}, text_total=1))

    assert outcome.status == "degraded"
    assert "不可读" in outcome.basis


def test_r10_extension_without_content_is_high_not_capped() -> None:
    outcome = detect_private_key(snapshot({"cert.pem": 10, "README.md": 10}, {}, text_total=10))

    findings = _findings(outcome)
    assert outcome.status == "fail"
    assert findings[0].severity == "high"
    assert findings[0].cap_eligible is False
    assert "未构成全仓结论" in findings[0].impact


def test_r11_docs_info_and_tests_low() -> None:
    token = "sk-abcdefghijklmnopqrst"
    contents = {"docs/usage.md": f"key={token}", "tests/test_x.py": f"key={token}"}
    outcome = detect_secret_token(snapshot({path: 50 for path in contents}, contents))

    findings = _findings(outcome)
    assert outcome.status == "pass"
    assert {item.file for item in findings} == {"docs/usage.md", "tests/test_x.py"}
    severities = {item.file: item.severity for item in findings}
    assert severities["docs/usage.md"] == "info"
    assert severities["tests/test_x.py"] == "low"


def test_r11_low_sampling_outside_high_risk_is_high_without_cap() -> None:
    text = 'TOKEN = "sk-abcdefghijklmnopqrst"\n'
    outcome = detect_secret_token(
        snapshot({"src/app.py": 50}, {"src/app.py": text}, text_total=100)
    )

    assert outcome.status == "fail"
    assert outcome.finding is not None
    assert outcome.finding.severity == "high"
    assert outcome.finding.cap_eligible is False
    assert "未构成全仓结论" in outcome.finding.impact


def test_r11_placeholder_token_is_low() -> None:
    text = 'TOKEN = "sk-xxxxxxxxxxxxxxxxxxxx"\n'
    outcome = detect_secret_token(snapshot({"app.py": 40}, {"app.py": text}))

    assert outcome.status == "pass"
    assert outcome.extra_findings[0].severity == "low"


def test_r09_keyword_table_boundary_and_api_key() -> None:
    text = "AUTHOR=real-person\nAPI_KEY=sk-real-looking-32chars\nPRIVATE_STUFF=abc\n"
    outcome = detect_env_committed(snapshot({".env": 80}, {".env": text}))

    assert outcome.status == "fail"
    assert outcome.finding is not None
    assert outcome.finding.severity == "critical"
    assert "API_KEY=***" in outcome.finding.evidence_masked
    assert "PRIVATE_STUFF=***" in outcome.finding.evidence_masked
    assert "AUTHOR" not in outcome.finding.evidence_masked


def test_r11_rule_definition_paths_are_info() -> None:
    token = "sk-abcdefghijklmnopqrst"
    contents = {
        "cmd/generate/config/rules/anthropic.go": f'{{"key": "{token}"}}',
        "config/gitleaks.toml": f'regex = "{token}"',
    }
    outcome = detect_secret_token(snapshot({path: 50 for path in contents}, contents))

    findings = _findings(outcome)
    assert outcome.status == "pass"
    assert {item.severity for item in findings} == {"info"}


def test_r10_private_key_at_low_sampling_is_high_not_capped() -> None:
    text = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n"
    outcome = detect_private_key(snapshot({"id_rsa": 80}, {"id_rsa": text}, text_total=100))

    assert outcome.status == "fail"
    assert outcome.finding is not None
    assert outcome.finding.severity == "high"
    assert outcome.finding.cap_eligible is False
    assert "未构成全仓结论" in outcome.finding.impact


def test_mask_secret_format() -> None:
    assert mask_secret("sk-abcdefghij") == "sk-a***ij"
    assert mask_secret("short") == "***"
    assert mask_secret("12345678") == "1234***78"
