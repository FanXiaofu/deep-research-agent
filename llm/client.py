"""OpenAI 兼容的 LLM 客户端封装（默认 DeepSeek，可切换任意兼容服务/Ollama）。

Langfuse 配置了 key 时自动换成 langfuse.openai 的追踪版客户端，
每次调用的 token 用量/延迟自动上报，作为 @observe span 的子节点。
"""
import json
import logging
import re

import httpx
import observability
import run_context
from openai import OpenAI

import config
import reliability

_client: OpenAI | None = None
logger = logging.getLogger("deep_research.llm")

# qwen3 等思考型模型可能把推理过程以 <think> 标签混入 content，统一剥离
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class BudgetExceeded(RuntimeError):
    """墙钟预算已耗尽，停止发起新的 LLM 调用。"""


def _remaining_budget_s() -> float | None:
    """当前 run 的剩余墙钟；default context（CLI/单测）返回 None 表示不裁剪。"""
    ctx = run_context.current_run()
    if ctx.run_id is None:
        return None
    return max(0.0, config.MAX_WALL_CLOCK_S - ctx.elapsed_s())


def _request_timeout() -> float:
    """把剩余墙钟按最大尝试次数均摊，避免一次卡住的调用拖垮整轮预算。"""
    remaining = _remaining_budget_s()
    if remaining is None:
        return config.LLM_CALL_TIMEOUT_S
    if remaining <= 0:
        raise BudgetExceeded(f"墙钟预算已耗尽（{config.MAX_WALL_CLOCK_S:.0f}s）")
    attempts = max(1, config.LLM_MAX_RETRIES + 1)
    return max(0.5, min(config.LLM_CALL_TIMEOUT_S, remaining / attempts))


class _UsageProxy:
    """兼容原 USAGE 接口；实际读写当前 run 的 UsageTracker。"""

    def reset(self) -> None:
        run_context.current_run().usage.reset()

    def add(self, prompt_tokens: int, completion_tokens: int) -> None:
        run_context.current_run().usage.add(prompt_tokens, completion_tokens)

    def snapshot(self) -> dict:
        return run_context.current_run().usage.snapshot()


USAGE = _UsageProxy()


def _build_client() -> OpenAI:
    kwargs = dict(
        api_key=config.LLM_API_KEY or "EMPTY",
        base_url=config.LLM_BASE_URL,
        max_retries=config.LLM_MAX_RETRIES,
        timeout=config.LLM_CALL_TIMEOUT_S,  # 单次调用上限；实际值还会受剩余墙钟约束
    )
    if observability.is_enabled():
        try:
            from langfuse.openai import OpenAI as TracedOpenAI
            return TracedOpenAI(**kwargs)
        except ImportError:
            pass
    return OpenAI(**kwargs)


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = _build_client()
    return _client


@observability.observe(name="llm.chat")
def chat(
    messages: list[dict],
    *,
    temperature: float | None = None,
    json_mode: bool = False,
    model: str | None = None,
) -> str:
    """单轮对话。json_mode=True 时要求模型输出 JSON（提示词里需包含"JSON"字样）。"""
    if config.LLM_PROMPT_SUFFIX:
        messages = [dict(m) for m in messages]
        messages[-1]["content"] = messages[-1]["content"] + config.LLM_PROMPT_SUFFIX
    if config.LLM_NATIVE_OLLAMA:
        return _native_chat(messages, temperature=temperature, json_mode=json_mode, model=model)
    kwargs = dict(
        model=model or config.LLM_MODEL,
        messages=messages,
        temperature=config.LLM_TEMPERATURE if temperature is None else temperature,
    )
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    kwargs["timeout"] = _request_timeout()
    resp = get_client().chat.completions.create(**kwargs)
    usage = getattr(resp, "usage", None)
    if usage is not None:
        pt = int(getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0)
        ct = int(getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0)
        USAGE.add(pt, ct)
    choices = getattr(resp, "choices", None) or []
    if not choices:
        raise RuntimeError("LLM 返回空 choices（可能是内容过滤或 provider 异常）")
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None) or ""
    return _THINK_RE.sub("", content).strip()


def chat_json(messages: list[dict], **kwargs) -> dict:
    return parse_json(chat(messages, json_mode=True, **kwargs))


