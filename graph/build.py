"""组装 LangGraph 状态机。

拓扑（W2 起，W5 增加压缩与重规划，W6 增加研究档案，W7 增加规划审核点）：
    START → coordinator → review_brief(interrupt) → planner → review_plan(interrupt)
          → [Send fan-out × N] researcher → compactor → verifier
          → verifier 之后按需回到 planner（重规划）或进入 writer → archivist → END

两个 human-in-the-loop 点位都可用 REVIEW_POINTS 开关（brief/plan），关掉时不产生中断。

P2 起 researcher / verifier / writer 内部可用有界 Agent 循环（AGENT_MODE=on）；
图的拓扑、预算、checkpoint 和 HITL 仍由这个 supervisor workflow 控制。

四个长流程控制点：
- 预算闸门：planner / researcher 在开新分支前查全局预算，超限则把原因写入状态，
  路由据此跳过并行检索，直接收敛到 writer（保证"预算耗尽也有报告"）；
- 重规划回边：verifier 报告 gaps/conflicts 且未超过 MAX_REPLAN_ROUNDS 时回到 planner，
  只对新增子问题做 Send fan-out（已完成的分支不重跑）；
- 压缩：researcher 全部完成后先经 compactor，把笔记正文压到目标字数再进核查/撰写；
- 研究档案：planner 查记忆（命中的子问题直接复用笔记、不开分支），archivist 在报告后落档。
  若子问题全部命中记忆，则没有 researcher 可跑，路由直接进 compactor，保持链路一致。
节点级 retry_policy 只兜"节点整体抛出异常"这种意外（节点内部已做降级，正常不会触发）。
"""
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, Send

import config
import db
import logging_config
import run_context
from graph.nodes import (
    archivist,
    compactor,
    coordinator,
    planner,
    researcher,
    review,
    verifier,
    writer,
)
from graph.state import ResearchState, effective_max_search_rounds

# 节点内已有降级逻辑，这里的重试只针对"未被捕获的意外异常"，重试次数保持克制
_NODE_RETRY = RetryPolicy(max_attempts=2, initial_interval=1.0, retry_on=Exception)


def _bind_run(fn):
    """节点入口绑定本轮 run：并行 researcher 在各自线程里也能读写同一份 run 状态。"""

    def wrapped(state):
        run_id = state.get("run_id")
        with run_context.run_scope(run_id), logging_config.thread_scope(run_id):
            return fn(state)

    return wrapped


def _fan_out(state: ResearchState) -> list[Send] | str:
    """并行派发 researcher；无新子问题（含全部命中记忆）或预算超限时走收敛路径。"""
    if state.get("budget_exceeded"):
        return "writer"
    subs = state.get("subquestions") or []
    if not subs:
        # 没有要检索的子问题：仍要经过压缩/核查再撰写（复用来的笔记同样需要核对冲突）
        return "compactor"
    # 并行分支只拿到 Send 的 payload（不是完整 state），轮数上限必须随 payload 带下去
    rounds = effective_max_search_rounds(state)
    return [
        Send(
            "researcher",
            {"subquestion": sub, "max_search_rounds": rounds, "run_id": state.get("run_id")},
        )
        for sub in subs
    ]


def _route_after_verify(state: ResearchState) -> str:
    """重规划判定：有缺口/冲突且还有重规划额度才回 planner，否则去 writer。"""
    if state.get("budget_exceeded"):
        return "writer"
    if state.get("replan_count", 0) >= config.MAX_REPLAN_ROUNDS:
        return "writer"
    if not (state.get("gaps") or state.get("conflicts")):
        return "writer"
    return "planner"


_pg_pool = None


def make_checkpointer():
    """默认 SQLite；配置 DATABASE_URL 后使用 PostgresSaver（连接池进程级复用）。"""
    if config.DATABASE_URL.strip():
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise RuntimeError(
                "DATABASE_URL 已配置但缺少 Postgres 依赖："
                "pip install 'langgraph-checkpoint-postgres' 'psycopg[binary]' 'psycopg-pool'"
            ) from exc
        global _pg_pool
        if _pg_pool is None:
            _pg_pool = ConnectionPool(
                conninfo=config.DATABASE_URL,
                min_size=1,
                max_size=10,
                open=True,
                # PostgresSaver.setup() 里有 CREATE INDEX CONCURRENTLY，必须 autocommit
                kwargs={"autocommit": True},
            )
        saver = PostgresSaver(_pg_pool)
        saver.setup()
        return saver
    conn = db.connect(config.CHECKPOINT_DB, check_same_thread=False)
    return SqliteSaver(conn)


def thread_config(thread_id: str) -> dict:
    """统一构造运行配置：递归上限跟随"规划-检索-核查"最多可循环的轮数。"""
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": config.GRAPH_RECURSION_LIMIT,
    }


def build_research_app(checkpointer=None):
    g = StateGraph(ResearchState)
    g.add_node("coordinator", _bind_run(coordinator.coordinator), retry_policy=_NODE_RETRY)
    g.add_node("review_brief", _bind_run(review.review_brief))
    g.add_node("planner", _bind_run(planner.planner), retry_policy=_NODE_RETRY)
    g.add_node("review_plan", _bind_run(review.review_plan))
    g.add_node("researcher", _bind_run(researcher.research_one), retry_policy=_NODE_RETRY)
    g.add_node("compactor", _bind_run(compactor.compactor), retry_policy=_NODE_RETRY)
    g.add_node("verifier", _bind_run(verifier.verifier), retry_policy=_NODE_RETRY)
    g.add_node("writer", _bind_run(writer.writer), retry_policy=_NODE_RETRY)
    g.add_node("archivist", _bind_run(archivist.archivist), retry_policy=_NODE_RETRY)

    g.add_edge(START, "coordinator")
    g.add_edge("coordinator", "review_brief")
    g.add_edge("review_brief", "planner")
    g.add_edge("planner", "review_plan")
    g.add_conditional_edges("review_plan", _fan_out, ["researcher", "compactor", "writer"])
    g.add_edge("researcher", "compactor")
    g.add_edge("compactor", "verifier")
    g.add_conditional_edges("verifier", _route_after_verify, ["planner", "writer"])
    g.add_edge("writer", "archivist")
    g.add_edge("archivist", END)
    return g.compile(checkpointer=checkpointer)
