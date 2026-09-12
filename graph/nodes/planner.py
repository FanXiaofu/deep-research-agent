"""planner：把任务书拆解为若干可独立检索的子问题（JSON 输出）。"""
import config
from graph.state import ResearchState
from llm.client import chat_json

PROMPT = """你是研究规划师。研究任务书如下：
{brief}

请拆解出最多 {max_n} 个可独立检索、互不重叠的子问题，共同覆盖任务书的主要维度。
子问题要具体、包含利于搜索引擎检索的关键词，使用与任务书相同的语言。
以 JSON 输出：{{"subquestions": ["子问题1", "子问题2"]}}"""


def planner(state: ResearchState) -> dict:
    data = chat_json(
        [{"role": "user", "content": PROMPT.format(brief=state["brief"], max_n=config.MAX_SUBQUESTIONS)}],
        temperature=0.2,
    )
    subs = [str(s).strip() for s in data.get("subquestions", []) if str(s).strip()]
    if not subs:  # 兜底：规划失败时退化为直接研究原问题
        subs = [state["topic"]]
    return {"subquestions": subs[: config.MAX_SUBQUESTIONS]}
