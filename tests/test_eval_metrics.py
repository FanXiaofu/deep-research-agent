"""F1：评测分位数与单变量覆盖参数。"""
import sys
from pathlib import Path

import pytest

import config

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))
from run_eval import apply_overrides, config_snapshot, percentile, percentile_snapshot  # noqa: E402


def test_percentile_interpolates():
    assert percentile([10, 20, 30, 40], 0.5) == 25
    assert percentile([10, 20, 30, 40], 0.95) == 38.5
    assert percentile([42], 0.5) == 42
    assert percentile([], 0.5) is None


def test_percentile_snapshot_skips_errors():
    rows = [
        {"elapsed_s": 10, "cost_usd_est": 0.01, "input_tokens": 100, "output_tokens": 10},
        {"elapsed_s": 20, "cost_usd_est": 0.02, "input_tokens": 200, "output_tokens": 20},
        {"error": "boom", "elapsed_s": 999, "cost_usd_est": 99},
    ]

    pct = percentile_snapshot(rows)

    assert pct["elapsed_s_p50"] == 15
    assert pct["elapsed_s_p95"] == 19.5
    assert pct["input_tokens_p50"] == 150


def test_apply_overrides_parses_types(monkeypatch):
    monkeypatch.setattr(config, "NOTE_MIN_CHARS", 200)
    monkeypatch.setattr(config, "WRITER_STRICT_GROUNDING", False)
    monkeypatch.setattr(config, "LLM_PRICE_INPUT_PER_M", 0.27)
    monkeypatch.setattr(config, "JUDGE_MODEL", "")

    applied = apply_overrides([
        "NOTE_MIN_CHARS=300",
        "WRITER_STRICT_GROUNDING=1",
        "LLM_PRICE_INPUT_PER_M=0.5",
        "JUDGE_MODEL=strong-judge",
    ])

    assert config.NOTE_MIN_CHARS == 300
    assert config.WRITER_STRICT_GROUNDING is True
    assert config.LLM_PRICE_INPUT_PER_M == 0.5
    assert config.JUDGE_MODEL == "strong-judge"
    assert applied["NOTE_MIN_CHARS"] == 300


def test_apply_overrides_rejects_bad_input(monkeypatch):
    with pytest.raises(ValueError):
        apply_overrides(["NO_EQUALS"])
    with pytest.raises(ValueError):
        apply_overrides(["NOT_A_CONFIG=1"])
    monkeypatch.setattr(config, "NOTE_MIN_CHARS", 200)
    with pytest.raises(ValueError):
        apply_overrides(["NOTE_MIN_CHARS=not-int"])


def test_config_snapshot_records_judge_and_overrides():
    snapshot = config_snapshot(2, 1, overrides={"NOTE_MIN_CHARS": 300})

    assert snapshot["judge_model"]
    assert snapshot["overrides"] == {"NOTE_MIN_CHARS": 300}
