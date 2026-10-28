"""planner：把任务书拆解为可独立检索的子问题（JSON 输出）。

两个入口共用本节点：
- 首次规划：按任务书拆子问题（上限取 state 覆盖，缺省 config.MAX_SUBQUESTIONS）；
- 重规划（verifier 报告缺口/冲突后回到这里）：只拆"新增"的子问题，专门补缺口，
  数量受 MAX_FOLLOWUP_SUBQUESTIONS 约束，且必须避开已检索过的子问题（否则重复烧 token）。

W6 起在拆解后查一次**研究档案**（memory.py）：命中的子问题直接复用历史笔记（不再派
检索分支），未命中的才进 Send fan-out。一次最多复用 MEMORY_MAX_HITS 条 ——
避免整篇新报告都建立在旧结论上；复用记录写进 memory_reused，由 writer 在报告里标注来源与日期。
预算超限时不再规划新分支，直接返回空列表让路由收敛到 writer。
"""
import config
import memory
import reliability
from budget import BUDGET
from graph.state import ResearchState, effective_max_subquestions
from llm.client import chat_json_safe

PROMPT = """你是研究规划师。研究任务书如下：
{brief}

请拆解出最多 {max_n} 个可独立检索、互不重叠的子问题，共同覆盖任务书的主要维度。
子问题要具体、包含利于搜索引擎检索的关键词，使用与任务书相同的语言。
以 JSON 输出：{{"subquestions": ["子问题1", "子问题2"]}}"""

FOLLOWUP_PROMPT = """你是研究规划师。研究任务书如下：
{brief}

已完成检索的子问题（不要重复，也不要提出与之重叠的问题）：
{covered}

事实核查员本轮标记的缺口与冲突（本轮规划必须针对性补齐）：
{feedback}

请再拆解最多 {max_n} 个**新的**可独立检索子问题，专门用于补上述缺口或交叉验证冲突来源；
若上述问题已可由现有笔记回答、无需新的检索，输出空列表。
以 JSON 输出：{{"subquestions": ["新子问题1"]}}"""


def _clean(raw, limit: int) -> list[str]:
    return [str(s).strip() for s in raw if str(s).strip()][:limit]


def _split_by_memory(subquestions: list) -> tuple:
    """
    查研究档案，把子问题分成"可直接复用"和"需要检索"两组。

    返回 (待检索子问题, 复用笔记, 复用记录)。记忆不可用时原样返回，绝不阻断研究。
    """
    if not config.MEMORY_ENABLED or not subquestions:
        return subquestions, [], []

    try:
        hits = memory.lookup(subquestions)
    except Exception as exc:  # 记忆库异常必须降级为"无记忆"，而不是让研究失败
        reliability.record_degradation("memory", f"记忆查询失败（{type(exc).__name__}: {exc}）")
        return subquestions, [], []

    reused_notes, records, used_ids, pending = [], [], [], []

    for sub in subquestions:
        hit = hits.get(sub)
        if hit and len(reused_notes) < config.MEMORY_MAX_HITS:
            # 复用形态与 Note 完全一致，下游（压缩/核查/撰写）无需区分来源
            reused_notes.append({
                "subquestion": sub,
                "content": hit["note"],
                "sources": hit["sources"],
            })
            records.append({
                "subquestion": sub,
                "note_subquestion": hit.get("subquestion") or sub,
                "topic": hit["topic"],
                "similarity": hit["similarity"],
                "age_days": hit["age_days"],
                "updated_at": hit["updated_at"],
                "sources": len(hit["sources"]),
            })
            used_ids.append(hit["id"])
        else:
            pending.append(sub)

    if used_ids:
        try:
            memory.mark_used(used_ids)
        except Exception:
            pass  # 命中统计失败不影响主流程

    return pending, reused_notes, records


def planner(state: ResearchState) -> dict:
    blocked = BUDGET.exceeded()
    if blocked:
        # 预算已超限：不新增分支，交给 writer 用现有笔记收敛
        return {"subquestions": [], "gaps": [], "budget_exceeded": blocked}

    gaps = state.get("gaps") or []
    conflicts = state.get("conflicts") or []
    replan_round = state.get("replan_count", 0) + 1 if (gaps or conflicts) else 0

    if replan_round:
        feedback = "\n".join(f"- {g}" for g in gaps) or "（无缺口，仅需交叉验证冲突来源）"
        if conflicts:
            feedback += "\n" + "\n".join(f"- 冲突：{c}" for c in conflicts)
        data = chat_json_safe(
            [{
                "role": "user",
                "content": FOLLOWUP_PROMPT.format(
                    brief=state["brief"],
                    covered="\n".join(f"- {s}" for s in state.get("subquestions", [])) or "（无）",
                    feedback=feedback,
                    max_n=config.MAX_FOLLOWUP_SUBQUESTIONS,
                ),
            }],
            temperature=0.2,
            fallback={"subquestions": []},
            what="重规划",
        )
        subs = _clean(data.get("subquestions", []), config.MAX_FOLLOWUP_SUBQUESTIONS)
        pending, reused, records = _split_by_memory(subs)
        # gaps 已消费：清空，避免下一轮路由仅凭旧缺口再次重规划（conflicts 保留给 writer 呈现）
        return {
            "subquestions": pending,
            "notes": reused,
            "memory_reused": records,
            "gaps": [],
            "replan_count": replan_round,
        }

    max_n = effective_max_subquestions(state)
    data = chat_json_safe(
        [{"role": "user", "content": PROMPT.format(brief=state["brief"], max_n=max_n)}],
        temperature=0.2,
        fallback={"subquestions": []},
        what="规划",
    )
    subs = _clean(data.get("subquestions", []), max_n)
    if not subs:  # 兜底：规划失败时退化为直接研究原问题
        subs = [state["topic"]]

    pending, reused, records = _split_by_memory(subs)

    return {
        "subquestions": pending,
        "notes": reused,
        "memory_reused": records,
        "replan_count": 0,
    }
