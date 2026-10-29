"""运行指标：本地 Prometheus 客户端 + 可选 Redis 共享计数。

- 未配置 REDIS_URL：进程内计数，适合单实例；
- 配置 REDIS_URL：请求/研究计数写入 Redis，`/metrics` 从 Redis 汇总，
  多实例共享同一份指标；Redis 故障时自动回退本地计数并留日志。
"""
import logging
import threading
import time
from collections import Counter

from prometheus_client import Counter as PCounter
from prometheus_client import Gauge, generate_latest

import config

try:
    import redis
except ImportError:  # pragma: no cover - 依赖缺失时回退本地
    redis = None

logger = logging.getLogger("deep_research.metrics")

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

_PREFIX = "dr:metrics:"
_HTTP_KEY = _PREFIX + "http"
_STARTED_KEY = _PREFIX + "research:started"
_FINISHED_KEY = _PREFIX + "research:finished"
_OUTCOMES_KEY = _PREFIX + "research:outcomes"
_ACTIVE_KEY = _PREFIX + "research:active"

_redis_client = None
_redis_url = None
_redis_disabled_until = 0.0
_redis_warned = False


def _redis():
    """懒加载 Redis 客户端；短时间故障后暂停重试，避免每次指标写入都超时。"""
    global _redis_client, _redis_url, _redis_disabled_until
    if not config.REDIS_URL.strip() or redis is None:
        return None
    if time.time() < _redis_disabled_until:
        return None
    with _lock:
        if _redis_url != config.REDIS_URL:
            _redis_client = None
            _redis_url = config.REDIS_URL
            _redis_disabled_until = 0.0
        if _redis_client is None:
            try:
                _redis_client = redis.from_url(
                    config.REDIS_URL, decode_responses=True, socket_timeout=1.0
                )
            except Exception:
                return None
        return _redis_client


def _redis_failed(exc: Exception) -> None:
    global _redis_disabled_until, _redis_warned
    _redis_disabled_until = time.time() + 30
    if not _redis_warned:
        logger.warning("redis_metrics_failed", extra={"error": f"{type(exc).__name__}: {exc}"})
        _redis_warned = True


def record_request(path: str, status: int) -> None:
    with _lock:
        _requests[f"{path} {status}"] += 1
    HTTP_REQUESTS.labels(path=path, status=str(status)).inc()
    client = _redis()
    if client is not None:
        try:
            client.hincrby(_HTTP_KEY, f"{path}|{status}", 1)
        except Exception as exc:
            _redis_failed(exc)


def research_started() -> None:
    global _research_started, _active
    with _lock:
        _research_started += 1
        _active += 1
    RESEARCH_STARTED.inc()
    RESEARCH_ACTIVE.inc()
    client = _redis()
    if client is not None:
        try:
            pipe = client.pipeline()
            pipe.incr(_STARTED_KEY)
            pipe.incr(_ACTIVE_KEY)
            pipe.execute()
        except Exception as exc:
            _redis_failed(exc)


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
    client = _redis()
    if client is not None:
        try:
            pipe = client.pipeline()
            pipe.incr(_FINISHED_KEY)
            pipe.hincrby(_OUTCOMES_KEY, outcome, 1)
            pipe.decr(_ACTIVE_KEY)
            pipe.execute()
        except Exception as exc:
            _redis_failed(exc)


def _escape(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_redis(client) -> str:
    http = client.hgetall(_HTTP_KEY)
    started = int(client.get(_STARTED_KEY) or 0)
    active = max(0, int(client.get(_ACTIVE_KEY) or 0))
    outcomes = client.hgetall(_OUTCOMES_KEY)
    lines = [
        "# HELP dr_http_requests_total HTTP requests by route and status",
        "# TYPE dr_http_requests_total counter",
    ]
    for field, count in sorted(http.items()):
        path, _, status = field.rpartition("|")
        lines.append(
            f'dr_http_requests_total{{path="{_escape(path)}",status="{_escape(status)}"}} {int(count)}'
        )
    lines += [
        "# HELP dr_research_started_total Research runs started",
        "# TYPE dr_research_started_total counter",
        f"dr_research_started_total {started}",
        "# HELP dr_research_active Currently running research runs",
        "# TYPE dr_research_active gauge",
        f"dr_research_active {active}",
        "# HELP dr_research_finished_total Research runs finished by outcome",
        "# TYPE dr_research_finished_total counter",
    ]
    for outcome, count in sorted(outcomes.items()):
        lines.append(
            f'dr_research_finished_total{{outcome="{_escape(outcome)}"}} {int(count)}'
        )
    return "\n".join(lines) + "\n"


def render() -> str:
    client = _redis()
    if client is not None:
        try:
            return _render_redis(client)
        except Exception as exc:
            _redis_failed(exc)
    return generate_latest().decode("utf-8")


def _snapshot_redis(client) -> dict:
    http = client.hgetall(_HTTP_KEY)
    outcomes = client.hgetall(_OUTCOMES_KEY)
    return {
        "uptime_s": round(time.time() - _started_at, 1),
        "requests": {field.replace("|", " ", 1): int(v) for field, v in http.items()},
        "research": {
            "started": int(client.get(_STARTED_KEY) or 0),
            "finished": int(client.get(_FINISHED_KEY) or 0),
            "failed": int(outcomes.get("failed", 0)),
            "active": max(0, int(client.get(_ACTIVE_KEY) or 0)),
            "outcomes": {k: int(v) for k, v in outcomes.items()},
        },
    }


def snapshot() -> dict:
    client = _redis()
    if client is not None:
        try:
            return _snapshot_redis(client)
        except Exception as exc:
            _redis_failed(exc)
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
    global _redis_disabled_until
    with _lock:
        _started_at = time.time()
        _requests.clear()
        _outcomes.clear()
        _research_started = _research_finished = _research_failed = _active = 0
        _redis_disabled_until = 0.0
    client = _redis()
    if client is not None:
        try:
            client.delete(_HTTP_KEY, _STARTED_KEY, _FINISHED_KEY, _OUTCOMES_KEY, _ACTIVE_KEY)
        except Exception as exc:
            _redis_failed(exc)
