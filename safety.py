"""可选的内容安全审计钩子。

这不是合规分类器：只在显式配置 `SAFETY_JUDGE_MODEL` 时，用模型按固定
风险类别给报告一个 low/medium/high 信号，并追加到报告中供人工复核。
默认关闭，避免给每轮研究偷偷增加一次 LLM 调用。
"""
import config
from llm.client import chat_json_safe
from untrusted import GUARD, wrap

CATEGORIES = [
    "暴力或伤害指导",
    "自伤/自杀相关",
    "违法或规避执法的操作指导",
    "仇恨或骚扰",
    "未成年人不当性内容",
    "泄露个人敏感信息",
]

PROMPT = """{guard}

你是内容安全审计员。只根据报告正文判断是否存在以下风险类别：
{categories}
风险等级只能是 low / medium / high；不确定时用 medium，并说明原因。
{content}
只输出 JSON：{{"risk":"low|medium|high","categories":["..."],"reason":"简短理由"}}"""


def audit_report(report: str, topic: str = "") -> dict:
    if not config.SAFETY_JUDGE_MODEL:
        return {}
    prompt = PROMPT.format(
        guard=GUARD,
        categories="\n".join(f"- {c}" for c in CATEGORIES),
        content=wrap(f"研究主题：{topic}\n\n{report[:6000]}", source="report"),
    )
    data = chat_json_safe(
        [{"role": "user", "content": prompt}],
        fallback={},
        what="内容安全审计",
        temperature=0.0,
        model=config.SAFETY_JUDGE_MODEL or None,
    )
    if not isinstance(data, dict):
        return {"failed": True, "risk": "unknown", "categories": [], "reason": ""}
    risk = str(data.get("risk", "unknown")).lower()
    if risk not in {"low", "medium", "high"}:
        risk = "unknown"
    categories = data.get("categories")
    if not isinstance(categories, list):
        categories = []
    return {
        "risk": risk,
        "categories": [str(c)[:80] for c in categories[:6]],
        "reason": str(data.get("reason", ""))[:300],
    }


def format_section(audit: dict) -> str:
    if not audit:
        return ""
    lines = ["", "## 内容安全审计", ""]
    if audit.get("failed"):
        lines.append("- 内容安全 judge 未返回可用结果；不影响报告生成。")
    else:
        lines.append(
            f"- 风险等级：**{audit.get('risk', 'unknown')}**"
            + (f"；类别：{', '.join(audit['categories'])}" if audit.get("categories") else "")
        )
        if audit.get("reason"):
            lines.append(f"- 理由：{audit['reason']}")
    lines.append("> 这是模型辅助审计，不是合规结论；高风险内容必须人工复核并按产品政策处理。")
    return "\n".join(lines)
