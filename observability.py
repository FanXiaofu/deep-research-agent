"""Langfuse 可观测性封装：.env 配置了 LANGFUSE_PUBLIC_KEY/SECRET_KEY 才启用，否则优雅降级为 no-op。

启用后：
- @observe 装饰的函数自动上报 span（LLM 调用等）
- trace_context() 把整轮研究包成一条 trace，内部所有调用归入同一 session，
  便于在 Langfuse 里按 session 过滤出完整的一次 Agent 协作过程
- langfuse.openai 客户端自动记录每次调用的 token 用量与延迟

注意（SDK v4 的 API 变化）：
- v4 起不再识别 LANGFUSE_SESSION_ID 环境变量
- 也不再提供 update_current_trace()，trace 级属性必须用 propagate_attributes() 传播
  （见 https://langfuse.com/docs/observability/sdk/python/instrumentation）
"""
from contextlib import contextmanager

import config

_ENABLED = bool(config.LANGFUSE_PUBLIC_KEY and config.LANGFUSE_SECRET_KEY)

if _ENABLED:
    from langfuse import observe
else:
    def observe(fn=None, **_kwargs):
        """未配置 key 时的 no-op 替身，兼容 @observe 与 @observe(name=...) 两种写法。"""
        if fn is not None:
            return fn

        def deco(f):
            return f

        return deco


def is_enabled() -> bool:
    return _ENABLED


@contextmanager
def trace_context(session_id: str, trace_name: str = "deep-research", metadata: dict | None = None):
    """把一轮研究包成一条 trace：创建 agent 根 span，内部所有 LLM 调用成为其子 span。

    这样在 Langfuse 里能看到完整的调用树（deep-research → llm.chat × N），
    并按 session_id 过滤出某一轮研究的全部观测。
    """
    if not _ENABLED:
        yield None
        return
    try:
        from langfuse import get_client, propagate_attributes  # type: ignore[attr-defined]
        with propagate_attributes(session_id=session_id, metadata=metadata or {}):
            with get_client().start_as_current_observation(
                name=trace_name, as_type="agent", input=metadata or None
            ) as span:
                yield span
    except Exception:
        yield None  # 可观测性不能影响主流程


def flush() -> None:
    if _ENABLED:
        from langfuse import get_client  # type: ignore[attr-defined]
        get_client().flush()
