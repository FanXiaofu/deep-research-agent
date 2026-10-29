"""P1.2：Redis 共享限流与指标（无 TEST_REDIS_URL 时跳过）。"""
import os
import uuid

import pytest
from fastapi import HTTPException

import config
import metrics
from app import security

TEST_URL = os.getenv("TEST_REDIS_URL", "")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_REDIS_URL 未配置")


@pytest.fixture(autouse=True)
def _redis(monkeypatch):
    monkeypatch.setattr(config, "REDIS_URL", TEST_URL)
    security.reset_limiter()
    metrics.reset()
    yield
    metrics.reset()
    security.reset_limiter()


def test_redis_rate_limit_is_shared_and_scoped():
    limiter = security.RedisRateLimiter(2, TEST_URL)
    key = f"test-{uuid.uuid4().hex}"

    limiter.check(key)
    limiter.check(key)
    with pytest.raises(HTTPException) as blocked:
        limiter.check(key)
    assert blocked.value.status_code == 429
    limiter.check(key + "-other")


def test_redis_metrics_are_aggregated():
    metrics.record_request("/api/test", 200)
    metrics.research_started()
    metrics.research_finished("completed")

    snapshot = metrics.snapshot()
    assert snapshot["requests"]["/api/test 200"] == 1
    assert snapshot["research"]["started"] == 1
    assert snapshot["research"]["finished"] == 1
    assert snapshot["research"]["active"] == 0
    assert snapshot["research"]["outcomes"] == {"completed": 1}

    text = metrics.render()
    assert 'dr_http_requests_total{path="/api/test",status="200"} 1' in text
    assert "dr_research_started_total 1" in text
    assert 'dr_research_finished_total{outcome="completed"} 1' in text
