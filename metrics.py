"""进程级运行指标：JSON 快照 + Prometheus 文本指标。"""
import threading
import time
from collections import Counter

from prometheus_client import Counter as PCounter
from prometheus_client import Gauge, generate_latest

_started_at = time.time()
_lock = threading.Lock()
_requests: Counter = Counter()
_research_started = 0
_research_finished = 0
_research_failed = 0
_active = 0
_outcomes: Counter = Counter()

HTTP_REQUESTS = PCounter(
    "dr_http_requests_total", "HTTP requests by route and status", ["path", "status"]
)
RESEARCH_STARTED = PCounter("dr_research_started_total", "Research runs started")
RESEARCH_FINISHED = PCounter(
    "dr_research_finished_total", "Research runs finished by outcome", ["outcome"]
)
RESEARCH_ACTIVE = Gauge("dr_research_active", "Currently running research runs")


def record_request(path: str, status: int) -> None:
    with _lock:
        _requests[f"{path} {status}"] += 1
    HTTP_REQUESTS.labels(path=path, status=str(status)).inc()


def research_started() -> None:
    global _research_started, _active
    with _lock:
        _research_started += 1
        _active += 1
    RESEARCH_STARTED.inc()
    RESEARCH_ACTIVE.inc()


def research_finished(outcome: str) -> None:
    global _research_finished, _research_failed, _active
    with _lock:
        _research_finished += 1
        _active = max(0, _active - 1)
        _outcomes[outcome] += 1
        if outcome == "failed":
            _research_failed += 1
    RESEARCH_FINISHED.labels(outcome=outcome).inc()
    RESEARCH_ACTIVE.dec()


def render() -> str:
    return generate_latest().decode("utf-8")


def snapshot() -> dict:
    with _lock:
        return {
            "uptime_s": round(time.time() - _started_at, 1),
            "requests": dict(_requests),
            "research": {
                "started": _research_started,
                "finished": _research_finished,
                "failed": _research_failed,
                "active": _active,
                "outcomes": dict(_outcomes),
            },
        }


def reset() -> None:
    global _started_at, _research_started, _research_finished, _research_failed, _active
    with _lock:
        _started_at = time.time()
        _requests.clear()
        _outcomes.clear()
        _research_started = _research_finished = _research_failed = _active = 0
