"""P1.5：<NAME>_FILE secret 文件优先于环境变量。"""
import pytest

import config


def test_secret_file_takes_precedence(tmp_path, monkeypatch):
    path = tmp_path / "llm.key"
    path.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("DEMO_API_KEY", "from-env")
    monkeypatch.setenv("DEMO_API_KEY_FILE", str(path))
    assert config._secret("DEMO_API_KEY") == "from-file"

    monkeypatch.delenv("DEMO_API_KEY_FILE")
    assert config._secret("DEMO_API_KEY") == "from-env"


def test_missing_secret_file_fails_fast(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_API_KEY_FILE", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError):
        config._secret("DEMO_API_KEY")
