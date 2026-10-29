"""P1.7：可选内容安全审计钩子（默认关闭）。"""
import config
import safety


def test_safety_disabled_without_model(monkeypatch):
    monkeypatch.setattr(config, "SAFETY_JUDGE_MODEL", "")
    assert safety.audit_report("正常报告", "主题") == {}
    assert safety.format_section({}) == ""


def test_safety_high_risk_section(monkeypatch):
    monkeypatch.setattr(config, "SAFETY_JUDGE_MODEL", "safety-judge")
    monkeypatch.setattr(
        safety,
        "chat_json_safe",
        lambda *args, **kwargs: {
            "risk": "high",
            "categories": ["违法或规避执法的操作指导"],
            "reason": "包含具体规避步骤",
        },
    )
    audit = safety.audit_report("报告正文", "主题")
    section = safety.format_section(audit)
    assert audit["risk"] == "high"
    assert "内容安全审计" in section
    assert "high" in section
    assert "人工复核" in section
