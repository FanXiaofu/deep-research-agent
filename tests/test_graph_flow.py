"""长流程控制逻辑：路由（预算跳过 / 重规划回边）、压缩节点、writer 降级与去重。"""
import pytest

import config
import reliability
from budget import BUDGET
from graph.build import _fan_out, _route_after_verify, build_research_app
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END
from langgraph.types import Command
from llm.client import USAGE

from graph.nodes import compactor as compactor_mod
from graph.nodes import coordinator as coordinator_mod
from graph.nodes import planner as planner_mod
from graph.nodes import researcher as researcher_mod
from graph.nodes import review as review_mod
from graph.nodes import verifier as verifier_mod
from graph.nodes import writer as writer_mod
from graph.state import effective_notes
from search.base import SearchResult


@pytest.fixture(autouse=True)
def _clean_degradations():
    """降级事件是进程级共享的，测试之间必须隔离（否则"无降级"的断言会受其他测试污染）。"""
    reliability.reset_all()
    yield
    reliability.reset_all()


def _note(sub: str, content: str, urls: list[str]) -> dict:
    return {
        "subquestion": sub,
        "content": content,
        "sources": [{"title": u, "url": u} for u in urls],
    }


# ---------- 路由 ----------

def test_fan_out_dispatches_one_branch_per_subquestion():
    sends = _fan_out({"subquestions": ["a", "b"], "budget_exceeded": ""})
    assert isinstance(sends, list) and len(sends) == 2
    # 并行分支只拿到 Send 的 payload（不是完整 state），轮数上限必须随 payload 带下去
    assert all("max_search_rounds" in s.arg for s in sends)


def test_fan_out_skips_research_when_budget_exceeded():
    assert _fan_out({"subquestions": ["a"], "budget_exceeded": "wall_clock=900s/900s"}) == "writer"


def test_fan_out_routes_to_compactor_when_no_new_subquestions():
    """没有要检索的子问题（全部命中记忆）时走压缩/核查链路，而不是直接撰写。"""
    assert _fan_out({"subquestions": [], "budget_exceeded": ""}) == "compactor"


def test_route_after_verify_replans_on_gaps(monkeypatch):
    monkeypatch.setattr(config, "MAX_REPLAN_ROUNDS", 1)
    assert _route_after_verify({"gaps": ["缺 X 的对比数据"], "replan_count": 0}) == "planner"


def test_route_after_verify_stops_at_replan_limit(monkeypatch):
    monkeypatch.setattr(config, "MAX_REPLAN_ROUNDS", 1)
    assert _route_after_verify({"gaps": ["缺口"], "replan_count": 1, "conflicts": []}) == "writer"


def test_route_after_verify_goes_to_writer_when_clean():
    assert _route_after_verify({"gaps": [], "conflicts": [], "replan_count": 0}) == "writer"


def test_route_after_verify_respects_budget():
    assert _route_after_verify({"gaps": ["缺口"], "replan_count": 0, "budget_exceeded": "llm_calls=40/40"}) == "writer"


def test_graph_topology_keeps_replan_and_compaction_paths():
    graph = build_research_app().get_graph()
    edges = {(e.source, e.target) for e in graph.edges}
    assert {"planner", "review_plan", "researcher", "compactor", "verifier", "writer", "archivist"} <= set(graph.nodes)
    assert ("verifier", "planner") in edges        # 重规划回边
    assert ("planner", "review_plan") in edges     # 拆解后先过审核点，再决定 fan-out
    assert ("review_plan", "writer") in edges      # 预算超限时收敛
    assert ("review_plan", "compactor") in edges   # 全部命中记忆：跳过检索但仍走压缩/核查
    assert ("researcher", "compactor") in edges
    assert ("writer", "archivist") in edges        # 报告后落档（长期记忆写入）
    assert ("archivist", END) in edges


# ---------- 压缩 ----------

def test_compactor_passes_through_below_threshold(monkeypatch):
    monkeypatch.setattr(config, "COMPACTION_ENABLED", True)
    monkeypatch.setattr(config, "COMPACT_TRIGGER_CHARS", 10_000)
    out = compactor_mod.compactor({"topic": "T", "notes": [_note("s1", "短", ["u1"])]})
    assert out["notes_digest"] == []          # 清空压缩视图，消费侧回落到原文
    assert out["compaction"]["applied"] is False
    assert effective_notes({"notes": [_note("s1", "短", ["u1"])], "notes_digest": []})[0]["content"] == "短"


