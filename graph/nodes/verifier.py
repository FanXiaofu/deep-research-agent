"""verifier：交叉比对各子问题的笔记，找出矛盾表述与**尚未被回答的缺口**。

两个产出分别驱动不同动作：
- conflicts → 交给 writer 如实呈现（跨轮累积，writer 展示前去重）；
- gaps      → 触发重规划：路由据此回到 planner 追加有针对性的检索子问题。
无笔记时本节点不调用 LLM（空跑直接返回），避免纯浪费一次调用。
"""
import config
from graph.agents import run_agent
from graph.state import ResearchState, effective_notes
from llm.client import chat_json_safe
from fetch import fetch_page
from search import get_search_provider

PROMPT = """你是事实核查员。以下是研究主题「{topic}」各子问题的研究笔记：

{notes_text}

请完成两项判断：
1. conflicts：不同来源之间相互矛盾的表述、缺乏来源支撑的可疑论断；
2. gaps：任务相关但现有笔记**没有回答**的关键点（下轮检索可以补上的具体问题）。
要求：只依赖上面给出的笔记判断；笔记里已经说清的内容不要列为 gap。
以 JSON 输出：{{"conflicts": ["矛盾点描述（注明涉及哪几条来源）"], "gaps": ["待补充检索的具体问题"]}}，没有则输出空列表。"""


def _agent_verifier(state: ResearchState, notes_text: str) -> dict | None:
    provider = get_search_provider()
    seen: set[str] = set()

    def search(data: dict) -> dict:
        query = str(data.get("query") or "").strip()
        if not query:
            return {"error": "缺少 query"}
        results = provider.search(query, config.MAX_RESULTS_PER_SEARCH)
        for result in results:
            if result.url:
                seen.add(result.url)
        return {
            "results": [
                {"title": r.title, "url": r.url, "snippet": (r.snippet or "")[:300]}
                for r in results
            ]
        }

    def fetch(data: dict) -> dict:
        url = str(data.get("url") or "").strip()
        if not url:
            return {"error": "缺少 url"}
        if url not in seen:
            return {"error": "只能抓取 search 返回过的 url"}
        page = fetch_page(url)
        if not page.ok:
            return {"error": "抓取失败", "url": url}
        return {"url": url, "text": page.text[: config.RESEARCH_PAGE_CHARS]}

    final = run_agent(
        role="VerificationAgent",
        task=(
            f"研究主题：{state['topic']}\n\n研究笔记：\n{notes_text}\n\n"
            "判断笔记之间的冲突和尚未回答的缺口。必要时用 search/fetch 交叉核对来源；"
            "最后用 final 输出 conflicts 和 gaps 两个字符串数组。"
        ),
        tool_specs=[
            '{"action":"search","query":"用于核对的检索词"}',
            '{"action":"fetch","url":"https://..."}',
            '{"action":"final","conflicts":["冲突"],"gaps":["缺口"]}',
        ],
        tools={"search": search, "fetch": fetch},
        max_steps=config.AGENT_VERIFIER_MAX_STEPS,
    )
    if not final or final.get("action") != "final":
        return None
    conflicts = [str(c).strip() for c in final.get("conflicts", []) if str(c).strip()]
    gaps = [str(g).strip() for g in final.get("gaps", []) if str(g).strip()]
    return {"conflicts": conflicts, "gaps": gaps}


def verifier(state: ResearchState) -> dict:
    notes = [n for n in effective_notes(state) if n.get("content")]
    if not notes:
        return {"conflicts": [], "gaps": []}
    notes_text = "\n\n".join(f"### 子问题：{n['subquestion']}\n{n['content']}" for n in notes)
    if config.AGENT_MODE == "on":
        result = _agent_verifier(state, notes_text)
        if result is not None:
            return result
    data = chat_json_safe(
        [{"role": "user", "content": PROMPT.format(topic=state["topic"], notes_text=notes_text)}],
        temperature=0.1,
        fallback={},
        what="事实核查",
    )
    conflicts = [str(c).strip() for c in data.get("conflicts", []) if str(c).strip()]
    gaps = [str(g).strip() for g in data.get("gaps", []) if str(g).strip()]
    return {"conflicts": conflicts, "gaps": gaps}
