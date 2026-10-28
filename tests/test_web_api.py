"""Web 接口的规模参数边界：越界直接 400，而不是静默降级成空报告。"""
import config
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_research_stream_rejects_over_limit_subquestions():
    resp = client.get("/api/research/stream", params={"topic": "测试主题", "max_sub": 99})
    assert resp.status_code == 400
    assert "子问题上限" in resp.json()["detail"]


def test_research_stream_rejects_zero_rounds():
    """rounds=0 会让 researcher 一次都不检索，必须在入口拦住。"""
    resp = client.get("/api/research/stream", params={"topic": "测试主题", "rounds": 0})
    assert resp.status_code == 400
    assert "检索轮数" in resp.json()["detail"]


def test_index_renders_customisable_scale_inputs():
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="max-sub"' in resp.text and 'type="number"' in resp.text
    assert f'max="{config.MAX_SUBQUESTIONS_LIMIT}"' in resp.text


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
