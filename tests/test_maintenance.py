from __future__ import annotations

import datetime as dt

from app.rules.maintenance import detect_archived, detect_license_missing, detect_stale_repo
from app.snapshot import BlobRef, Snapshot


def snapshot(paths: dict[str, int] | None = None, **kwargs) -> Snapshot:
    return Snapshot(
        owner="owner",
        repo="repo",
        commit_sha="a" * 40,
        ref="main",
        blobs=tuple(BlobRef(path=path, size=size) for path, size in (paths or {}).items()),
        **kwargs,
    )


def test_r12_passes_with_root_license() -> None:
    outcome = detect_license_missing(snapshot({"LICENSE": 10}))
    assert outcome.status == "pass"
    assert outcome.finding is None


def test_r12_missing_metadata_is_na() -> None:
    outcome = detect_license_missing(snapshot({"README.md": 10}))
    assert outcome.status == "n/a"
    assert outcome.finding is None
    assert "元信息" in outcome.basis


def test_r12_missing_file_with_known_license_is_low_hint() -> None:
    outcome = detect_license_missing(snapshot({"README.md": 10}, license="MIT"))
    assert outcome.status == "pass"
    assert outcome.extra_findings
    assert outcome.extra_findings[0].severity == "low"
    assert "LICENSE" in outcome.basis


def test_r13_flags_older_than_12_months() -> None:
    now = dt.datetime(2026, 11, 27, tzinfo=dt.timezone.utc)
    outcome = detect_stale_repo(
        snapshot(pushed_at="2024-01-01T00:00:00Z", scanned_at=now)
    )
    assert outcome.status == "pass"
    assert outcome.extra_findings
    assert outcome.extra_findings[0].severity == "low"


def test_r13_recent_passes() -> None:
    now = dt.datetime(2026, 11, 27, tzinfo=dt.timezone.utc)
    outcome = detect_stale_repo(
        snapshot(pushed_at="2026-10-01T00:00:00Z", scanned_at=now)
    )
    assert outcome.status == "pass"
    assert outcome.finding is None


def test_r13_missing_is_na() -> None:
    outcome = detect_stale_repo(snapshot(pushed_at=None))
    assert outcome.status == "n/a"
    assert "pushed_at" in outcome.basis


def test_r14_archived_is_low_hint() -> None:
    outcome = detect_archived(snapshot(archived=True))
    assert outcome.status == "pass"
    assert outcome.extra_findings
    assert outcome.extra_findings[0].severity == "low"


def test_r14_not_archived_passes() -> None:
    outcome = detect_archived(snapshot(archived=False))
    assert outcome.status == "pass"
    assert outcome.finding is None