def chat_safe(messages: list[dict], *, fallback: str = "", what: str = "LLM 调用", **kwargs) -> str:
    """失败不抛异常的 chat：重试/超时受 LLM_MAX_RETRIES 与剩余墙钟约束，仍失败则降级。

    长流程里单个节点把异常抛出去会毁掉整轮研究（checkpoint 之后虽然能续跑，
    但已花掉的检索成本要重来），因此节点侧统一用这个包装做优雅降级。
    """
    try:
        return chat(messages, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 降级是显式设计
        reliability.record_degradation("llm", f"{what}失败（{type(exc).__name__}: {exc}）")
        return fallback


def chat_json_safe(messages: list[dict], *, fallback: dict | None = None, what: str = "LLM 调用", **kwargs) -> dict:
    """chat_safe 的 JSON 版本：调用失败或解析失败都返回 fallback。"""
    try:
        return chat_json(messages, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 降级是显式设计
        reliability.record_degradation("llm", f"{what}失败（{type(exc).__name__}: {exc}）")
        return fallback if fallback is not None else {}


def estimate_tokens(text: str) -> int:
    """粗估 token 数：CJK 约 1 字 1 token，ASCII 约 4 字符 1 token。

    只用于"提示词是否逼近上下文窗口"的对账告警，不追求精确。
    """
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f")
    other = len(text) - cjk
    return cjk + other // 4


_NATIVE_SYSTEM = (
    "你是生产系统中的一个处理组件。直接输出最终结果本身，"
    "禁止输出思考过程、推理步骤、自我对话或对任务要求的复述。"
)


def _native_chat(
    messages: list[dict], *, temperature: float | None, json_mode: bool, model: str | None = None
) -> str:
    """Ollama 原生 /api/chat：唯一能真正关掉 qwen3 思考通道的路径（think=false）。

    think=false 时 qwen3 仍可能把推理写进正文，因此叠加系统提示与 /no_think 软开关双保险，
    并把 num_ctx 提到可配置的窗口（config.OLLAMA_NUM_CTX，默认 8192），避免长研究提示词在默认 4096 下被静默截断。
    提示词估算接近窗口上限时打告警：静默截断会表现为"模型答非所问/空回复"，
    这类问题必须能在日志里对账（token 与延迟对不上账的老问题）。
    注意：此路径不经过 langfuse.openai 自动埋点（trace 仍由 @observe 生成，无 token 明细）。
    """
    base = config.LLM_BASE_URL.removesuffix("/v1").rstrip("/")
    msgs = [{"role": m["role"], "content": m["content"]} for m in messages]
    if not any(m["role"] == "system" for m in msgs):
        msgs.insert(0, {"role": "system", "content": _NATIVE_SYSTEM})
    prompt_tokens = sum(estimate_tokens(m["content"]) for m in msgs)
    if prompt_tokens > config.OLLAMA_NUM_CTX * 0.8:
        logger.warning(
            "ollama prompt near context limit",
            extra={"prompt_tokens": prompt_tokens, "num_ctx": config.OLLAMA_NUM_CTX},
        )
        reliability.record_degradation("ollama_ctx", f"提示词约 {prompt_tokens} tokens 逼近 num_ctx={config.OLLAMA_NUM_CTX}")
    body = {
        "model": model or config.LLM_MODEL,
        "messages": msgs,
        "stream": False,
        "think": False,
        "options": {
            "temperature": config.LLM_TEMPERATURE if temperature is None else temperature,
            "num_ctx": config.OLLAMA_NUM_CTX,
        },
    }
    if json_mode:
        body["format"] = "json"

    attempts = max(1, config.LLM_MAX_RETRIES + 1)
    last_exc: Exception | None = None
    for _attempt in range(attempts):
        try:
            timeout = _request_timeout()  # 每次尝试都重新计算剩余墙钟
            resp = httpx.post(f"{base}/api/chat", json=body, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            USAGE.add(int(data.get("prompt_eval_count", 0)), int(data.get("eval_count", 0)))
            return _THINK_RE.sub("", data.get("message", {}).get("content", "") or "").strip()
        except BudgetExceeded:
            raise
        except Exception as exc:
            last_exc = exc
            # 不支持 thinking 的模型（如 qwen2.5）收到 think 参数会报错，降级为不带该参数重试
            if "think" in str(exc).lower():
                body.pop("think", None)
    raise RuntimeError(f"Ollama 原生调用失败（重试 {attempts} 次）：{last_exc}")


def parse_json(text: str) -> dict:
    """容错解析：剥掉 markdown 代码围栏，再截取最外层 JSON。"""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}|\[.*\]", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise
