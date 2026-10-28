"""compactor：笔记正文超阈值时先压缩，再交给 verifier / writer 消费。

动机：并行分支的笔记量随子问题数线性增长，长流程（含重规划新增分支）下会把
verifier 与 writer 的上下文顶到模型窗口上限 —— 本地 7B 模型尤其明显（曾因
num_ctx 截断产生空回复）。压缩把正文压到目标字数，但**来源列表（sources）原样保留**：
writer 的引用编号注册表是从 sources 构建的，因此压缩不会影响引用编号的有效性，
只影响正文信息密度。

两级策略：
1. LLM 压缩：保留数字/专有名词/条件与矛盾表述，禁止新增信息；
2. 确定性兜底：LLM 不可用或漏掉某条笔记时，按目标字数截断（保头部），并标注省略字数。

产物写入 notes_digest（不覆盖 notes 原文），消费侧统一走 state.effective_notes()。
"""
import config
import reliability
from graph.state import ResearchState
from llm.client import chat_json_safe


PROMPT = """你是研究资料压缩员。研究主题：{topic}

下面是 {n} 条研究笔记（共约 {before} 字），请把每条压缩到 {target} 字左右，必须保留：
- 所有数字、百分比、版本号、专有名词；
- 结论的因果与适用条件（"在 X 情况下是 Y"不能压成"Y"）；
- 相互矛盾的说法都要保留，不要替我做裁决。
不得新增笔记中没有的信息，也不要添加评论。子问题字段原样照抄。
以 JSON 输出：{{"notes": [{{"subquestion": "原样照抄", "content": "压缩后正文"}}]}}"""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n…（压缩省略 {len(text) - limit} 字）"


def _digest_from_llm(topic: str, notes: list[dict], target_per_note: int) -> tuple[list[dict], int]:
    """LLM 压缩并按 subquestion 回填；返回 (压缩视图, LLM 成功压缩的条数)。"""
    data = chat_json_safe(
        [{
            "role": "user",
            "content": PROMPT.format(
                topic=topic,
                n=len(notes),
                before=sum(len(n.get("content", "")) for n in notes),
                target=target_per_note,
            ),
        }],
        temperature=0.1,
        fallback={},
        what="笔记压缩",
    )
    compressed = {str(item.get("subquestion", "")).strip(): str(item.get("content", "")).strip()
                  for item in data.get("notes", []) if isinstance(item, dict)}
    digest, hits = [], 0
    for note in notes:
        body = compressed.get(str(note.get("subquestion", "")).strip(), "")
        if body:
            hits += 1
        else:  # 该条未被压缩（模型漏项或整体失败）：确定性截断兜底
            body = _truncate(note.get("content", ""), target_per_note)
        digest.append({**note, "content": body})
    return digest, hits


def compactor(state: ResearchState) -> dict:
    if not config.COMPACTION_ENABLED:
        return {}
    notes = [n for n in (state.get("notes") or []) if n.get("content")]
    before = sum(len(n.get("content", "")) for n in notes)
    if not notes or before < config.COMPACT_TRIGGER_CHARS:
        # 未触发压缩：清空压缩视图，消费侧自动回落到 notes 原文
        return {"notes_digest": [], "compaction": {"applied": False, "before_chars": before}}

    target_per_note = max(200, config.COMPACT_TARGET_CHARS // len(notes))
    digest, hits = _digest_from_llm(state.get("topic", ""), notes, target_per_note)
    after = sum(len(n.get("content", "")) for n in digest)
    reliability.record_degradation(
        "compaction", f"笔记 {before}→{after} 字（{len(notes)} 条，LLM 压缩 {hits} 条）"
    )
    return {
        "notes_digest": digest,
        "compaction": {
            "applied": True,
            "before_chars": before,
            "after_chars": after,
            "ratio": round(before / after, 2) if after else None,
            "n_notes": len(notes),
            "llm_compressed": hits,
            "target_per_note": target_per_note,
        },
    }
