"""外部依赖的容错层：重试 + 熔断 + 降级留痕。

搜索与抓取都是不可靠的外部调用，且共享额度（Tavily 免费额度 1000 次/月）：
- 重试：瞬时网络错误不该毁掉一个研究分支；
- 熔断：连续失败达到阈值后短路一段时间，让整个流程快速降级（按"无结果"继续），
  而不是每个并行分支都去撞同一堵墙（额度耗尽 / 断网的典型场景）；
- 降级留痕：每次降级记一条事件，供 CLI、前端和评测审计 —— 降级静默发生就等于没有降级。

熔断器按名字全局共享（并行 researcher 实例共用同一个 breaker）。
"""
import threading
import time

import config


class CircuitBreaker:
    """三态熔断器：closed（正常）→ open（冷却期内直接拒绝）→ half-open（放行一次探活）。

    半开探活失败会重新进入冷却；探活成功即回到 closed。状态对调用方可见（snapshot），
    降级频率因此是可观测的，而不是"悄悄跳过"。
    """

    def __init__(self, name: str, failure_threshold: int, cooldown_s: float) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at = 0.0
        self._probing = False

    def allow(self) -> bool:
        with self._lock:
            if self._opened_at == 0.0:
                return True  # closed
            if time.time() - self._opened_at < self.cooldown_s:
                return False  # open：冷却期内短路，不再打外部服务
            self._probing = True  # half-open：放行一次探活
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = 0.0
            self._probing = False

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._probing or self._failures >= self.failure_threshold:
                self._opened_at = time.time()  # 探活失败 → 重新冷却
                self._probing = False

    def state(self) -> str:
        with self._lock:
            if self._opened_at == 0.0:
                return "closed"
            if self._probing or time.time() - self._opened_at >= self.cooldown_s:
                return "half-open"
            return "open"

    def snapshot(self) -> dict:
        return {"name": self.name, "state": self.state(), "failures": self._failures}


_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()
_degradations: list[dict] = []
_degradations_lock = threading.Lock()


def get_breaker(name: str) -> CircuitBreaker:
    with _breakers_lock:
        if name not in _breakers:
            _breakers[name] = CircuitBreaker(
                name, config.BREAKER_FAILURE_THRESHOLD, config.BREAKER_COOLDOWN_S
            )
        return _breakers[name]


def record_degradation(component: str, reason: str) -> None:
    with _degradations_lock:
        _degradations.append(
            {"component": component, "reason": reason, "at_s": round(time.time() % 100000, 1)}
        )
        del _degradations[:-50]  # 只保留最近 50 条


def degradations() -> list[dict]:
    with _degradations_lock:
        return list(_degradations)


def reset_all() -> None:
    """每轮研究开始前重置（熔断状态跨轮保留会误伤下一轮）。"""
    with _breakers_lock:
        _breakers.clear()
    with _degradations_lock:
        _degradations.clear()


def call_with_retry(fn, *, attempts: int, delay_s: float = 1.0):
    """同步重试：第 i 次失败后等 delay_s * (i+1) 秒再试，全部失败抛出最后一次异常。"""
    last_exc: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 调用方自行决定降级策略
            last_exc = exc
            if i < attempts - 1:
                time.sleep(delay_s * (i + 1))
    raise last_exc  # type: ignore[misc]


def call_guarded(name: str, fn, *, attempts: int, default, what: str = ""):
    """熔断 + 重试 + 降级：不可用或最终失败时返回 default 并留痕，不抛异常。

    返回 (结果, 是否降级)。调用方用降级标志决定后续动作（例如 researcher 提前收尾）。
    """
    breaker = get_breaker(name)
    if not breaker.allow():
        record_degradation(name, f"熔断中（{breaker.state()}），跳过{what or '调用'}")
        return default, True
    try:
        result = call_with_retry(fn, attempts=attempts)
    except Exception as exc:
        breaker.record_failure()
        record_degradation(name, f"{what or '调用'}失败（{type(exc).__name__}: {exc}）")
        return default, True
    breaker.record_success()
    return result, False
