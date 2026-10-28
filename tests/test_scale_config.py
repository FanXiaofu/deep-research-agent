"""规模参数的边界校验与 per-run 覆盖兜底（自定义子问题上限 / 检索轮数）。"""
import pytest

import config
from graph.state import effective_max_search_rounds, effective_max_subquestions


# ---------- 边界校验 ----------

def test_validate_scale_defaults_to_config():
    assert config.validate_scale() == (config.MAX_SUBQUESTIONS, config.MAX_SEARCH_ROUNDS)


def test_validate_scale_accepts_custom_within_range():
    assert config.validate_scale(6, 4) == (6, 4)


def test_validate_scale_fills_partial_override_from_config():
    assert config.validate_scale(6, None) == (6, config.MAX_SEARCH_ROUNDS)


def test_validate_scale_rejects_zero():
    """0 曾被 `if max_sub:` 当 falsy 静默忽略；现在必须显式报错。"""
    with pytest.raises(ValueError, match="子问题上限"):
        config.validate_scale(0, 1)
    with pytest.raises(ValueError, match="检索轮数"):
        config.validate_scale(1, 0)


def test_validate_scale_rejects_negative_and_over_limit():
    with pytest.raises(ValueError):
        config.validate_scale(-1, 1)
    with pytest.raises(ValueError):
        config.validate_scale(1, -1)
    with pytest.raises(ValueError):
        config.validate_scale(config.MAX_SUBQUESTIONS_LIMIT + 1, 1)
    with pytest.raises(ValueError):
        config.validate_scale(1, config.MAX_SEARCH_ROUNDS_LIMIT + 1)


# ---------- per-run 覆盖 ----------

def test_effective_scale_falls_back_to_config():
    assert effective_max_subquestions({}) == config.MAX_SUBQUESTIONS
    assert effective_max_search_rounds({}) == config.MAX_SEARCH_ROUNDS


def test_effective_scale_prefers_state_override():
    state = {"max_subquestions": 7, "max_search_rounds": 4}
    assert effective_max_subquestions(state) == 7
    assert effective_max_search_rounds(state) == 4


def test_effective_scale_does_not_mutate_global_config():
    """覆盖值必须留在 state 里 —— 写回全局 config 会让同进程并发请求互相覆盖。"""
    before = (config.MAX_SUBQUESTIONS, config.MAX_SEARCH_ROUNDS)

    effective_max_subquestions({"max_subquestions": config.MAX_SUBQUESTIONS_LIMIT})
    effective_max_search_rounds({"max_search_rounds": config.MAX_SEARCH_ROUNDS_LIMIT})

    assert (config.MAX_SUBQUESTIONS, config.MAX_SEARCH_ROUNDS) == before
