from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from app import main


def test_env_file_points_to_project_root() -> None:
    """README 要求把 .env 放在项目根目录，加载路径必须与文档一致。"""
    assert main.ENV_FILE == Path(main.BASE_DIR).parent / ".env"
    assert main.ENV_FILE.name == ".env"


def test_dotenv_reads_model_config(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("LLM_MODEL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_MODEL=test-model\n", encoding="utf-8")
    assert load_dotenv(env_file, override=False) is True
    assert os.environ["LLM_MODEL"] == "test-model"
