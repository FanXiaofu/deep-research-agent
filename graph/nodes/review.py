"""human-in-the-loop：在配置的审核点位暂停执行，把内容交给用户审核/改写。

两个点位（由 state.review_points 决定，缺省走 config.REVIEW_POINTS，空=全自动）：
- brief：coordinator 之后审核研究任务书；
- plan：首次 planner 拆解之后、fan-out 之前审核子问题 —— 这是"花钱前"的最后一道关，
  重规划（verifier 回边）不再中断，否则一次研究会被反复打断。

interrupt 依赖图上的 checkpointer；恢复时通过 Command(resume=...) 传回用户输入：
空串 = 确认原内容；非空字符串 = 整组替换（任务书按原文，子问题按行拆分）。

中断值统一为 {kind, title, content}，CLI/Web 据此渲染同一套界面。
"""
from langgraph.types import interrupt

import config
from graph.state import ResearchState

_TITLES = {"brief": "研究任务书", "plan": "研究子问题"}


def _enabled_points(state: ResearchState) -> list[str]:
    """state 未显式指定审核点位时回落到配置（Web/CLI 可逐次覆盖）。"""
    points = state.get("review_points")
    return config.REVIEW_POINTS if points is None else points


def _review(state: ResearchState, kind: str, content: str) -> str:
    """通用中断-恢复：返回用户输入（空串=确认原内容）。"""
    return interrupt({"kind": kind, "title": _TITLES.get(kind, kind), "content": content})


def review_brief(state: ResearchState) -> dict:
    if "brief" not in _enabled_points(state):
        return {}
    edited = _review(state, "brief", state["brief"])
    if isinstance(edited, str) and edited.strip():
        return {"brief": edited.strip()}
    return {}


def review_plan(state: ResearchState) -> dict:
    # 重规划（replan_count>0）不再中断：否则每次 verifier 回边都会卡住一次
    if "plan" not in _enabled_points(state) or state.get("replan_count", 0) > 0:
        return {}
    subs = state.get("subquestions") or []
    if not subs or len(subs) < config.PLAN_REVIEW_MIN_SUBQUESTIONS:
        return {}
    edited = _review(state, "plan", "\n".join(subs))
    if isinstance(edited, str) and edited.strip():
        new_subs = [s.strip() for s in edited.splitlines() if s.strip()]
        if new_subs:  # 全部删空则视为确认原计划，避免拆出 0 个子问题
            return {"subquestions": new_subs}
    return {}
