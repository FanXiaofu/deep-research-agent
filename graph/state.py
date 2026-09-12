"""LangGraph 共享状态定义。

notes 用 Annotated + operator.add 做归并：W2 把 researcher 拆成并行节点后，
多个分支同时写入 notes 不会互相覆盖，这是并行 fan-out 的状态层准备。
"""
import operator
from typing import Annotated, TypedDict


class Source(TypedDict):
    title: str
    url: str


class Note(TypedDict):
    subquestion: str
    content: str          # 摘要正文，关键事实内嵌 markdown 引用 [标题](url)
    sources: list[Source]  # 本条笔记检索到并展示给模型的来源


class ResearchState(TypedDict):
    topic: str
    brief: str                      # coordinator 输出的研究任务书
    require_review: bool            # 是否在 review_brief 处暂停等待人工审核
    subquestions: list[str]         # planner 拆解的子问题
    notes: Annotated[list[Note], operator.add]
    conflicts: list[str]            # verifier 发现的来源冲突
    report: str                     # writer 最终报告（Markdown）
