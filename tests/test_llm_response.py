"""API provider 返回空 choices 时给出清晰错误，而不是 TypeError。"""
from types import SimpleNamespace

import pytest

from llm import client


class _FakeCompletions:
    def __init__(self, response):
        self.response = response

    def create(self, **kwargs):
        return self.response


def _patch_client(monkeypatch, response):
    fake = SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions(response))
    )
    monkeypatch.setattr(client, "get_client", lambda: fake)


def test_empty_choices_raise_clear_error(monkeypatch):
    _patch_client(monkeypatch, SimpleNamespace(choices=[], usage=None))
    with pytest.raises(RuntimeError, match="空 choices"):
        client.chat([{"role": "user", "content": "hello"}])


def test_none_content_becomes_empty(monkeypatch):
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None))],
        usage=None,
    )
    _patch_client(monkeypatch, response)
    assert client.chat([{"role": "user", "content": "hello"}]) == ""


class _BadRequest(Exception):
    status_code = 400


class _RetryCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise _BadRequest("response_format unsupported")
        assert "response_format" not in kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
            usage=None,
        )


def test_json_mode_400_retries_without_response_format(monkeypatch):
    completions = _RetryCompletions()
    fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(client, "get_client", lambda: fake)
    assert client.chat_json([{"role": "user", "content": "JSON"}]) == {"ok": True}
    assert completions.calls == 2
