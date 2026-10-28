"""容错层：重试、熔断开合、降级留痕。"""
import time

import pytest

import config
import reliability


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(config, "BREAKER_FAILURE_THRESHOLD", 3)
    monkeypatch.setattr(config, "BREAKER_COOLDOWN_S", 60.0)
    reliability.reset_all()
    yield
    reliability.reset_all()


def test_retry_recovers_from_transient_failure():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("瞬时错误")
        return "ok"

    assert reliability.call_with_retry(flaky, attempts=2, delay_s=0) == "ok"
    assert calls["n"] == 2


def test_retry_raises_after_exhausting_attempts():
    def always_fail():
        raise RuntimeError("一直失败")

    with pytest.raises(RuntimeError):
        reliability.call_with_retry(always_fail, attempts=2, delay_s=0)


def test_guarded_degrades_without_raising_and_records_event():
    result, degraded = reliability.call_guarded(
        "search", lambda: (_ for _ in ()).throw(RuntimeError("额度耗尽")),
        attempts=1, default=[], what="搜索",
    )
    assert result == [] and degraded is True
    events = reliability.degradations()
    assert events and events[-1]["component"] == "search"


def test_breaker_opens_then_half_opens_after_cooldown(monkeypatch):
    monkeypatch.setattr(config, "BREAKER_COOLDOWN_S", 0.05)
    breaker = reliability.get_breaker("search")

    for _ in range(3):
        breaker.record_failure()
    assert breaker.state() == "open"
    assert breaker.allow() is False  # open 期间直接短路，不再打外部服务

    time.sleep(0.06)
    assert breaker.allow() is True  # 冷却结束放行探活
    assert breaker.state() == "half-open"


def test_half_open_probe_failure_reopens_the_breaker(monkeypatch):
    monkeypatch.setattr(config, "BREAKER_COOLDOWN_S", 0.05)
    breaker = reliability.get_breaker("search")
    for _ in range(3):
        breaker.record_failure()
    time.sleep(0.06)
    breaker.allow()
    breaker.record_failure()  # 探活失败

    assert breaker.state() == "open"
    assert breaker.allow() is False


def test_half_open_probe_success_closes_the_breaker(monkeypatch):
    monkeypatch.setattr(config, "BREAKER_COOLDOWN_S", 0.05)
    breaker = reliability.get_breaker("search")
    for _ in range(3):
        breaker.record_failure()
    time.sleep(0.06)
    breaker.allow()
    breaker.record_success()

    assert breaker.state() == "closed"
    assert breaker.allow() is True


def test_breaker_closes_after_success():
    breaker = reliability.get_breaker("fetch")
    for _ in range(2):
        breaker.record_failure()
    breaker.record_success()
    assert breaker.state() == "closed"
    assert breaker.allow() is True


def test_degradation_log_is_capped():
    for i in range(80):
        reliability.record_degradation("llm", f"失败{i}")
    assert len(reliability.degradations()) == 50
