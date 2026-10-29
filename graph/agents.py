"""轻量 JSON 动作 Agent 循环。

不依赖 provider 原生 tool calling：每一步让模型输出
`{"action": "...", ...}`，代码执行白名单工具并把 observation 回填。
这样本地 Ollama、OpenAI 兼容 API、Command Code API 都能使用同一套 Agent。
"""
import json
from typing import Callable

from budget import BUDGET
from llm.client import chat_json_safe
from untrusted import GUARD, wrap

Tool = Callable[[dict], object]


def _short(value, limit: int = 4000) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > limit:
        text = text[:limit] + "…"
    return wrap(text, source="tool_observation")


def run_agent(
    *,
    role: str,
    task: str,
    tool_specs: list[str],
    tools: dict[str, Tool],
    max_steps: int,
    final_action: str = "final",
) -> dict | None:
    history: list[str] = []
    for _ in range(max(1, max_steps)):
        blocked = BUDGET.exceeded()
        if blocked:
            return {"action": "budget_exceeded", "reason": blocked}
        prompt = f"""{GUARD}

你是 {role}。任务：
{task}

可用工具（只能使用这些 action）：
{chr(10).join('- ' + spec for spec in tool_specs)}

已有观察：
{chr(10).join(history[-8:]) if history else '（无）'}

每一步只输出一个 JSON 对象。格式：
- 调用工具：{{"action":"工具名", "参数":"..."}}
- 结束：{{"action":"{final_action}", ...最终结果字段}}
不要输出 Markdown、解释或代码围栏。"""
        data = chat_json_safe(
            [{"role": "user", "content": prompt}],
            fallback={},
            what=f"{role} 决策",
            temperature=0.1,
        )
        if not isinstance(data, dict) or not data:
            return None
        action = str(data.get("action", "")).strip()
        if action == final_action:
            return data
        if action == "budget_exceeded":
            return data
        tool = tools.get(action)
        if tool is None:
            history.append(_short({"action": action, "error": "未知工具"}))
            continue
        try:
            observation = tool(data)
        except Exception as exc:  # 单个工具失败不应毁掉整个 Agent
            observation = {"error": f"{type(exc).__name__}: {exc}"}
        history.append(_short({
            "action": action,
            "params": {k: v for k, v in data.items() if k != "action"},
            "observation": observation,
        }))
    return None
