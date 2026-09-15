"""OpenAI 兼容的 LLM 客户端封装（默认 DeepSeek，可切换任意兼容服务/Ollama）。

Langfuse 配置了 key 时自动换成 langfuse.openai 的追踪版客户端，
每次调用的 token 用量/延迟自动上报，作为 @observe span 的子节点。
"""
import json
import re
import threading

import httpx
import observability
from openai import OpenAI

import config

_client: OpenAI | None = None

# qwen3 等思考型模型可能把推理过程以 <think> 标签混入 content，统一剥离
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class UsageTracker:
    """线程安全的 token 用量累计器（并行 researcher 的调用同时计入）。

    eval 用它统计单次研究的成本；CLI 场景不重置就是整个进程的累计值。
    """

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
            self.input_tokens += prompt_tokens
            self.output_tokens += completion_tokens

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "calls": self.calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            }


USAGE = UsageTracker()


def _build_client() -> OpenAI:
    kwargs = dict(
        api_key=config.LLM_API_KEY or "EMPTY",
        base_url=config.LLM_BASE_URL,
        max_retries=3,
        timeout=300,  # qwen3 思考模式下单次调用可达 1~2 分钟，本地推理要放宽
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
def chat(messages: list[dict], *, temperature: float | None = None, json_mode: bool = False) -> str:
    """单轮对话。json_mode=True 时要求模型输出 JSON（提示词里需包含"JSON"字样）。"""
    if config.LLM_PROMPT_SUFFIX:
        messages = [dict(m) for m in messages]
        messages[-1]["content"] = messages[-1]["content"] + config.LLM_PROMPT_SUFFIX
    if config.LLM_NATIVE_OLLAMA:
        return _native_chat(messages, temperature=temperature, json_mode=json_mode)
    kwargs = dict(
        model=config.LLM_MODEL,
        messages=messages,
        temperature=config.LLM_TEMPERATURE if temperature is None else temperature,
    )
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = get_client().chat.completions.create(**kwargs)
    usage = getattr(resp, "usage", None)
    if usage is not None:
        pt = int(getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0)
        ct = int(getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0)
        USAGE.add(pt, ct)
    return _THINK_RE.sub("", resp.choices[0].message.content or "").strip()


def chat_json(messages: list[dict], **kwargs) -> dict:
    return parse_json(chat(messages, json_mode=True, **kwargs))


_NATIVE_SYSTEM = (
    "你是生产系统中的一个处理组件。直接输出最终结果本身，"
    "禁止输出思考过程、推理步骤、自我对话或对任务要求的复述。"
)


def _native_chat(messages: list[dict], *, temperature: float | None, json_mode: bool) -> str:
    """Ollama 原生 /api/chat：唯一能真正关掉 qwen3 思考通道的路径（think=false）。

    think=false 时 qwen3 仍可能把推理写进正文，因此叠加系统提示与 /no_think 软开关双保险，
    并把 num_ctx 提到 8192，避免长研究提示词在默认 4096 下被静默截断。
    注意：此路径不经过 langfuse.openai 自动埋点（trace 仍由 @observe 生成，无 token 明细）。
    """
    base = config.LLM_BASE_URL.removesuffix("/v1").rstrip("/")
    msgs = [{"role": m["role"], "content": m["content"]} for m in messages]
    if not any(m["role"] == "system" for m in msgs):
        msgs.insert(0, {"role": "system", "content": _NATIVE_SYSTEM})
    body = {
        "model": config.LLM_MODEL,
        "messages": msgs,
        "stream": False,
        "think": False,
        "options": {
            "temperature": config.LLM_TEMPERATURE if temperature is None else temperature,
            "num_ctx": 8192,
        },
    }
    if json_mode:
        body["format"] = "json"

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = httpx.post(f"{base}/api/chat", json=body, timeout=600)
            resp.raise_for_status()
            data = resp.json()
            USAGE.add(int(data.get("prompt_eval_count", 0)), int(data.get("eval_count", 0)))
            return _THINK_RE.sub("", data.get("message", {}).get("content", "") or "").strip()
        except Exception as exc:
            last_exc = exc
            # 不支持 thinking 的模型（如 qwen2.5）收到 think 参数会报错，降级为不带该参数重试
            if "think" in str(exc).lower():
                body.pop("think", None)
    raise RuntimeError(f"Ollama 原生调用失败（重试 3 次）：{last_exc}")


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
