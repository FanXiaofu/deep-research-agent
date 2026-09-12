"""组装 LangGraph 状态机。

W2 架构：
- planner 经 _fan_out（Send API）为每个子问题并行启动 researcher 实例
- coordinator 之后是 review_brief（human-in-the-loop interrupt）
- 全图挂 SqliteSaver checkpoint：每步落盘，支持中断后续跑
"""
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

import config
from graph.nodes import coordinator, planner, researcher, review, verifier, writer
from graph.state import ResearchState


def _fan_out(state: ResearchState) -> list[Send]:
    return [Send("researcher", {"subquestion": sub}) for sub in state["subquestions"]]


def make_checkpointer() -> SqliteSaver:
    conn = sqlite3.connect(config.CHECKPOINT_DB, check_same_thread=False)
    return SqliteSaver(conn)


def build_research_app(checkpointer: SqliteSaver | None = None):
    g = StateGraph(ResearchState)
    g.add_node("coordinator", coordinator.coordinator)
    g.add_node("review_brief", review.review_brief)
    g.add_node("planner", planner.planner)
    g.add_node("researcher", researcher.research_one)
    g.add_node("verifier", verifier.verifier)
    g.add_node("writer", writer.writer)

    g.add_edge(START, "coordinator")
    g.add_edge("coordinator", "review_brief")
    g.add_edge("review_brief", "planner")
    g.add_conditional_edges("planner", _fan_out, ["researcher"])
    g.add_edge("researcher", "verifier")
    g.add_edge("verifier", "writer")
    g.add_edge("writer", END)
    return g.compile(checkpointer=checkpointer)
