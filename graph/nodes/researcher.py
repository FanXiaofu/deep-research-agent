"""researcher：单个子问题的 搜索 → 抓取 → 摘要 → 反思 循环。

W2 起由 planner 经 Send API 为每个子问题并行启动一个本节点实例：
实例之间上下文完全隔离（各自独立的检索/笔记上下文），只回传一条精炼笔记，
notes 在主状态经 operator.add 归并 —— 这就是"子 Agent 上下文隔离"的实现。
"""
from typing import TypedDict

import config
from fetch import fetch_page
from graph.state import Note, Source
from llm.client import chat_json
from search import get_search_provider
from search.base import SearchProvider, SearchResult

# 每个网页进入提示词的正文上限（控制 token 成本；eval A/B 调优项，见 config.RESEARCH_PAGE_CHARS）

ROUND_PROMPT = """你是研究助理。研究子问题：{sub}
{prev_section}
本轮搜索到的网页（含正文节选）：
{blocks}

请完成三件事，以 JSON 输出：
1. content：用 {note_min}~{note_max} 字中文总结与该子问题强相关的事实与数据，关键结论后内嵌 markdown 引用 [标题](url)，只允许引用上面列出的 url；
2. sufficient：布尔值，结合之前已收集的笔记，判断现有信息是否足以回答该子问题；
3. next_query：若 sufficient 为 false，给出一个更具体、更利于检索的新查询词（可用英文）；否则为空字符串。
输出 JSON：{{"content": "...", "sufficient": false, "next_query": "..."}}"""


class ResearcherInput(TypedDict):
    subquestion: str


def _build_blocks(results: list[SearchResult], pages: dict) -> str:
    blocks = []
    for i, r in enumerate(results, 1):
        page = pages.get(r.url)
        body = page.text[: config.RESEARCH_PAGE_CHARS] if page and page.ok else ""
        block = f"[{i}] {r.title} | {r.url}\n摘要：{r.snippet[:300]}"
        if body:
            block += f"\n正文节选：{body}"
        blocks.append(block)
    return "\n\n".join(blocks)


def _research_one(provider: SearchProvider, subquestion: str) -> Note:
    content_parts: list[str] = []
    used: dict[str, Source] = {}
    seen_urls: set[str] = set()
    query = subquestion

    for round_i in range(config.MAX_SEARCH_ROUNDS):
        results = [r for r in provider.search(query, config.MAX_RESULTS_PER_SEARCH) if r.url and r.url not in seen_urls]
        if not results:
            break
        for r in results:
            seen_urls.add(r.url)
            used[r.url] = {"title": r.title or r.url, "url": r.url}

        pages = {r.url: fetch_page(r.url) for r in results}
        prev_section = (
            f"之前几轮已收集的笔记：\n{chr(10).join(content_parts)}\n" if content_parts else ""
        )
        data = chat_json(
            [{
                "role": "user",
                "content": ROUND_PROMPT.format(
                    sub=subquestion,
                    prev_section=prev_section,
                    blocks=_build_blocks(results, pages),
                    note_min=config.NOTE_MIN_CHARS,
                    note_max=config.NOTE_MAX_CHARS,
                ),
            }],
            temperature=0.2,
        )
        note = str(data.get("content", "")).strip()
        if note:
            content_parts.append(note)
        if data.get("sufficient") or round_i == config.MAX_SEARCH_ROUNDS - 1:
            break
        query = str(data.get("next_query") or "").strip() or f"{subquestion} 详细分析"

    return {
        "subquestion": subquestion,
        "content": "\n\n".join(content_parts),
        "sources": list(used.values()),
    }


def research_one(state: ResearcherInput) -> dict:
    provider = get_search_provider()
    note = _research_one(provider, state["subquestion"])
    return {"notes": [note]}
