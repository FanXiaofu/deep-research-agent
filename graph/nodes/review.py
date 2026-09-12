"""human-in-the-loop：暂停执行，把研究任务书交给用户审核。

require_review=False（CLI 的 --no-review）时直接放行。
interrupt 依赖图上的 checkpointer；恢复时通过 Command(resume=...) 传回用户输入：
回车确认传回 None，输入文字则替换为新任务书。
"""
from langgraph.types import interrupt

from graph.state import ResearchState


def review_brief(state: ResearchState) -> dict:
    if not state.get("require_review", True):
        return {}
    edited = interrupt({"brief": state["brief"]})
    if isinstance(edited, str) and edited.strip():
        return {"brief": edited.strip()}
    return {}
