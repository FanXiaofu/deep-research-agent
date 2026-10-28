"""LLM-as-judge：对研究报告做三维评分（忠实度 / 覆盖度 / 引用质量）+ 必覆盖要点覆盖率 + 引用硬校验。

覆盖度评分会带上评测集的 must_cover 清单（此前该字段只存在于数据里、从未参与评分），
并把"未覆盖要点"换算成 must_cover_coverage（0~1）——把主观打分降级为可逐条核对的比对。

说明：judge 与研究共用 config.LLM_MODEL。小模型当 judge 有系统性偏差，
分数主要用于 A/B 相对对比；对外报告的绝对分数建议用更强模型复评。
"""
import re

from llm.client import chat_json

FAITHFULNESS_PROMPT = """你是严格的事实核查评审员。

研究报告正文：
{report}

撰写该报告所依据的研究笔记（唯一合法的事实来源）：
{notes}

请评估：报告中的论断（尤其是数字、百分比、专有名词）是否都能在研究笔记中找到依据。
以 JSON 输出：{{"score": 整数1~5, "unsupported_claims": ["报告中无笔记依据的具体论断，含数字的优先", ...]}}
评分标准：5=所有关键论断均有笔记依据；4=个别衔接性表述无依据；3=<20% 论断无依据；2=20%~40% 无依据；1=大量论断无依据或明显编造。"""

COVERAGE_PROMPT = """你是评审员。

研究任务书：{brief}
规划出的子问题：
{subquestions}

本题的必覆盖要点清单（逐条判断报告是否讲到）：
{must_cover}

研究报告正文：
{report}

请评估：报告是否完整覆盖了所有子问题、任务书要求的维度以及上面的必覆盖要点，有无明显遗漏或偏题。
以 JSON 输出：{{"score": 整数1~5, "missing": ["遗漏的维度或问题", ...], "uncovered_points": ["未覆盖的必覆盖要点，原样照抄清单里的文字"]}}
评分标准：5=全部覆盖且论述充分；3=覆盖大部分但有明显薄弱项；1=遗漏多个子问题。"""

CITATION_PROMPT = """你是引用规范评审员。

报告正文（含 [n] 引用标记）：
{report}

参考来源清单：
{refs}

请评估：[n] 标记是否与来源清单对应、标注位置是否合理（关键论断是否挂了引用、是否存在明显把论断挂到不相关来源的情况）。
以 JSON 输出：{{"score": 整数1~5, "issues": ["具体问题", ...]}}
评分标准：5=引用位置准确、覆盖所有关键论断；3=引用基本正确但有明显漏挂或挂错；1=引用混乱或形同虚设。"""


# 引用标记：[1] 以及复合写法 [2, 6, 12] / [2，6]（中文逗号）
# 早期版本只匹配单个 [n]，会把复合引用误判成"无引用"，系统性低估 strict grounding 的效果
_CITE_RE = re.compile(r"\[(\d{1,2}(?:\s*[,，]\s*\d{1,2})*)\]")


def _extract_citations(text: str) -> list[int]:
    out: list[int] = []
    for m in _CITE_RE.finditer(text):
        for part in re.split(r"[,，]\s*", m.group(1)):
            if part.strip().isdigit():
                out.append(int(part))
    return out


def citation_checks(report: str, n_refs: int) -> dict:
    """确定性校验（不依赖 LLM）：正文 [n] 是否越界、来源利用率。"""
    body = report.split("## 参考来源")[0]
    cited = _extract_citations(body)
    valid = [n for n in cited if 1 <= n <= n_refs]
    return {
        "n_citations": len(cited),
        "citation_validity": round(len(valid) / len(cited), 3) if cited else 0.0,
        "source_utilization": round(len(set(valid)) / n_refs, 3) if n_refs else 0.0,
    }


def _normalize_point(text: str) -> str:
    return re.sub(r"[\s，。；、,.;：:（）()/]+", "", str(text)).lower()


def match_uncovered(points: list[str], reported: list[str]) -> list[str]:
    """把 judge 自报的 uncovered_points 与必覆盖清单对齐（双向包含即认为命中）。

    judge 可能会改写措辞，所以不做严格字符串相等；对齐后再算 must_cover_coverage，
    避免"清单里有 3 条、judge 只报了 1 条"时把覆盖率算成 1.0。
    """
    reported_norm = [_normalize_point(r) for r in reported if str(r).strip()]
    missed = []
    for point in points:
        norm = _normalize_point(point)
        if not norm:
            continue
        hit = any(norm in r or r in norm for r in reported_norm)
        if hit:
            missed.append(point)
    return missed


def _score(data: dict) -> int:
    try:
        return max(1, min(5, int(data.get("score", 3))))
    except (TypeError, ValueError):
        return 0  # judge 输出解析失败


def judge_report(
    question: str,
    brief: str,
    subquestions: list[str],
    report: str,
    notes: list[dict],
    n_refs: int,
    must_cover: list[str] | None = None,
) -> dict:
    body = report.split("## 参考来源")[0]
    must_cover = [str(x).strip() for x in (must_cover or []) if str(x).strip()]
    notes_text = "\n\n".join(
        f"### 子问题：{n.get('subquestion', '')}\n{n.get('content', '')}" for n in notes if n.get("content")
    ) or "（无笔记）"
    refs_match = re.search(r"## 参考来源\n\n(.+)$", report, re.DOTALL)
    refs = refs_match.group(1).strip() if refs_match else "（未找到参考来源）"

    result: dict = {"question": question}
    try:
        data = chat_json(
            [{"role": "user", "content": FAITHFULNESS_PROMPT.format(report=body, notes=notes_text)}],
            temperature=0.0,
        )
        result["faithfulness"] = _score(data)
        result["unsupported_claims"] = data.get("unsupported_claims", [])[:5]
    except Exception as exc:
        result["faithfulness"] = None
        result["judge_error"] = f"faithfulness: {type(exc).__name__}: {exc}"

    try:
        data = chat_json(
            [{
                "role": "user",
                "content": COVERAGE_PROMPT.format(
                    brief=brief,
                    subquestions="\n".join(f"- {s}" for s in subquestions),
                    must_cover="\n".join(f"- {x}" for x in must_cover) or "（本题未定义必覆盖要点）",
                    report=body,
                ),
            }],
            temperature=0.0,
        )
        result["coverage"] = _score(data)
        result["missing"] = data.get("missing", [])[:5]
        if must_cover:
            missed = match_uncovered(must_cover, data.get("uncovered_points", []) or [])
            result["must_cover_total"] = len(must_cover)
            result["must_cover_miss"] = len(missed)
            result["uncovered_points"] = missed
            result["must_cover_coverage"] = round(1 - len(missed) / len(must_cover), 3)
    except Exception as exc:
        result["coverage"] = None
        if must_cover:
            result["must_cover_coverage"] = None
        result["judge_error"] = result.get("judge_error", "") + f" coverage: {exc}"

    try:
        data = chat_json(
            [{"role": "user", "content": CITATION_PROMPT.format(report=body, refs=refs)}],
            temperature=0.0,
        )
        result["citation_quality"] = _score(data)
    except Exception as exc:
        result["citation_quality"] = None
        result["judge_error"] = result.get("judge_error", "") + f" citation: {exc}"

    result.update(citation_checks(report, n_refs))
    return result
