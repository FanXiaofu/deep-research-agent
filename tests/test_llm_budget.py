"""R3：LLM 调用的 timeout/重试受剩余墙钟预算约束。"""
import time
from types import SimpleNamespace

import pytest

import config
import llm.client as client
import run_context


class _FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        )


class _FakeClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=_FakeCompletions())


def _raw_chat():
    return getattr(client.chat, "__wrapped__", client.chat)


def test_request_timeout_is_capped_by_remaining_budget(monkeypatch):
    monkeypatch.setattr(config, "MAX_WALL_CLOCK_S", 100.0)
    monkeypatch.setattr(config, "LLM_CALL_TIMEOUT_S", 300.0)
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 3)

    with run_context.run_scope("timeout-budget") as ctx:
        ctx.started_at = time.time() - 90
        timeout = client._request_timeout()

    assert 1.0 <= timeout <= 3.0  # 剩余约 10s，按 4 次尝试均摊


def test_request_timeout_raises_when_budget_is_exhausted(monkeypatch):
    monkeypatch.setattr(config, "MAX_WALL_CLOCK_S", 100.0)

    with run_context.run_scope("timeout-exhausted") as ctx:
        ctx.started_at = time.time() - 101
        with pytest.raises(client.BudgetExceeded):
            client._request_timeout()


def test_chat_passes_bounded_timeout(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(client, "get_client", lambda: fake)
    monkeypatch.setattr(config, "LLM_NATIVE_OLLAMA", False)
    monkeypatch.setattr(config, "MAX_WALL_CLOCK_S", 1000.0)
    monkeypatch.setattr(config, "LLM_CALL_TIMEOUT_S", 42.0)
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 3)

    with run_context.run_scope("chat-timeout"):
        assert _raw_chat()([{"role": "user", "content": "hi"}]) == "ok"

    assert fake.chat.completions.calls[0]["timeout"] == 42.0


def test_chat_stops_before_calling_when_budget_is_exhausted(monkeypatch):
    called = []

    def fake_get_client():
        called.append(True)
        raise AssertionError("预算耗尽时不应创建/调用客户端")

    monkeypatch.setattr(client, "get_client", fake_get_client)
    monkeypatch.setattr(config, "LLM_NATIVE_OLLAMA", False)
    monkeypatch.setattr(config, "MAX_WALL_CLOCK_S", 100.0)

    with run_context.run_scope("chat-exhausted") as ctx:
        ctx.started_at = time.time() - 101
        with pytest.raises(client.BudgetExceeded):
            _raw_chat()([{"role": "user", "content": "hi"}])

    assert called == []


def test_native_ollama_uses_bounded_timeout(monkeypatch):
    calls = []

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "prompt_eval_count": 1,
                "eval_count": 1,
                "message": {"content": "ok"},
            }

    def fake_post(url, json, timeout):
        calls.append(timeout)
        return FakeResp()

    monkeypatch.setattr(client.httpx, "post", fake_post)
    monkeypatch.setattr(config, "MAX_WALL_CLOCK_S", 100.0)
    monkeypatch.setattr(config, "LLM_CALL_TIMEOUT_S", 300.0)
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 3)

    with run_context.run_scope("native-timeout") as ctx:
        ctx.started_at = time.time() - 90
        assert client._native_chat(
            [{"role": "user", "content": "hi"}], temperature=0, json_mode=False
        ) == "ok"

    assert 1.0 <= calls[0] <= 3.0
