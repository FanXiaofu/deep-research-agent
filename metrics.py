"""进程级运行指标：不引入 Prometheus 依赖，先提供可直接 JSON 采集的计数。"""
import threading
import time
from collections import Counter

_started_at = time.time()
_lock = threading.Lock()
_requests: Counter = Counter()
_research_started = 0
_research_finished = 0
_research_failed = 0
_active = 0
_outcomes: Counter = Counter()


def record_request(path: str, status: int) -> None:
    with _lock:
        _requests[f"{path} {status}"] += 1


def research_started() -> None:
    global _research_started, _active
    with _lock:
        _research_started += 1
        _active += 1


def research_finished(outcome: str) -> None:
    global _research_finished, _research_failed, _active
    with _lock:
        _research_finished += 1
        _active = max(0, _active - 1)
        _outcomes[outcome] += 1
        if outcome == "failed":
            _research_failed += 1


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
