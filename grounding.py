"""证据支撑审计：词面覆盖 + 可选 LLM 语义 judge。

词面覆盖是零成本启发式：只说明报告句子与来源摘要/节选的关键词重合度，
**不能**据此判定事实真假；语义 judge 也需要人工复核。审计只追加说明，
不删改报告结论。
"""
import json
import re

import config
from citations import extract_citations
from llm.client import chat_json_safe
from untrusted import GUARD, wrap

_REFERENCE_SECTION = re.compile(r"\n##\s*参考来源\b.*\Z", re.S)
_SENTENCE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")
_NUMBER = re.compile(r"\d+(?:\.\d+)?%?")
_WORD = re.compile(r"[a-z0-9][a-z0-9._%+-]*")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _strip_references(report: str) -> str:
    return _REFERENCE_SECTION.sub("", report)


def _claims(report: str, limit: int) -> list[str]:
    claims: list[str] = []
    for match in _SENTENCE.finditer(_strip_references(report)):
        text = match.group(0).strip()
        if not text or text.startswith("#") or not extract_citations(text):
            continue
        claims.append(text)
        if len(claims) >= limit:
            break
    return claims


def _tokens(text: str) -> set[str]:
    words = set(_WORD.findall(text.lower()))
    chars = "".join(_CJK.findall(text))
    bigrams = {chars[i:i + 2] for i in range(len(chars) - 1)}
    return words | bigrams


def _evidence(claim: str, sources: dict) -> str:
    parts: list[str] = []
    for ref in extract_citations(claim):
        src = sources.get(ref) or {}
        parts.append(str(src.get("title", "")))
        parts.append(str(src.get("excerpt", "")))
    return "\n".join(part for part in parts if part)


def _lexical_result(claim: str, sources: dict) -> dict:
    evidence = _evidence(claim, sources)
    claim_tokens = _tokens(claim)
    overlap = claim_tokens & _tokens(evidence)
    score = len(overlap) / max(1, len(claim_tokens))
    claim_nums = set(_NUMBER.findall(claim))
    evidence_nums = set(_NUMBER.findall(evidence))
    numeric_mismatch = bool(claim_nums and not claim_nums <= evidence_nums)
    return {
        "claim": claim[:240],
        "citations": extract_citations(claim),
        "score": round(score, 3),
        "low_support": score < config.GROUNDING_LEXICAL_THRESHOLD or numeric_mismatch,
        "numeric_mismatch": numeric_mismatch,
    }


def _semantic_audit(claims: list[str], sources: dict) -> dict:
    items = [
        {
            "id": i,
            "claim": claim[:500],
            "evidence": _evidence(claim, sources)[:1600],
        }
        for i, claim in enumerate(claims, 1)
    ]
    prompt = f"""{GUARD}

你是事实核查员。只根据 evidence 判断 claim 是否被来源支持，不要用外部知识：
- supported：evidence 明确支持；
- partial：只支持一部分，或需要额外推断；
- unsupported：没有支持、与 evidence 冲突，或 evidence 为空。
{wrap(json.dumps(items, ensure_ascii=False), source="grounding_evidence")}
只输出 JSON：{{"results":[{{"id":1,"verdict":"supported|partial|unsupported","reason":"简短理由"}}]}}"""
    data = chat_json_safe(
        [{"role": "user", "content": prompt}],
        fallback={},
        what="证据支撑审计",
        temperature=0.0,
        model=config.GROUNDING_JUDGE_MODEL or None,
    )
    raw = data.get("results") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return {"failed": True, "checked": 0}

    counts = {"supported": 0, "partial": 0, "unsupported": 0, "uncertain": 0}
    details = []
    by_id = {str(item.get("id")): item for item in raw if isinstance(item, dict)}
    for i, claim in enumerate(claims, 1):
        item = by_id.get(str(i), {})
        verdict = str(item.get("verdict", "uncertain")).lower()
        if verdict not in counts:
            verdict = "uncertain"
        counts[verdict] += 1
        details.append({
            "id": i,
            "verdict": verdict,
            "reason": str(item.get("reason", ""))[:240],
            "claim": claim[:240],
        })
    return {"checked": len(claims), **counts, "details": details}


def audit_report(report: str, sources: dict, *, use_semantic: bool | None = None) -> dict:
    if not config.GROUNDING_AUDIT_ENABLED:
        return {}
    limit = config.GROUNDING_MAX_CLAIMS
    if limit <= 0:
        return {}
    claims = _claims(report, limit)
    if not claims:
        return {"claims": 0, "low_support": [], "numeric_mismatch": 0, "semantic": None}

    lexical = [_lexical_result(claim, sources) for claim in claims]
    semantic = None
    enabled = bool(config.GROUNDING_JUDGE_MODEL) if use_semantic is None else use_semantic
    if enabled:
        semantic = _semantic_audit(claims, sources)
    return {
        "claims": len(claims),
        "low_support": [item for item in lexical if item["low_support"]],
        "numeric_mismatch": sum(1 for item in lexical if item["numeric_mismatch"]),
        "semantic": semantic,
    }


def format_section(audit: dict) -> str:
    if not audit:
        return ""
    low = audit.get("low_support") or []
    semantic = audit.get("semantic")
    lines = ["", "## 证据支撑审计", ""]
    lines.append(
        f"- 检查 {audit.get('claims', 0)} 条带引用论断；"
        f"词面覆盖低于阈值 {len(low)} 条，数字未在来源中找到 {audit.get('numeric_mismatch', 0)} 条。"
    )
    if semantic:
        if semantic.get("failed"):
            lines.append("- 语义 judge 未返回可用结果（不影响报告生成）。")
        else:
            lines.append(
                "- 语义判定：supported {supported} / partial {partial} / "
                "unsupported {unsupported} / uncertain {uncertain}。".format(**semantic)
            )
    for item in low[:3]:
        lines.append(
            f"- 低覆盖：引用 {item['citations']}，覆盖率 {item['score']}：{item['claim']}"
        )
    lines.append("> 词面覆盖/模型判定只是审计信号，不等同于事实裁决；关键结论请人工复核原始来源。")
    return "\n".join(lines)
