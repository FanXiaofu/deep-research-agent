"""researcher：单个子问题的 搜索 → 抓取 → 摘要 → 反思 循环。

W2 起由 planner 经 Send API 为每个子问题并行启动一个本节点实例：
实例之间上下文完全隔离（各自独立的检索/笔记上下文），只回传一条精炼笔记，
notes 在主状态经 operator.add 归并 —— 这就是"子 Agent 上下文隔离"的实现。

W5 长流程加固：
- 每轮开始前检查全局预算，超限即带着已有内容收尾（不再开新检索）；
- 搜索走重试 + 熔断，抓取按结果统计熔断；外部依赖不可用时返回"部分笔记"，
  而不是把异常抛出节点 —— 一个分支失败不该毁掉整轮研究（也避免重跑已花的检索成本）。
"""
from typing import TypedDict

import config
import reliability
from budget import BUDGET
from graph.agents import run_agent
from fetch import fetch_page
from graph.state import Note, Source, effective_max_search_rounds
from llm.client import chat_json_safe
from search import get_search_provider
from search.base import SearchProvider, SearchResult
from untrusted import GUARD, wrap

# 每个网页进入提示词的正文上限（控制 token 成本；eval A/B 调优项，见 config.RESEARCH_PAGE_CHARS）

ROUND_PROMPT = """你是研究助理。研究子问题：{sub}
{guard}
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
    max_search_rounds: int  # 由 _fan_out 随 Send payload 带下来（并行分支拿不到完整 state）
    run_id: str             # 同上：并行分支据此绑定到本轮 usage/降级/熔断


def _build_blocks(results: list[SearchResult], pages: dict) -> str:
    blocks = []
    for i, r in enumerate(results, 1):
        page = pages.get(r.url)
        body = page.text[: config.RESEARCH_PAGE_CHARS] if page and page.ok else ""
        block = f"[{i}] {r.title} | {r.url}\n摘要：{r.snippet[:300]}"
        if body:
            block += f"\n正文节选：{body}"
        blocks.append(wrap(block, source=r.url))
    return "\n\n".join(blocks)


def _fetch_all(results: list[SearchResult]) -> dict:
    """抓取本轮结果：命中熔断就停止剩余抓取（快速降级，不做无谓重试）。"""
    pages = {}
    breaker = reliability.get_breaker("fetch")
    for r in results:
        if not breaker.allow():
            reliability.record_degradation("fetch", "熔断中，跳过本轮剩余网页抓取")
            break
        page = fetch_page(r.url)
        if page.ok:
            breaker.record_success()
        else:
            breaker.record_failure()
        pages[r.url] = page
    return pages


def _research_one(provider: SearchProvider, subquestion: str, max_rounds: int) -> tuple[Note, str]:
    """返回 (笔记, 预算超限原因)。第二个值为空串表示正常跑完。"""
    content_parts: list[str] = []
    used: dict[str, Source] = {}
    seen_urls: set[str] = set()
    query = subquestion
    blocked = ""

    for round_i in range(max_rounds):
        blocked = BUDGET.exceeded()
        if blocked:
            reliability.record_degradation("budget", f"「{subquestion[:20]}」因 {blocked} 提前收尾")
            break

        results, search_failed = reliability.call_guarded(
            "search",
            lambda q=query: provider.search(q, config.MAX_RESULTS_PER_SEARCH),
            attempts=config.SEARCH_RETRIES,
            default=[],
            what=f"搜索「{query[:24]}」",
        )
        results = [r for r in results if r.url and r.url not in seen_urls]
        if search_failed or not results:
            break
        pages = _fetch_all(results)
        for r in results:
            seen_urls.add(r.url)
            page = pages.get(r.url)
            excerpt = (r.snippet or (page.text if page and page.ok else ""))[:500]
            used[r.url] = {
                "title": r.title or r.url,
                "url": r.url,
                "excerpt": excerpt,
            }

        prev_section = (
            f"之前几轮已收集的笔记：\n{chr(10).join(content_parts)}\n" if content_parts else ""
        )
        data = chat_json_safe(
            [{
                "role": "user",
                "content": ROUND_PROMPT.format(
                    sub=subquestion,
                    guard=GUARD,
                    prev_section=prev_section,
                    blocks=_build_blocks(results, pages),
                    note_min=config.NOTE_MIN_CHARS,
                    note_max=config.NOTE_MAX_CHARS,
                ),
            }],
            temperature=0.2,
            fallback={},
            what="笔记生成",
        )
        if not data:
            break
        note = str(data.get("content", "")).strip()
        if note:
            content_parts.append(note)
        if data.get("sufficient") or round_i == max_rounds - 1:
            break
        query = str(data.get("next_query") or "").strip() or f"{subquestion} 详细分析"

    return {
        "subquestion": subquestion,
        "content": "\n\n".join(content_parts),
        "sources": list(used.values()),
    }, blocked


def _agent_research(
    provider: SearchProvider, subquestion: str, max_rounds: int
) -> tuple[Note, str] | None:
    """有界 ResearchAgent：模型自己决定搜索、抓取、何时收尾。"""
    used: dict[str, Source] = {}
    seen: set[str] = set()

    def search(data: dict) -> dict:
        query = str(data.get("query") or subquestion).strip()
        results, failed = reliability.call_guarded(
            "search",
            lambda: provider.search(query, config.MAX_RESULTS_PER_SEARCH),
            attempts=config.SEARCH_RETRIES,
            default=[],
            what=f"搜索「{query[:24]}」",
        )
        if failed:
            return {"error": "搜索失败", "results": []}
        fresh = [r for r in results if r.url and r.url not in seen]
        for r in fresh:
            seen.add(r.url)
            used[r.url] = {
                "title": r.title or r.url,
                "url": r.url,
                "excerpt": (r.snippet or "")[:500],
            }
        return {
            "results": [
                {"title": r.title, "url": r.url, "snippet": (r.snippet or "")[:300]}
                for r in fresh
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
        used.setdefault(url, {"title": url, "url": url, "excerpt": page.text[:500]})
        return {"url": url, "text": page.text[: config.RESEARCH_PAGE_CHARS]}

    final = run_agent(
        role="ResearchAgent",
        task=(
            f"研究子问题：{subquestion}\n"
            f"先用 search 找资料，再按需 fetch 正文，最后用 final 交回 "
            f"{config.NOTE_MIN_CHARS}~{config.NOTE_MAX_CHARS} 字中文笔记。"
            "笔记中的关键结论只能引用你实际看过的 url，并使用 markdown 链接。"
        ),
        tool_specs=[
            '{"action":"search","query":"检索词"}',
            '{"action":"fetch","url":"https://..."}',
            '{"action":"final","content":"笔记正文","sufficient":true}',
        ],
        tools={"search": search, "fetch": fetch},
        max_steps=max(2, min(config.AGENT_MAX_STEPS, max_rounds * 2)),
    )
    if not final:
        return None
    content = str(final.get("content") or "").strip()
    if not content:
        return None
    blocked = str(final.get("reason") or "") if final.get("action") == "budget_exceeded" else ""
    return {
        "subquestion": subquestion,
        "content": content,
        "sources": list(used.values()),
    }, blocked


def research_one(state: ResearcherInput) -> dict:
    provider = get_search_provider()
    subquestion = state["subquestion"]
    max_rounds = effective_max_search_rounds(state)
    note, blocked = None, ""
    if config.AGENT_MODE == "on":
        result = _agent_research(provider, subquestion, max_rounds)
        if result:
            note, blocked = result
    if note is None:
        # Agent 不可用/未按协议收尾时回落到旧的确定性检索循环。
        note, blocked = _research_one(provider, subquestion, max_rounds)
    out: dict = {"notes": [note]}
    if blocked:  # 只在超限时写该字段：并行分支用空串覆盖会抹掉真实原因
        out["budget_exceeded"] = blocked
    return out
