"""B1 集成级验证：并发 SSE 流的 stats 只统计自己这一轮。"""
import json
import threading
import time
from types import SimpleNamespace

from app import main as app_main
from llm.client import USAGE


class FakeApp:
    """用一个假图代替真实研究：stream 时模拟一次 LLM 用量，然后返回报告。"""

    def __init__(self):
        self.states = {}

    def stream(self, payload, cfg, stream_mode="updates"):
        tid = cfg["configurable"]["thread_id"]
        USAGE.add(10, 5)
        time.sleep(0.02)  # 让多个流真正重叠
        self.states[tid] = {"report": f"# {tid}", "subquestions": [], "notes": []}
        yield {"writer": {}}

    def get_state(self, cfg):
        tid = cfg["configurable"]["thread_id"]
        return SimpleNamespace(values=self.states.get(tid, {}))


def test_concurrent_streams_keep_stats_isolated(monkeypatch):
    fake = FakeApp()
    monkeypatch.setattr(app_main, "get_research_app", lambda: fake)
    results = {}

    def worker(tid):
        events = list(
            app_main._stream(
                {"run_id": tid},
                {"configurable": {"thread_id": tid}},
                time.time(),
            )
        )
        payload = json.loads(events[-1].split("data: ", 1)[1])
        results[tid] = payload["stats"]

    threads = [threading.Thread(target=worker, args=(f"stream-{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 3
    for stats in results.values():
        assert stats["llm_calls"] == 1
        assert stats["input_tokens"] == 10
        assert stats["output_tokens"] == 5
