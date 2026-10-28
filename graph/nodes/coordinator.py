"""coordinator：把用户的原始问题改写成清晰的研究任务书（brief）。

改写失败（模型不可用）时退回原始问题当任务书：任务书只是给 planner 的输入，
不值得为了它中断整轮研究。
"""
from graph.state import ResearchState
from llm.client import chat_safe

PROMPT = """你是深度研究协调员。用户的原始研究请求如下：
{topic}

请把它改写成一份清晰的研究任务书（brief），说明：研究目标、需要覆盖的维度、期望的产出形式。
只输出任务书正文（120 字以内），不要开始检索或回答问题本身。"""


def coordinator(state: ResearchState) -> dict:
    brief = chat_safe(
        [{"role": "user", "content": PROMPT.format(topic=state["topic"])}],
        temperature=0.2,
        fallback=state["topic"],
        what="任务书生成",
    ).strip()
    return {"brief": brief or state["topic"]}
