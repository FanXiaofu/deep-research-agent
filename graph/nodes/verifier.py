"""verifier：交叉比对各子问题的笔记，找出相互矛盾或来源可疑的表述。"""
from graph.state import ResearchState
from llm.client import chat_json

PROMPT = """你是事实核查员。以下是研究主题「{topic}」各子问题的研究笔记：

{notes_text}

请交叉比对，找出：不同来源之间相互矛盾的表述、缺乏来源支撑的可疑论断。
以 JSON 输出：{{"conflicts": ["矛盾点描述（注明涉及哪几条来源）", "..."]}}，没有则输出空列表。"""


def verifier(state: ResearchState) -> dict:
    if not state.get("notes"):
        return {"conflicts": []}
    notes_text = "\n\n".join(
        f"### 子问题：{n['subquestion']}\n{n['content']}" for n in state["notes"] if n.get("content")
    )
    data = chat_json(
        [{"role": "user", "content": PROMPT.format(topic=state["topic"], notes_text=notes_text)}],
        temperature=0.1,
    )
    conflicts = [str(c).strip() for c in data.get("conflicts", []) if str(c).strip()]
    return {"conflicts": conflicts}
