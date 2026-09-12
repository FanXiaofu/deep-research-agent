"""writer：汇总笔记生成带引用编号的 Markdown 报告。

引用编号在代码里确定性生成（按来源首次出现顺序去重编号），
模型只负责用 [n] 标注，"参考来源"章节由代码自动附加 —— 避免模型编造编号。
"""
import config
from graph.state import ResearchState
from llm.client import chat


def _build_registry(notes: list[dict]) -> tuple[dict[str, int], list[dict]]:
    """按首次出现顺序给来源去重编号：url -> 序号。"""
    registry: dict[str, int] = {}
    ordered: list[dict] = []
    for note in notes:
        for src in note.get("sources", []):
            url = src.get("url", "")
            if url and url not in registry:
                registry[url] = len(ordered) + 1
                ordered.append(src)
    return registry, ordered


PROMPT = """你是研究报告撰写人。

研究主题：{topic}
研究任务书：{brief}

各子问题的研究笔记（内嵌 markdown 链接）：
{notes_text}

来源编号清单（正文中的引用 [n] 必须与此对应，不得编造编号）：
{sources_text}
{conflicts_section}
请撰写一篇结构化 Markdown 研究报告，要求：
1. 结构：# 标题、## 核心结论（3~5 条要点）、## 各维度发现（按子问题分节论述）、## 不确定性与来源冲突（仅当有冲突时保留）；
2. 每个关键论断都用 [n] 标注引用编号；不要自行输出"参考来源"章节（系统会自动附加）；
3. 中文，800~1500 字。{strict_rules}直接输出 Markdown 正文，不要代码围栏。"""

STRICT_GROUNDING_RULES = """
4. 严格 grounding 约束（最高优先级）：
   - 所有数字、百分比、专有名词必须能在研究笔记中逐字找到，禁止推测或补全；
   - 每个论断句都必须带 [n] 引用标记，找不到可用引用的论断直接舍弃不写；
   - 笔记之间信息冲突时，如实呈现双方说法，不要自行裁决。"""


def _strip_preamble(text: str) -> str:
    """兜底：小模型偶尔在正文前先"自言自语"（推理泄露），从第一个 Markdown 标题行截断。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#"):
            return "\n".join(lines[i:]).strip()
    return text.strip()


def writer(state: ResearchState) -> dict:
    notes = [n for n in state.get("notes", []) if n.get("content")]
    if not notes:
        return {"report": f"# {state['topic']}\n\n> 未能收集到有效研究资料，请检查搜索服务与网络后重试。"}

    registry, ordered = _build_registry(notes)
    notes_text = "\n\n".join(f"### 子问题：{n['subquestion']}\n{n['content']}" for n in notes)
    sources_text = "\n".join(
        f"[{registry[s['url']]}] {s['title']} - {s['url']}" for s in ordered
    )
    conflicts = state.get("conflicts") or []
    conflicts_section = (
        "事实核查员标记的冲突（报告需单列一节说明）：\n" + "\n".join(f"- {c}" for c in conflicts)
        if conflicts
        else ""
    )
    strict_rules = STRICT_GROUNDING_RULES if config.WRITER_STRICT_GROUNDING else ""

    report = _strip_preamble(
        chat(
            [{
                "role": "user",
                "content": PROMPT.format(
                    topic=state["topic"],
                    brief=state.get("brief", state["topic"]),
                    notes_text=notes_text,
                    sources_text=sources_text,
                    conflicts_section=conflicts_section,
                    strict_rules=strict_rules,
                ),
            }],
            temperature=0.4,
        )
    )

    references = "\n".join(
        f"{registry[s['url']]}. [{s['title']}]({s['url']})" for s in ordered
    )
    report = f"{report}\n\n## 参考来源\n\n{references}\n"
    return {"report": report}
