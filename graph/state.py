"""LangGraph 共享状态定义。

notes 用 Annotated + operator.add 做归并：W2 把 researcher 拆成并行节点后，
多个分支同时写入 notes 不会互相覆盖，这是并行 fan-out 的状态层准备。

W5 新增的长流程控制字段：
- budget_exceeded  预算超限原因（空串=正常）。由节点写入、路由读取，非空即停止开新分支
- replan_count     已执行的重规划轮数（上限 MAX_REPLAN_ROUNDS）
- gaps             verifier 标记的"尚未被笔记回答的点"，planner 消费后清空
- notes_digest     压缩后的笔记视图（为空表示直接用 notes 原文）
- compaction       压缩审计（是否触发、压缩前后字符数、方式）
- memory_reused    复用历史记忆的子问题（含相似度、记忆时间），writer 据此标注来源
- memory_written   本轮写入研究档案的笔记条数（archivist 落档）
conflicts 改为累积式（跨轮追加），writer 展示前去重。
"""
import operator
from typing import Annotated, TypedDict

import config


class Source(TypedDict):
    title: str
    url: str


class Note(TypedDict):
    subquestion: str
    content: str          # 摘要正文，关键事实内嵌 markdown 引用 [标题](url)
    sources: list[Source]  # 本条笔记检索到并展示给模型的来源


class MemoryHit(TypedDict):
    subquestion: str      # 当前子问题
    note_subquestion: str  # 记忆里存的子问题原文
    topic: str
    similarity: float
    age_days: float
    updated_at: str
    sources: int          # 复用的来源条数


class ResearchState(TypedDict):
    run_id: str                     # 本轮运行 id（= thread_id），用于隔离 usage/降级/熔断
    topic: str
    brief: str                      # coordinator 输出的研究任务书
    review_points: list[str]        # 启用的审核点位（brief/plan）；缺省走 config.REVIEW_POINTS，空=全自动
    max_subquestions: int           # 本轮子问题上限；缺省回落 config.MAX_SUBQUESTIONS（走 state 避免改全局）
    max_search_rounds: int          # 本轮每子问题检索轮数上限；缺省回落 config.MAX_SEARCH_ROUNDS
    subquestions: list[str]         # planner 拆解的子问题（重规划时只含新增的子问题）
    notes: Annotated[list[Note], operator.add]
    conflicts: Annotated[list[str], operator.add]  # verifier 发现的来源冲突（跨轮累积）
    gaps: list[str]                 # verifier 发现的覆盖缺口（planner 消费后清空）
    replan_count: int               # 已重规划轮数
    budget_exceeded: str            # 非空=本轮已降级收敛，值为超限原因
    notes_digest: list[Note]        # 压缩后的笔记；为空表示未压缩
    compaction: dict                # 压缩审计信息
    memory_reused: Annotated[list[MemoryHit], operator.add]  # 复用历史记忆的记录（跨轮累积）
    memory_written: int             # 本轮落档到研究档案的笔记条数
    report: str                     # writer 最终报告（Markdown）


def effective_notes(state: ResearchState) -> list[Note]:
    """消费侧统一入口：有压缩视图用压缩视图，否则用原始笔记。"""
    return state.get("notes_digest") or state.get("notes") or []


def effective_max_subquestions(state: ResearchState) -> int:
    """本轮子问题上限：state 覆盖优先，缺省回落配置。

    按次覆盖放 state 而不是改全局 config —— 否则同进程内并发请求会互相覆盖
    （节点在"执行时"才读全局值，谁后写谁生效）。
    """
    value = state.get("max_subquestions")
    return config.MAX_SUBQUESTIONS if value is None else value


def effective_max_search_rounds(state: ResearchState) -> int:
    """本轮检索轮数上限：state 覆盖优先，缺省回落配置。"""
    value = state.get("max_search_rounds")
    return config.MAX_SEARCH_ROUNDS if value is None else value
