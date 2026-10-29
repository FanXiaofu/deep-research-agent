"""R10：writer 出口的引用/链接确定性校验。"""
import pytest

import reliability
from citations import sanitize_citations, sanitize_links
from graph.nodes import writer as writer_mod


@pytest.fixture(autouse=True)
def _clean_degradations():
    reliability.reset_all()
    yield
    reliability.reset_all()


def test_sanitize_removes_out_of_range_numbers():
    text, removed = sanitize_citations("A [1] B [99] C [2, 99] D [3，7]", max_ref=3)

    assert "[99]" not in text
    assert "[2]" in text
    assert "[3]" in text
    assert removed == 3


def test_sanitize_removes_empty_groups_and_dedupes():
    text, removed = sanitize_citations("结论 [7] 与 [9]，保留 [1, 1]", max_ref=2)

    assert "[7]" not in text and "[9]" not in text
    assert "[1]" in text
    assert text.count("[1]") == 1
    assert removed == 3  # 7、9、重复的 1


def test_unknown_links_become_text_and_anchors_survive():
    text, removed = sanitize_links(
        "[ok](https://ok.test/) [bad](https://bad.test) [anchor](#part)",
        {"https://ok.test"},
    )

    assert "[ok](https://ok.test/)" in text
    assert "https://bad.test" not in text
    assert "bad" in text
    assert "[anchor](#part)" in text
    assert removed == 1


def test_writer_audits_bad_citations_and_links(monkeypatch):
    monkeypatch.setattr(
        writer_mod,
        "chat_safe",
        lambda *a, **k: "# 报告\n\n结论 [1] [99] [bad](https://evil.test)\n",
    )
    state = {
        "topic": "T",
        "brief": "B",
        "notes": [{
            "subquestion": "s",
            "content": "笔记",
            "sources": [{"title": "ok", "url": "https://ok.test"}],
        }],
        "conflicts": [],
    }

    out = writer_mod.writer(state)
    report = out["report"]

    assert out["citation_audit"] == {"invalid_citations": 1, "unknown_links": 1}
    assert "[1]" in report
    assert "[99]" not in report
    assert "https://evil.test" not in report
    assert "## 引用校验说明" in report
    assert "https://ok.test" in report
    assert "## 参考来源" in report


def test_writer_valid_report_has_no_audit_section(monkeypatch):
    monkeypatch.setattr(writer_mod, "chat_safe", lambda *a, **k: "# 报告\n\n结论 [1]\n")
    state = {
        "topic": "T",
        "brief": "B",
        "notes": [{
            "subquestion": "s",
            "content": "笔记",
            "sources": [{"title": "ok", "url": "https://ok.test"}],
        }],
        "conflicts": [],
    }

    out = writer_mod.writer(state)

    assert out["citation_audit"] == {"invalid_citations": 0, "unknown_links": 0}
    assert "## 引用校验说明" not in out["report"]


def test_fallback_report_is_also_sanitized(monkeypatch):
    monkeypatch.setattr(writer_mod, "chat_safe", lambda *a, **k: "")
    state = {
        "topic": "T",
        "brief": "B",
        "notes": [{
            "subquestion": "s",
            "content": "结论 [99]",
            "sources": [{"title": "ok", "url": "https://ok.test"}],
        }],
        "conflicts": [],
    }

    out = writer_mod.writer(state)

    assert out["citation_audit"]["invalid_citations"] == 1
    assert "[99]" not in out["report"]
