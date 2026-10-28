"""archivist：把本轮研究的笔记落进研究档案（长期记忆）。

放在 writer 之后：一轮研究只要产出了笔记就值得记住 —— 包括"预算受限提前收敛"的那类
不完整结果（它们是真实检索得来的证据）。压缩生效时存的是压缩后的视图（信息密度更高、
体积更小）；来源列表原样保留，复用时的引用编号因此仍然完整。

落档失败只记降级事件，不影响报告输出：记忆是增益，不是主流程的依赖。
"""
import config
import memory
import reliability
from graph.state import ResearchState, effective_notes


def archivist(state: ResearchState) -> dict:
    if not config.MEMORY_ENABLED:
        return {}

    notes = [n for n in effective_notes(state) if n.get("content")]
    if not notes:
        return {}

    try:
        stored = memory.remember(state.get("topic", ""), notes)
    except Exception as exc:
        reliability.record_degradation("memory", f"落档失败（{type(exc).__name__}: {exc}）")
        return {}

    return {"memory_written": stored}
