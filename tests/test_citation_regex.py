"""评测工具自身的边界用例：引用正则与确定性校验。

这组用例的由来是一次真实的翻车：早期的引用正则只匹配单个 `[n]`，
把 `[2, 6]` 这类**复合引用**判成"不存在"，于是"整篇都是复合引用"的报告
被算成零引用、整题得 0 分——而且它系统性压低了强化 grounding 那一组的表现
（那种提示词鼓励"多个片段共同支撑一个结论"）。修正后靠 `--recompute` 重算历史批次。

教训落成规则：**确定性指标必须用边界用例自测**。这个文件就是那条规则。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))

from judge import _CITE_RE, _extract_citations, citation_checks  # noqa: E402


def test_single_citation():
    assert _extract_citations("结论 [1]") == [1]


def test_compound_citation_halfwidth():
    assert _extract_citations("多个片段共同支撑 [2, 6]") == [2, 6]


def test_compound_citation_fullwidth_comma():
    """中文逗号是模型最常见的中文写法，早期就是它被漏掉。"""
    assert _extract_citations("结论 [2，6，12]") == [2, 6, 12]


def test_adjacent_citation_groups():
    assert _extract_citations("先说 A [1][3] 再说 B [2]") == [1, 3, 2]


def test_no_citation_returns_empty():
    assert _extract_citations("这篇报告没有任何引用标记") == []


def test_three_digit_number_is_not_citation():
    """`[123]` 超出引用编号的合理范围（两位以内），不应被当成引用。"""
    assert _extract_citations("版本 [123] 发布") == []


def test_citation_validity_zero_when_all_out_of_range():
    result = citation_checks("结论 [7] 与 [9]\n\n## 参考来源\n\n1. [a](http://a)", n_refs=3)
    assert result["n_citations"] == 2
    assert result["citation_validity"] == 0.0


def test_citation_validity_counts_compound_as_valid():
    """回归用例：修复前这条会算出 0（复合引用被当成不存在）。"""
    result = citation_checks("结论 [1, 3]\n\n## 参考来源\n\n1. [a](http://a)", n_refs=3)
    assert result["n_citations"] == 2
    assert result["citation_validity"] == 1.0


def test_reference_block_is_excluded_from_counts():
    """参考来源清单里的编号是代码生成的，不算正文引用。"""
    report = "结论 [1]\n\n## 参考来源\n\n1. [a](http://a)\n2. [b](http://b)"
    result = citation_checks(report, n_refs=2)
    assert result["n_citations"] == 1
    assert result["source_utilization"] == 0.5


def test_source_utilization_deduplicates_sources():
    result = citation_checks("先说 [1]，再说 [1]，最后 [2]\n\n## 参考来源\n\n1. [a](http://a)\n2. [b](http://b)", n_refs=2)
    assert result["n_citations"] == 3
    assert result["source_utilization"] == 1.0


def test_no_citations_scores_zero():
    result = citation_checks("整篇没有任何引用", n_refs=3)
    assert result["n_citations"] == 0
    assert result["citation_validity"] == 0.0
    assert result["source_utilization"] == 0.0


def test_regex_does_not_match_bare_brackets():
    assert _CITE_RE.findall("[abc] [ ] [1x]") == []
