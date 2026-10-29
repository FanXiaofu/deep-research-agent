"""引用与正文链接的确定性校验。

writer 的编号注册表只能保证"参考来源"清单是代码生成的；模型仍可能输出
越界 `[99]` 或不属于来源清单的正文链接。这里在报告出口做一次纯函数校验，
剔除无效项并返回审计计数，避免把不可溯源的内容直接交付给用户。
"""
import re

# 与 eval/judge.py 共用同一口径：两位以内、支持 [2, 6] 与中文逗号 [2，6]
CITE_RE = re.compile(r"\[(\d{1,2}(?:\s*[,，]\s*\d{1,2})*)\]")
LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)\)")


def extract_citations(text: str) -> list[int]:
    """按出现顺序提取引用编号（复合引用拆开）。"""
    out: list[int] = []
    for match in CITE_RE.finditer(text):
        for part in re.split(r"[,，]\s*", match.group(1)):
            if part.strip().isdigit():
                out.append(int(part))
    return out


def sanitize_citations(text: str, max_ref: int) -> tuple[str, int]:
    """剔除越界编号；返回 (新文本, 移除的编号数)。"""
    removed = 0

    def repl(match: re.Match) -> str:
        nonlocal removed
        nums = [int(p) for p in re.split(r"[,，]\s*", match.group(1)) if p.strip().isdigit()]
        valid: list[int] = []
        seen: set[int] = set()
        for n in nums:
            if 1 <= n <= max_ref and n not in seen:
                valid.append(n)
                seen.add(n)
        removed += len(nums) - len(valid)
        if len(valid) == len(nums):
            return match.group(0)
        if not valid:
            return ""
        return "[" + ", ".join(str(n) for n in valid) + "]"

    return CITE_RE.sub(repl, text), removed


def _norm_url(url: str) -> str:
    return url.rstrip("/")


def sanitize_links(text: str, allowed_urls) -> tuple[str, int]:
    """把不在来源清单中的正文链接降级为纯文本（保留锚点与图片）。"""
    allowed = {_norm_url(url) for url in allowed_urls if url}
    removed = 0

    def repl(match: re.Match) -> str:
        nonlocal removed
        label, url = match.group(1), match.group(2)
        if url.startswith("#") or _norm_url(url) in allowed:
            return match.group(0)
        removed += 1
        return label

    return LINK_RE.sub(repl, text), removed


def validate_report(text: str, allowed_urls, max_ref: int) -> tuple[str, dict]:
    """先校验引用编号，再校验正文链接；返回 (新文本, 审计)。"""
    text, invalid_citations = sanitize_citations(text, max_ref)
    text, unknown_links = sanitize_links(text, allowed_urls)
    return text, {
        "invalid_citations": invalid_citations,
        "unknown_links": unknown_links,
    }