def test_compactor_truncates_when_llm_unavailable_and_keeps_sources(monkeypatch):
    monkeypatch.setattr(config, "COMPACTION_ENABLED", True)
    monkeypatch.setattr(config, "COMPACT_TRIGGER_CHARS", 100)
    monkeypatch.setattr(config, "COMPACT_TARGET_CHARS", 200)
    monkeypatch.setattr(compactor_mod, "chat_json_safe", lambda *a, **k: {})

    notes = [_note("s1", "甲" * 300, ["u1", "u2"]), _note("s2", "乙" * 300, ["u3"])]
    out = compactor_mod.compactor({"topic": "T", "notes": notes})
    digest = out["notes_digest"]

    assert out["compaction"]["applied"] is True
    assert out["compaction"]["before_chars"] == 600
    assert out["compaction"]["after_chars"] < 600
    assert out["compaction"]["llm_compressed"] == 0
    assert len(digest) == 2
    assert [n["sources"] for n in digest] == [n["sources"] for n in notes]
    assert "压缩省略" in digest[0]["content"]


def test_compactor_prefers_llm_output_and_falls_back_per_note(monkeypatch):
    monkeypatch.setattr(config, "COMPACTION_ENABLED", True)
    monkeypatch.setattr(config, "COMPACT_TRIGGER_CHARS", 100)
    monkeypatch.setattr(config, "COMPACT_TARGET_CHARS", 200)
    monkeypatch.setattr(
        compactor_mod, "chat_json_safe",
        lambda *a, **k: {"notes": [{"subquestion": "s1", "content": "压缩后的一号笔记"}]},
    )

    notes = [_note("s1", "甲" * 300, ["u1"]), _note("s2", "乙" * 300, ["u2"])]
    out = compactor_mod.compactor({"topic": "T", "notes": notes})

    assert out["notes_digest"][0]["content"] == "压缩后的一号笔记"
    assert out["compaction"]["llm_compressed"] == 1
    assert "压缩省略" in out["notes_digest"][1]["content"]  # 漏掉的条目由确定性截断兜底


# ---------- writer ----------

def test_writer_falls_back_to_notes_when_model_unavailable(monkeypatch):
    monkeypatch.setattr(writer_mod, "chat_safe", lambda *a, **k: "")
    state = {"topic": "研究主题", "brief": "任务书", "notes": [_note("s1", "笔记正文一", ["http://a"])], "conflicts": []}

    report = writer_mod.writer(state)["report"]

    assert report.startswith("# 研究主题")
    assert "笔记正文一" in report
    assert "## 参考来源" in report
    assert "[http://a](http://a)" in report


def test_writer_dedupes_conflicts_and_appends_limitation_section(monkeypatch):
    captured = {}

    def fake_chat(messages, **kwargs):
        captured["prompt"] = messages[0]["content"]
        return "# 报告标题\n\n正文 [1]"

    monkeypatch.setattr(writer_mod, "chat_safe", fake_chat)
    state = {
        "topic": "T", "brief": "B",
        "notes": [_note("s1", "正文", ["http://a"])],
        "conflicts": ["冲突A", "冲突A", "冲突B"],
        "budget_exceeded": "llm_calls=40/40",
    }

    report = writer_mod.writer(state)["report"]

    assert captured["prompt"].count("- 冲突A") == 1  # 跨轮累积的冲突展示前去重
    assert "- 冲突B" in captured["prompt"]
    assert "## 研究受限说明" in report
    assert "llm_calls=40/40" in report


def test_writer_reports_normally_when_budget_is_healthy(monkeypatch):
    monkeypatch.setattr(writer_mod, "chat_safe", lambda *a, **k: "# 报告标题\n\n正文 [1]")
    state = {"topic": "T", "brief": "B", "notes": [_note("s1", "正文", ["http://a"])], "conflicts": []}

    assert "## 研究受限说明" not in writer_mod.writer(state)["report"]


# ---------- 审核点位（human-in-the-loop）----------

def test_review_plan_skips_when_point_not_enabled():
    assert review_mod.review_plan({"review_points": ["brief"], "subquestions": ["a"]}) == {}


def test_review_plan_skips_on_replan():
    """重规划（verifier 回边）不再中断，否则一次研究会被反复打断。"""
    state = {"review_points": ["plan"], "replan_count": 1, "subquestions": ["a"]}
    assert review_mod.review_plan(state) == {}


def test_review_plan_respects_min_subquestions(monkeypatch):
    monkeypatch.setattr(config, "PLAN_REVIEW_MIN_SUBQUESTIONS", 5)
    state = {"review_points": ["plan"], "replan_count": 0, "subquestions": ["a", "b"]}
    assert review_mod.review_plan(state) == {}


