"""六仓 summary golden：文件存在且显式联网时运行；默认不参与 CI。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from app.main import _inspect

GOLDEN = Path(__file__).parent / "golden" / "six_repos.json"
RUN = os.environ.get("REPOGATE_GOLDEN_NETWORK") == "1"

pytestmark = pytest.mark.skipif(
    not GOLDEN.exists(),
    reason="tests/golden/six_repos.json 未生成；先手动运行 scripts/regen_golden.py",
)


@pytest.mark.skipif(not RUN, reason="需要 REPOGATE_GOLDEN_NETWORK=1 才联网跑六仓")
def test_six_repo_summary_golden() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert golden["schema_version"] == 1

    async def run_all() -> None:
        for expected in golden["repos"]:
            url = f"https://github.com/{expected['repo']}"
            result = await _inspect(url, refresh=True)
            checks = {check.rule_id: check.status for check in result.checks}
            counts = {
                rule_id: sum(1 for item in result.findings if item.rule_id == rule_id)
                for rule_id in checks
            }
            assert result.commit.sha == expected["commit"]
            assert result.score.total == expected["score"]["total"]
            assert result.score.grade == expected["score"]["grade"]
            assert checks == expected["checks"]
            assert counts == expected["finding_counts"]

    asyncio.run(run_all())
