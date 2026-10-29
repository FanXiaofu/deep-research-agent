"""E2：结构化日志、request_id 透传与异常堆栈。"""
import json
import logging
import time

from fastapi.testclient import TestClient

import app.main as app_main
import logging_config
from app.main import app

client = TestClient(app)


class _FailingApp:
    def stream(self, payload, cfg, stream_mode="updates"):
        raise RuntimeError("boom")

    def get_state(self, cfg):  # pragma: no cover - 不会走到
        raise AssertionError("should not be called")


def test_request_id_is_echoed():
    resp = client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert resp.status_code == 200
    assert resp.headers["x-request-id"] == "abc-123"


def test_invalid_request_id_is_replaced():
    resp = client.get("/healthz", headers={"X-Request-ID": "bad id!!"})
    assert resp.status_code == 200
    rid = resp.headers["x-request-id"]
    assert rid != "bad id!!"
    assert len(rid) == 12


def test_json_formatter_includes_context_and_extras():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    record.path = "/api/test"

    with logging_config.request_scope("req-1"), logging_config.thread_scope("thread-1"):
        payload = json.loads(logging_config.JsonFormatter().format(record))

    assert payload["message"] == "hello world"
    assert payload["request_id"] == "req-1"
    assert payload["thread_id"] == "thread-1"
    assert payload["path"] == "/api/test"


def test_request_log_record_carries_request_id(caplog):
    with caplog.at_level(logging.INFO, logger="deep_research.web"):
        client.get("/", headers={"X-Request-ID": "req-abc"})

    records = [r for r in caplog.records if r.msg == "http_request"]
    assert records
    assert records[-1].request_id == "req-abc"


def test_stream_failure_logs_exception(monkeypatch, caplog):
    monkeypatch.setattr(app_main, "get_research_app", lambda: _FailingApp())

    with caplog.at_level(logging.ERROR, logger="deep_research.web"):
        events = list(
            app_main._stream(
                {},
                {"configurable": {"thread_id": "log-test"}},
                time.time(),
                request_id="req-log",
            )
        )

    assert any('"type": "error"' in event for event in events)
    assert "research_failed" in caplog.text
    assert "RuntimeError" in caplog.text
    failures = [r for r in caplog.records if r.msg == "research_failed"]
    assert failures and failures[-1].thread_id == "log-test"
