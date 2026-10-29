"""Researcher / Verification / Writer Agent 的动作循环。"""
from dataclasses import dataclass

import config
import reliability
from budget import BUDGET
from graph import agents as agents_mod
from graph.nodes import researcher as researcher_mod
from graph.nodes import verifier as verifier_mod
from graph.nodes import writer as writer_mod
from llm.client import USAGE
from search.base import SearchResult


@dataclass
class _Page:
    ok: bool = True
    text: str = "网页正文"


class _Provider:
    def __init__(self):
        self.queries = []

    def search(self, query, max_results=5):
        self.queries.append(query)
        return [SearchResult(title="T", url="https://x.test/a", snippet="摘要")]


def _reset():
    USAGE.reset()
    BUDGET.reset()
    reliability.reset_all()


def test_research_agent_searches_then_finalizes(monkeypatch):
    provider = _Provider()
    monkeypatch.setattr(config, "AGENT_MODE", "on")
    monkeypatch.setattr(researcher_mod, "get_search_provider", lambda: provider)
    monkeypatch.setattr(researcher_mod, "fetch_page", lambda url: _Page())
    steps = iter([
        {"action": "search", "query": "LangGraph"},
        {"action": "final", "content": "结论 [T](https://x.test/a)", "sufficient": True},
    ])
    monkeypatch.setattr(agents_mod, "chat_json_safe", lambda *a, **k: next(steps))
    _reset()

    out = researcher_mod.research_one({"subquestion": "Q", "max_search_rounds": 2})

    note = out["notes"][0]
    assert note["content"].startswith("结论")
    assert note["sources"][0]["url"] == "https://x.test/a"
    assert provider.queries == ["LangGraph"]


def test_verification_agent_returns_final_judgement(monkeypatch):
    provider = _Provider()
    monkeypatch.setattr(config, "AGENT_MODE", "on")
    monkeypatch.setattr(verifier_mod, "get_search_provider", lambda: provider)
    monkeypatch.setattr(
        agents_mod, "chat_json_safe",
        lambda *a, **k: {"action": "final", "conflicts": ["冲突"], "gaps": ["缺口"]},
    )
    _reset()

    out = verifier_mod.verifier({
        "topic": "T",
        "notes": [{"subquestion": "S", "content": "笔记", "sources": []}],
    })

    assert out == {"conflicts": ["冲突"], "gaps": ["缺口"]}


def test_writer_agent_revises_when_critique_requests_it(monkeypatch):
    monkeypatch.setattr(config, "AGENT_MODE", "on")
    monkeypatch.setattr(config, "AGENT_WRITER_REVISIONS", 1)
    monkeypatch.setattr(
        agents_mod, "chat_json_safe",
        lambda *a, **k: {"action": "revise", "instructions": "补引用"},
    )
    monkeypatch.setattr(writer_mod, "chat_json_safe", lambda *a, **k: {
        "action": "revise", "instructions": "补引用",
    })
    monkeypatch.setattr(writer_mod, "chat_safe", lambda *a, **k: "# 修订后\n\n结论 [1]")

    assert writer_mod._agent_revise("# 初稿", {}) == "# 修订后\n\n结论 [1]"
