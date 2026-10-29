"""D1：报告 Markdown → HTML 后必须走白名单清洗。"""
from app.main import render_report_html


def test_scripts_and_event_handlers_are_removed():
    html = render_report_html(
        "# 标题\n\n<script>alert(1)</script>\n<img src=x onerror=alert(2)>\n"
    )

    assert "<script" not in html.lower()
    assert "onerror" not in html.lower()
    assert "<img" in html  # 图片本身保留，只去掉危险属性


def test_javascript_links_are_removed():
    html = render_report_html("[x](javascript:alert(1))")

    assert "javascript:" not in html.lower()


def test_safe_links_are_kept_with_rel():
    html = render_report_html("[x](https://example.com)")

    assert 'href="https://example.com"' in html
    assert "noopener noreferrer" in html
