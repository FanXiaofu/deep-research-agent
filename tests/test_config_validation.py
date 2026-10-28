"""启动自检：配置错误必须在启动时暴露，而不是退化成空报告。"""
import pytest

import config


def _local_ok(monkeypatch):
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setattr(config, "LLM_MODEL", "qwen2.5:7b-instruct")
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.setattr(config, "LLM_NATIVE_OLLAMA", True)
    monkeypatch.setattr(config, "SEARCH_PROVIDER", "duckduckgo")
    monkeypatch.setattr(config, "SEARCH_API_KEY", "")


def test_local_ollama_allows_empty_key(monkeypatch):
    _local_ok(monkeypatch)
    assert config.runtime_config_errors() == []


def test_remote_llm_requires_api_key(monkeypatch):
    _local_ok(monkeypatch)
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setattr(config, "LLM_NATIVE_OLLAMA", False)

    errors = config.runtime_config_errors()

    assert any("LLM_API_KEY" in e for e in errors)


def test_tavily_requires_search_key(monkeypatch):
    _local_ok(monkeypatch)
    monkeypatch.setattr(config, "SEARCH_PROVIDER", "tavily")
    monkeypatch.setattr(config, "SEARCH_API_KEY", "")

    errors = config.runtime_config_errors()

    assert any("SEARCH_API_KEY" in e for e in errors)


def test_unknown_search_provider_is_rejected(monkeypatch):
    _local_ok(monkeypatch)
    monkeypatch.setattr(config, "SEARCH_PROVIDER", "bing")

    errors = config.runtime_config_errors()

    assert any("SEARCH_PROVIDER" in e for e in errors)


def test_non_positive_budget_is_rejected(monkeypatch):
    _local_ok(monkeypatch)
    monkeypatch.setattr(config, "MAX_LLM_CALLS", 0)

    errors = config.runtime_config_errors()

    assert any("MAX_LLM_CALLS" in e for e in errors)


def test_default_scale_above_limit_is_rejected(monkeypatch):
    _local_ok(monkeypatch)
    monkeypatch.setattr(config, "MAX_SUBQUESTIONS", 99)
    monkeypatch.setattr(config, "MAX_SUBQUESTIONS_LIMIT", 8)

    errors = config.runtime_config_errors()

    assert any("MAX_SUBQUESTIONS" in e for e in errors)


def test_validate_runtime_config_raises_with_all_errors(monkeypatch):
    _local_ok(monkeypatch)
    monkeypatch.setattr(config, "LLM_BASE_URL", "")
    monkeypatch.setattr(config, "LLM_MODEL", "")

    with pytest.raises(RuntimeError, match="配置校验失败"):
        config.validate_runtime_config()
