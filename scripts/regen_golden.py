"""手动刷新六仓 summary golden；不放进默认 CI / pytest 路径。

用法：
    .venv\\Scripts\\python.exe scripts/regen_golden.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.main import _inspect  # noqa: E402
from app.rules import RULES  # noqa: E402
from app.version import SCHEMA_VERSION  # noqa: E402

REPOS = (
    "https://github.com/octocat/Hello-World",
    "https://github.com/psf/requests",
    "https://github.com/microsoft/PowerToys",
    "https://github.com/microsoft/vscode",
    "https://github.com/gitleaks/gitleaks",
    "https://github.com/fastapi/full-stack-fastapi-template",
)
OUT = PROJECT_ROOT / "tests" / "golden" / "six_repos.json"


def stable_summary(result) -> dict:
    counts = {
        rule.id: sum(1 for finding in result.findings if finding.rule_id == rule.id)
        for rule in RULES
    }
    return {
        "repo": result.repo.full_name,
        "commit": result.commit.sha,
        "score": {"total": result.score.total, "grade": result.score.grade},
        "checks": {check.rule_id: check.status for check in result.checks},
        "finding_counts": counts,
    }


async def collect() -> list[dict]:
    summaries: list[dict] = []
    for url in REPOS:
        print(f"scanning {url} ...", flush=True)
        result = await _inspect(url, refresh=True)
        summary = stable_summary(result)
        print(
            f"  {summary['repo']} @ {summary['commit'][:7]} "
            f"{summary['score']['total']} {summary['score']['grade']}",
            flush=True,
        )
        summaries.append(summary)
    return summaries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    summaries = asyncio.run(collect())
    payload = {"schema_version": SCHEMA_VERSION, "repos": summaries}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
