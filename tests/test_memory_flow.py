"""研究档案与图链路的接合：命中记忆时不开检索分支、报告如实标注、落档不重复。"""
from dataclasses import dataclass

import pytest

import config
import memory
import reliability
from budget import BUDGET
from graph.build import _fan_out, build_research_app
from graph.nodes import coordinator as coordinator_mod
from graph.nodes import planner as planner_mod
from graph.nodes import researcher as researcher_mod
from graph.nodes import verifier as verifier_mod
from graph.nodes import writer as writer_mod
from graph.nodes import archivist as archivist_mod
from llm.client import USAGE

DRAFT = "# 研究报告\n\n核心结论 [1]\n"


@dataclass
class FakePage:
    ok: bool = True
    text: str = "网页正文"


class ExplodingProvider:
    """记忆命中时不该有任何检索发生 —— 被调用就说明复用路径没生效。"""

    def search(self, query, max_results=5):
        raise AssertionError(f"命中记忆却仍然发起了检索：{query}")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DB", str(tmp_path / "memory.db"))
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_SIMILARITY_THRESHOLD", 0.62)
    monkeypatch.setattr(config, "MEMORY_MAX_AGE_DAYS", 30)
    monkeypatch.setattr(config, "MEMORY_MAX_HITS", 4)
    USAGE.reset()
    BUDGET.reset()
    reliability.reset_all()
    yield
    reliability.reset_all()


def _install_stubs(subquestions):
    coordinator_mod.chat_safe = lambda *a, **k: "研究任务书"
    planner_mod.chat_json_safe = lambda *a, **k: {"subquestions": subquestions}
    verifier_mod.chat_json_safe = lambda *a, **k: {"conflicts": [], "gaps": []}
    writer_mod.chat_safe = lambda *a, **k: DRAFT
    researcher_mod.get_search_provider = lambda: ExplodingProvider()
    researcher_mod.fetch_page = lambda url: FakePage()


def _note(sub: str, content: str = "历史结论" * 20, urls=("https://old.test/1", "https://old.test/2")):
    return {
        "subquestion": sub,
        "content": content,
        "sources": [{"title": u, "url": u} for u in urls],
    }


# ---------- 路由 ----------

def test_fan_out_goes_to_compactor_when_all_reused():
    """没有要检索的子问题（全部命中记忆）时，仍要经过压缩/核查链路。"""
    assert _fan_out({"subquestions": [], "budget_exceeded": ""}) == "compactor"


def test_fan_out_still_respects_budget():
    assert _fan_out({"subquestions": [], "budget_exceeded": "wall_clock=900s/900s"}) == "writer"


# ---------- 记忆命中：整条图跑通 ----------

def test_memory_hit_skips_research_and_marks_report():
    memory.remember("历史主题", [_note("Rust 和 Go 的并发模型对比")])
    _install_stubs(["Rust 和 Go 的并发模型对比分析"])  # 近似重复：实测相似度 0.87，应当复用

    final = build_research_app().invoke(
        {"topic": "测试主题", "notes": [], "review_points": []}, {"recursion_limit": 50}
    )

    assert final["subquestions"] == [], "命中记忆的子问题不应进入检索队列"
    assert len(final["notes"]) == 1
    assert final["notes"][0]["content"].startswith("历史结论")
    assert len(final["notes"][0]["sources"]) == 2, "来源列表必须随记忆一起复用"

    reused = final["memory_reused"]
    assert len(reused) == 1
    assert reused[0]["similarity"] >= 0.62
    assert reused[0]["note_subquestion"] == "Rust 和 Go 的并发模型对比"

    report = final["report"]
    assert DRAFT.strip() in report
    assert "## 复用历史研究说明" in report
    assert "Rust 和 Go 的并发模型对比分析" in report
    assert "记忆时间" in report

    assert final["memory_written"] == 1
    assert memory.stats()["total_hits"] == 1


def test_memory_miss_still_researches(monkeypatch):
    """未命中时行为与开启记忆前一致：照常检索。"""
    memory.remember("历史主题", [_note("完全不相关的历史子问题")])

    calls = []

    class RecordingProvider:
        def search(self, query, max_results=5):
            calls.append(query)
            return []

    _install_stubs(["Kubernetes 探针有哪几种"])
    researcher_mod.get_search_provider = lambda: RecordingProvider()

    final = build_research_app().invoke(
        {"topic": "测试主题", "notes": [], "review_points": []}, {"recursion_limit": 50}
    )

    assert calls == ["Kubernetes 探针有哪几种"], "未命中记忆的子问题必须照常检索"
    assert final["memory_reused"] == []
    assert "## 复用历史研究说明" not in final["report"]


def test_memory_disabled_never_touches_store(monkeypatch):
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    memory.remember("历史主题", [_note("Rust 和 Go 的并发模型对比")])

    calls = []

    class RecordingProvider:
        def search(self, query, max_results=5):
            calls.append(query)
            return []

    _install_stubs(["Rust 和 Go 的并发模型对比"])
    researcher_mod.get_search_provider = lambda: RecordingProvider()

    final = build_research_app().invoke(
        {"topic": "测试主题", "notes": [], "review_points": []}, {"recursion_limit": 50}
    )

    assert calls == ["Rust 和 Go 的并发模型对比"]
    assert final["memory_reused"] == []
    assert final.get("memory_written", 0) == 0


def test_archivist_skips_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    assert archivist_mod.archivist({"topic": "T", "notes": [_note("子问题")]}) == {}


def test_archivist_reports_stored_count():
    out = archivist_mod.archivist({"topic": "T", "notes": [_note("子问题甲"), _note("子问题乙")]})
    assert out["memory_written"] == 2


def test_reused_notes_go_through_compaction(monkeypatch):
    """复用来的笔记同样受上下文压缩管辖（否则记忆会绕过压缩直接顶爆上下文）。"""
    monkeypatch.setattr(config, "COMPACT_TRIGGER_CHARS", 50)
    monkeypatch.setattr(config, "COMPACT_TARGET_CHARS", 100)
    from graph.nodes import compactor as compactor_mod

    monkeypatch.setattr(compactor_mod, "chat_json_safe", lambda *a, **k: {})

    memory.remember("历史主题", [_note("Rust 和 Go 的并发模型对比", content="历史结论" * 100)])
    _install_stubs(["Rust 和 Go 的并发模型对比"])

    final = build_research_app().invoke(
        {"topic": "测试主题", "notes": [], "review_points": []}, {"recursion_limit": 50}
    )

    assert final["compaction"]["applied"] is True
    assert "压缩省略" in final["notes_digest"][0]["content"]
    assert "## 复用历史研究说明" in final["report"]
