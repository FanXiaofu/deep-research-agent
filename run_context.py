"""按运行轮次隔离的运行时状态（usage / 预算时钟 / 降级事件 / 熔断器）。

原实现是进程级单例：Web 端两个并发研究会互相重置统计、共享熔断状态，
一个请求的降级事件还会出现在另一个请求的报告里。这里把状态收进 RunContext，
用 run_id（通常等于 thread_id）索引；图节点入口通过 run_scope(state["run_id"]) 激活。

没有 run_id 时使用 default context，保持 CLI / 单测的单进程行为不变。
"""
import contextvars
import threading
import time
from contextlib import contextmanager

_MAX_IDLE_S = 6 * 3600


class UsageTracker:
    """线程安全的 token 用量累计器（并行 researcher 的调用同时计入同一 run）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def reset(self) -> None:
        with self._lock:
            self.calls = self.input_tokens = self.output_tokens = 0

    def add(self, prompt_tokens: int, completion_tokens: int) -> None:
        with self._lock:
            self.calls += 1
            self.input_tokens += int(prompt_tokens or 0)
            self.output_tokens += int(completion_tokens or 0)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "calls": self.calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            }


class RunContext:
    """一轮研究的全部运行期状态；跨线程共享，内部加锁。"""

    def __init__(self, run_id: str | None = None) -> None:
        self.run_id = run_id
        self.usage = UsageTracker()
        self.started_at = time.time()
        self.last_used = time.time()
        self._lock = threading.Lock()
        self._degradations: list[dict] = []
        self._breakers: dict[str, object] = {}

    def reset_clock(self) -> None:
        self.started_at = time.time()

    def elapsed_s(self) -> float:
        return round(time.time() - self.started_at, 1)

    def reset(self) -> None:
        """清空降级事件与熔断器（不重置 usage；usage 由调用方显式 reset）。"""
        with self._lock:
            self._degradations.clear()
            self._breakers.clear()

    def reset_breakers(self) -> None:
        with self._lock:
            self._breakers.clear()

    def add_degradation(self, component: str, reason: str) -> None:
        with self._lock:
            self._degradations.append(
                {"component": component, "reason": reason, "at_s": round(time.time() % 100000, 1)}
            )
            del self._degradations[:-50]

    def degradations(self) -> list[dict]:
        with self._lock:
            return list(self._degradations)

    def get_or_create_breaker(self, name: str, factory):
        with self._lock:
            if name not in self._breakers:
                self._breakers[name] = factory()
            return self._breakers[name]


_DEFAULT = RunContext()
_current = contextvars.ContextVar("research_run_context", default=_DEFAULT)
_registry: dict[str, RunContext] = {}
_registry_lock = threading.Lock()


def _normalize(run_id: str | None) -> str:
    return (run_id or "").strip()


def _prune_locked() -> None:
    cutoff = time.time() - _MAX_IDLE_S
    for rid, ctx in list(_registry.items()):
        if ctx.last_used < cutoff:
            del _registry[rid]


def get_run(run_id: str | None = None) -> RunContext:
    """按 run_id 取上下文；空 run_id 返回 default context。"""
    rid = _normalize(run_id)
    if not rid:
        _DEFAULT.last_used = time.time()
        return _DEFAULT
    with _registry_lock:
        ctx = _registry.get(rid)
        if ctx is None:
            _prune_locked()
            ctx = RunContext(rid)
            _registry[rid] = ctx
        ctx.last_used = time.time()
        return ctx


def current_run() -> RunContext:
    return _current.get()


@contextmanager
def run_scope(run_id: str | None = None):
    """在 with 块内把当前线程/任务绑定到指定 run。"""
    token = _current.set(get_run(run_id))
    try:
        yield _current.get()
    finally:
        _current.reset(token)


def bind_run(run_id: str | None = None) -> RunContext:
    """在当前线程长期绑定（CLI / eval 这类单轮进程用）。"""
    ctx = get_run(run_id)
    _current.set(ctx)
    return ctx


def discard_run(run_id: str | None) -> None:
    """一轮研究完成后续跑已无意义，释放上下文（中断时不要调用）。"""
    rid = _normalize(run_id)
    if not rid:
        return
    with _registry_lock:
        _registry.pop(rid, None)


def reset_all_runs() -> None:
    """测试用：清空全部 run 与 default context。"""
    with _registry_lock:
        _registry.clear()
    _DEFAULT.usage.reset()
    _DEFAULT.reset()
    _DEFAULT.reset_clock()
