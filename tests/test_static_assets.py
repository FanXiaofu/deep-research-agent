"""P1.5：前端资源外置，CSP 可以去掉 unsafe-inline。"""
from fastapi.testclient import TestClient

from app.main import app


def test_index_has_no_inline_scripts_or_handlers():
    html = TestClient(app).get("/").text
    assert "/static/app.css" in html
    assert "/static/app.js" in html
    assert "<style" not in html
    assert "onclick=" not in html
    assert "oninput=" not in html
    assert "style=" not in html
    assert 'data-max-llm-calls="' in html


def test_static_assets_are_served():
    client = TestClient(app)
    js = client.get("/static/app.js")
    css = client.get("/static/app.css")
    assert js.status_code == 200
    assert css.status_code == 200
    assert "addEventListener" in js.text
    assert "{{" not in js.text
    assert "unsafe-inline" not in js.text
