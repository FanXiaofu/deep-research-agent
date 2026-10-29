"""Command Code headless judge adapter（不调用真实 CLI）。"""
import json

import pytest

import config
from eval import commandcode_judge as cc


class _Result:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_extract_json_from_fence():
    assert cc._extract_json('```json\n{"score": 5}\n```') == {"score": 5}
    with pytest.raises(ValueError):
        cc._extract_json("not json")


def test_judge_json_parses_result(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs["input"]
        return _Result(stdout=json.dumps({
            "type": "result",
            "finalText": '{"score": 4, "unsupported_claims": []}',
        }) + "\n")

    monkeypatch.setattr(config, "JUDGE_EFFORT", "high")
    monkeypatch.setattr(cc, "_cli", lambda: "cmdc")
    monkeypatch.setattr(cc.subprocess, "run", fake_run)
    data = cc.judge_json("评分 prompt", model="deepseek/deepseek-v4-pro")
    assert data["score"] == 4
    assert seen["input"] == "评分 prompt"
    assert "deepseek/deepseek-v4-pro" in seen["cmd"]


def test_judge_json_raises_on_cli_failure(monkeypatch):
    monkeypatch.setattr(cc, "_cli", lambda: "cmdc")
    monkeypatch.setattr(
        cc.subprocess,
        "run",
        lambda *args, **kwargs: _Result(stderr="auth failed", returncode=3),
    )
    with pytest.raises(RuntimeError, match="cmdc judge failed"):
        cc.judge_json("prompt")
