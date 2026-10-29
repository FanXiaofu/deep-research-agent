"""P1.6：证据支撑审计（词面覆盖 + 可选语义 judge）。"""
import config
import grounding
from graph.nodes import writer


def _sources():
    return {
        1: {"title": "苹果营养", "url": "https://example.com/apple",
            "excerpt": "苹果 富含 维生素C 和 膳食纤维"},
        2: {"title": "火星表面", "url": "https://example.com/mars",
            "excerpt": "火星 表面 干燥 寒冷 没有 液态水"},
    }


def test_lexical_audit_flags_low_support_and_numbers(monkeypatch):
    monkeypatch.setattr(config, "GROUNDING_JUDGE_MODEL", "")
    report = (
        "苹果富含维生素C [1]。\n"
        "火星表面有 95% 的液态水 [2]。\n\n"
        "## 参考来源\n\n1. [苹果](https://example.com/apple)\n"
    )
    audit = grounding.audit_report(report, _sources(), use_semantic=False)
    assert audit["claims"] == 2
    assert audit["low_support"]
    assert audit["numeric_mismatch"] >= 1
    section = grounding.format_section(audit)
    assert "证据支撑审计" in section
    assert "低覆盖" in section
    assert "人工复核" in section


def test_semantic_judge_counts(monkeypatch):
    monkeypatch.setattr(config, "GROUNDING_JUDGE_MODEL", "judge-model")
    monkeypatch.setattr(
        grounding,
        "chat_json_safe",
        lambda *args, **kwargs: {
            "results": [
                {"id": 1, "verdict": "supported", "reason": "摘要包含维生素C"},
                {"id": 2, "verdict": "unsupported", "reason": "来源说没有液态水"},
            ]
        },
    )
    report = "苹果富含维生素C [1]。\n火星有液态水 [2]。"
    audit = grounding.audit_report(report, _sources(), use_semantic=True)
    assert audit["semantic"]["supported"] == 1
    assert audit["semantic"]["unsupported"] == 1
    assert "语义判定" in grounding.format_section(audit)


def test_grounding_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "GROUNDING_AUDIT_ENABLED", False)
    assert grounding.audit_report("有引用 [1]。", _sources()) == {}


def test_writer_appends_grounding_audit(monkeypatch):
    monkeypatch.setattr(config, "GROUNDING_AUDIT_ENABLED", True)
    monkeypatch.setattr(config, "GROUNDING_JUDGE_MODEL", "")
    monkeypatch.setattr(writer, "chat_safe", lambda *args, **kwargs: "苹果富含维生素C [1]。")
    state = {
        "topic": "水果",
        "notes": [{
            "subquestion": "苹果",
            "content": "苹果富含维生素C [1]。",
            "sources": [{
                "title": "苹果营养",
                "url": "https://example.com/apple",
                "excerpt": "苹果 富含 维生素C",
            }],
        }],
    }
    out = writer.writer(state)
    assert out["grounding_audit"]["claims"] == 1
    assert "## 证据支撑审计" in out["report"]
    assert "## 参考来源" in out["report"]
