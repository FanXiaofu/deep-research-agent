"""Web 接口的规模参数边界与访问控制。"""
import config
from fastapi.testclient import TestClient

from app import security
from app.main import app

client = TestClient(app)


def test_research_stream_rejects_over_limit_subquestions():
    resp = client.post("/api/research/stream", json={"topic": "测试主题", "max_sub": 99})
    assert resp.status_code == 400
    assert "子问题上限" in resp.json()["detail"]


def test_research_stream_rejects_zero_rounds():
    """rounds=0 会让 researcher 一次都不检索，必须在入口拦住。"""
    resp = client.post("/api/research/stream", json={"topic": "测试主题", "rounds": 0})
    assert resp.status_code == 400
    assert "检索轮数" in resp.json()["detail"]


def test_research_stream_is_post_only():
    assert client.get("/api/research/stream").status_code == 405


def test_index_renders_customisable_scale_inputs():
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="max-sub"' in resp.text and 'type="number"' in resp.text
    assert f'max="{config.MAX_SUBQUESTIONS_LIMIT}"' in resp.text
    assert 'id="auth-token"' in resp.text


def test_oversized_body_is_rejected():
    big = "x" * (config.APP_MAX_REQUEST_BYTES + 10)

    resp = client.post("/api/research/stream", json={"topic": big})

    assert resp.status_code == 413


def _healthy_local(monkeypatch):
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setattr(config, "LLM_MODEL", "qwen2.5:7b-instruct")
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.setattr(config, "LLM_NATIVE_OLLAMA", True)
    monkeypatch.setattr(config, "SEARCH_PROVIDER", "duckduckgo")
    monkeypatch.setattr(config, "SEARCH_API_KEY", "")


def test_healthz_ok(monkeypatch):
    _healthy_local(monkeypatch)

    resp = client.get("/healthz")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["errors"] == []


def test_healthz_reports_config_errors(monkeypatch):
    _healthy_local(monkeypatch)
    monkeypatch.setattr(config, "SEARCH_PROVIDER", "tavily")

    resp = client.get("/healthz")

    assert resp.status_code == 503
    assert any("SEARCH_API_KEY" in e for e in resp.json()["errors"])


def test_new_thread_id_is_unique():
    from app.main import _new_thread_id

    ids = {_new_thread_id() for _ in range(100)}

    assert len(ids) == 100


def test_report_event_uses_current_run_stats():
    import json

    import run_context
    from app.main import _report_event
    from llm.client import USAGE

    with run_context.run_scope("stats-run"):
        USAGE.add(12, 4)
        raw = _report_event({"report": "# 报告"}, 1.0)

    payload = json.loads(raw.split("data: ", 1)[1])
    assert payload["stats"]["llm_calls"] == 1
    assert payload["stats"]["input_tokens"] == 12
    assert payload["stats"]["output_tokens"] == 4


def test_report_requires_token(monkeypatch):
    monkeypatch.setattr(config, "APP_AUTH_TOKEN", "secret")

    assert client.get("/api/report/unknown").status_code == 401

    ok = client.get("/api/report/unknown", headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200
    assert ok.json()["report"] == ""


def test_research_stream_requires_token(monkeypatch):
    monkeypatch.setattr(config, "APP_AUTH_TOKEN", "secret")

    assert client.post("/api/research/stream", json={"topic": "测试主题"}).status_code == 401

    allowed = client.post(
        "/api/research/stream",
        json={"topic": "测试主题", "max_sub": 99},
        headers={"Authorization": "Bearer secret"},
    )
    assert allowed.status_code == 400  # 认证通过后仍由规模校验拦住


def test_rate_limit_returns_429(monkeypatch):
    monkeypatch.setattr(config, "APP_RATE_LIMIT_PER_MINUTE", 1)
    security.reset_limiter()

    first = client.post("/api/research/stream", json={"topic": "测试主题", "max_sub": 99})
    second = client.post("/api/research/stream", json={"topic": "测试主题", "max_sub": 99})

    assert first.status_code == 400
    assert second.status_code == 429
    security.reset_limiter()


class _FakeGraphApp:
    def stream(self, payload, cfg, stream_mode="updates"):
        yield {"writer": {}}

    def get_state(self, cfg):
        from types import SimpleNamespace

        return SimpleNamespace(values={"report": "# 测试报告", "subquestions": [], "notes": []})


def test_research_stream_accepts_post(monkeypatch):
    import app.main as app_main

    monkeypatch.setattr(app_main, "get_research_app", lambda: _FakeGraphApp())
    resp = client.post("/api/research/stream", json={"topic": "测试主题", "points": []})

    assert resp.status_code == 200
    assert '"type": "report"' in resp.text
    assert "测试报告" in resp.text


def test_resume_stream_accepts_post(monkeypatch):
    import app.main as app_main

    monkeypatch.setattr(app_main, "get_research_app", lambda: _FakeGraphApp())
    resp = client.post("/api/resume/stream", json={"thread_id": "web-test", "value": ""})

    assert resp.status_code == 200
    assert '"type": "report"' in resp.text
