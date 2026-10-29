"""D3：进程级运行指标。"""
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.main as app_main
import metrics
from app.main import app

client = TestClient(app)


class _FakeApp:
    def stream(self, payload, cfg, stream_mode="updates"):
        yield {"writer": {}}

    def get_state(self, cfg):
        return SimpleNamespace(values={"report": "# ok", "subquestions": [], "notes": []})


def setup_function(_func):
    metrics.reset()


def test_metrics_endpoint_reports_requests():
    assert client.get("/metrics.json").status_code == 200
    client.get("/")

    snapshot = metrics.snapshot()

    assert snapshot["requests"]["/metrics.json 200"] == 1
    assert snapshot["requests"]["/ 200"] == 1
    assert snapshot["uptime_s"] >= 0


def test_prometheus_metrics_text():
    resp = client.get("/metrics")

    assert resp.status_code == 200
    assert "dr_http_requests_total" in resp.text
    assert "dr_research_active" in resp.text


def test_research_metrics_count_start_and_completion(monkeypatch):
    monkeypatch.setattr(app_main, "get_research_app", lambda: _FakeApp())

    list(app_main._stream({}, {"configurable": {"thread_id": "metrics-run"}}, time.time()))

    research = metrics.snapshot()["research"]
    assert research["started"] == 1
    assert research["finished"] == 1
    assert research["failed"] == 0
    assert research["active"] == 0
    assert research["outcomes"] == {"completed": 1}