def test_review_plan_skips_when_no_subquestions():
    assert review_mod.review_plan({"review_points": ["plan"], "subquestions": []}) == {}


def test_review_brief_skips_when_point_not_enabled():
    assert review_mod.review_brief({"review_points": ["plan"], "brief": "B"}) == {}


class _EmptyProvider:
    def search(self, query, max_results=5):
        return []


def test_review_plan_interrupts_after_planner_and_applies_edits(monkeypatch):
    """核心行为：plan 点位在拆解之后、fan-out 之前中断；续跑时整组替换子问题。"""
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    monkeypatch.setattr(config, "PLAN_REVIEW_MIN_SUBQUESTIONS", 0)
    coordinator_mod.chat_safe = lambda *a, **k: "研究任务书"
    planner_mod.chat_json_safe = lambda *a, **k: {"subquestions": ["原子问题甲", "原子问题乙"]}
    verifier_mod.chat_json_safe = lambda *a, **k: {"conflicts": [], "gaps": []}
    writer_mod.chat_safe = lambda *a, **k: "# 报告\n\n正文 [1]"
    researcher_mod.get_search_provider = lambda: _EmptyProvider()
    USAGE.reset()
    BUDGET.reset()

    app = build_research_app(MemorySaver())
    cfg = {"configurable": {"thread_id": "t-plan-review"}, "recursion_limit": 50}
    payload = {"topic": "测试主题", "notes": [], "review_points": ["plan"]}

    events = list(app.stream(payload, cfg, stream_mode="updates"))
    interrupts = [e["__interrupt__"][0] for e in events if "__interrupt__" in e]
    assert len(interrupts) == 1, "plan 点位应当恰好中断一次"
    assert interrupts[0].value["kind"] == "plan"
    assert interrupts[0].value["content"] == "原子问题甲\n原子问题乙"

    list(app.stream(Command(resume="新的子问题甲\n新的子问题乙\n新的子问题丙"), cfg, stream_mode="updates"))

    final = app.get_state(cfg).values
    assert final["subquestions"] == ["新的子问题甲", "新的子问题乙", "新的子问题丙"]


# ---------- 规模按次覆盖（自定义子问题上限 / 检索轮数）----------

class _FakePage:
    ok = True
    text = "网页正文"


class _CountingProvider:
    """每次检索返回一条新 url，用调用次数反映实际跑了几轮。"""

    def __init__(self):
        self.calls: list[str] = []

    def search(self, query, max_results=5):
        self.calls.append(query)
        n = len(self.calls)
        return [SearchResult(title=f"t{n}", url=f"https://x.test/{n}", snippet="摘要")]


def _stub_researcher(monkeypatch, provider):
    monkeypatch.setattr(researcher_mod, "get_search_provider", lambda: provider)
    monkeypatch.setattr(researcher_mod, "fetch_page", lambda url: _FakePage())
    monkeypatch.setattr(
        researcher_mod, "chat_json_safe",
        lambda *a, **k: {"content": "笔记", "sufficient": False, "next_query": "更细的查询"},
    )
    USAGE.reset()
    BUDGET.reset()
    reliability.reset_all()


def test_planner_honours_state_max_subquestions(monkeypatch):
    captured = {}

    def fake_chat_json(messages, **kwargs):
        captured["prompt"] = messages[0]["content"]
        return {"subquestions": ["a", "b", "c", "d", "e"]}

    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    monkeypatch.setattr(planner_mod, "chat_json_safe", fake_chat_json)
    BUDGET.reset()

    out = planner_mod.planner({"brief": "任务书", "max_subquestions": 2})

    assert len(out["subquestions"]) == 2, "子问题上限应取 state 覆盖值"
    assert "最多 2 个" in captured["prompt"]


def test_researcher_honours_state_max_search_rounds(monkeypatch):
    provider = _CountingProvider()
    _stub_researcher(monkeypatch, provider)

    note = researcher_mod.research_one({"subquestion": "S", "max_search_rounds": 3})["notes"][0]

    assert len(provider.calls) == 3, "检索轮数应取 state 覆盖值"
    assert note["content"] == "笔记\n\n笔记\n\n笔记"


def test_researcher_falls_back_to_config_rounds(monkeypatch):
    provider = _CountingProvider()
    _stub_researcher(monkeypatch, provider)
    monkeypatch.setattr(config, "MAX_SEARCH_ROUNDS", 2)

    researcher_mod.research_one({"subquestion": "S"})

    assert len(provider.calls) == 2, "缺省应回落 config.MAX_SEARCH_ROUNDS"
