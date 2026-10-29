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
