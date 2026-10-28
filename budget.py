"""全局预算：长流程的硬闸门。

与 llm/client.py 的 USAGE 共用同一本账（这里的用量全部从 USAGE 读出），
不重复计数 —— 曾经踩过的坑正是"两条统计口径对不上"（618s / 5.1k tokens），
所以用量只有一个来源，预算只负责判定"还能不能继续"。

判定结果由各节点/路由写入状态的 budget_exceeded 字段：
非空表示本轮研究已降级收敛（不再开新分支，直接把已有笔记交给 writer），
原因与剩余额度随状态一起落盘，可在评测/前端/CLI 里审计。
"""
import threading
import time

import config
from llm.client import USAGE


class Budget:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = time.time()

    def reset(self) -> None:
        """每次研究开始前调用（CLI 一次、Web 每个请求一次、评测每题一次）。"""
        with self._lock:
            self.started_at = time.time()

    def elapsed_s(self) -> float:
        return round(time.time() - self.started_at, 1)

    def usage(self) -> dict:
        usage = USAGE.snapshot()
        cost = (
            usage["input_tokens"] / 1e6 * config.LLM_PRICE_INPUT_PER_M
            + usage["output_tokens"] / 1e6 * config.LLM_PRICE_OUTPUT_PER_M
        )
        usage.update({"cost_usd_est": round(cost, 4), "elapsed_s": self.elapsed_s()})
        return usage

    def exceeded(self) -> str:
        """返回超限原因（空字符串=预算健康）。多维度同时超限时按成本敏感度排序取第一个。"""
        usage = self.usage()
        if usage["calls"] >= config.MAX_LLM_CALLS:
            return f"llm_calls={usage['calls']}/{config.MAX_LLM_CALLS}"
        if usage["input_tokens"] + usage["output_tokens"] >= config.MAX_TOTAL_TOKENS:
            total = usage["input_tokens"] + usage["output_tokens"]
            return f"total_tokens={total}/{config.MAX_TOTAL_TOKENS}"
        if usage["cost_usd_est"] >= config.MAX_BUDGET_USD:
            return f"cost_usd={usage['cost_usd_est']}/{config.MAX_BUDGET_USD}"
        if usage["elapsed_s"] >= config.MAX_WALL_CLOCK_S:
            return f"wall_clock={usage['elapsed_s']}s/{config.MAX_WALL_CLOCK_S}s"
        return ""

    def remaining(self) -> dict:
        """剩余额度（给 CLI 头部与前端统计卡展示，让"快没预算了"可见）。"""
        usage = self.usage()
        total_tokens = usage["input_tokens"] + usage["output_tokens"]
        return {
            "llm_calls_left": max(0, config.MAX_LLM_CALLS - usage["calls"]),
            "tokens_left": max(0, config.MAX_TOTAL_TOKENS - total_tokens),
            "seconds_left": max(0, round(config.MAX_WALL_CLOCK_S - usage["elapsed_s"], 1)),
            "usd_left": max(0.0, round(config.MAX_BUDGET_USD - usage["cost_usd_est"], 4)),
        }


BUDGET = Budget()
