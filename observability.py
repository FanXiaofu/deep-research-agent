"""Langfuse 可观测性开关：.env 配置了 LANGFUSE_PUBLIC_KEY/SECRET_KEY 才启用，否则优雅降级为 no-op。

启用后：
- @observe 装饰的函数自动上报 span（LangGraph 节点、LLM 调用逐层嵌套）
- langfuse.openai.OpenAI 替换原版客户端，自动记录每次调用的 token 用量与延迟
- LANGFUSE_SESSION_ID 环境变量把同一轮研究的所有 trace 归入同一会话
"""
import config

_ENABLED = bool(config.LANGFUSE_PUBLIC_KEY and config.LANGFUSE_SECRET_KEY)


def is_enabled() -> bool:
    return _ENABLED


if _ENABLED:
    from langfuse import observe
else:
    def observe(fn=None, **_kwargs):  # 未配置 key 时的 no-op 替身，兼容 @observe 和 @observe(name=...)
        if fn is not None:
            return fn
        def deco(f):
            return f
        return deco


def init_session(session_id: str) -> None:
    """同一 CLI 进程内的所有 trace 归入同一 session。"""
    if _ENABLED:
        import os
        os.environ["LANGFUSE_SESSION_ID"] = session_id


def flush() -> None:
    if _ENABLED:
        from langfuse import get_client
        get_client().flush()
