"""全局预算的判定逻辑：四个维度各自能触发、剩余额度不为负、原因字符串可读。"""
import config
from budget import BUDGET
from llm.client import USAGE


def setup_function(_func):
    USAGE.reset()
    BUDGET.reset()


def test_healthy_budget_returns_empty_reason():
    assert BUDGET.exceeded() == ""


def test_llm_call_limit_triggers(monkeypatch):
    monkeypatch.setattr(config, "MAX_LLM_CALLS", 2)
    USAGE.add(10, 10)
    USAGE.add(10, 10)
    assert BUDGET.exceeded().startswith("llm_calls=")


def test_token_limit_triggers(monkeypatch):
    monkeypatch.setattr(config, "MAX_TOTAL_TOKENS", 100)
    USAGE.add(60, 50)
    assert BUDGET.exceeded().startswith("total_tokens=")


def test_cost_limit_triggers(monkeypatch):
    monkeypatch.setattr(config, "MAX_BUDGET_USD", 0.0001)
    USAGE.add(1000, 1000)
    assert BUDGET.exceeded().startswith("cost_usd=")


def test_wall_clock_limit_triggers(monkeypatch):
    monkeypatch.setattr(config, "MAX_WALL_CLOCK_S", 0.0)
    assert BUDGET.exceeded().startswith("wall_clock=")


def test_remaining_never_negative(monkeypatch):
    monkeypatch.setattr(config, "MAX_LLM_CALLS", 1)
    monkeypatch.setattr(config, "MAX_TOTAL_TOKENS", 10)
    USAGE.add(100, 100)
    remaining = BUDGET.remaining()
    assert remaining["llm_calls_left"] == 0
    assert remaining["tokens_left"] == 0
    assert remaining["usd_left"] >= 0
