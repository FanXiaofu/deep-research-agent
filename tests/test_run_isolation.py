"""B1：运行期状态按 run_id 隔离；并发请求、评测重跑、HITL 续跑互不污染。"""
import threading

import config
import reliability
import run_context
from budget import BUDGET
from graph.build import _fan_out
from llm.client import USAGE


def setup_function(_func):
    run_context.reset_all_runs()
    run_context.bind_run(None)


def teardown_function(_func):
    run_context.reset_all_runs()
    run_context.bind_run(None)


def test_usage_is_isolated_between_runs():
    with run_context.run_scope("run-a"):
        USAGE.add(10, 5)
        assert USAGE.snapshot() == {"calls": 1, "input_tokens": 10, "output_tokens": 5}

    with run_context.run_scope("run-b"):
        assert USAGE.snapshot()["calls"] == 0

    with run_context.run_scope("run-a"):
        assert USAGE.snapshot()["calls"] == 1


def test_degradations_are_isolated():
    with run_context.run_scope("a"):
        reliability.record_degradation("search", "A 失败")
        assert len(reliability.degradations()) == 1

    with run_context.run_scope("b"):
        assert reliability.degradations() == []


def test_breakers_are_isolated(monkeypatch):
    monkeypatch.setattr(config, "BREAKER_FAILURE_THRESHOLD", 1)

    with run_context.run_scope("a"):
        breaker = reliability.get_breaker("search")
        breaker.record_failure()
        assert breaker.state() == "open"

    with run_context.run_scope("b"):
        assert reliability.get_breaker("search").state() == "closed"


def test_budget_reset_keeps_usage_for_resume():
    """续跑只重算墙钟，不能把上一段的调用数清零（否则预算会被重复使用）。"""
    with run_context.run_scope("resume"):
        USAGE.add(100, 50)
        BUDGET.reset()
        assert USAGE.snapshot()["calls"] == 1
        assert BUDGET.usage()["input_tokens"] == 100


def test_discard_run_starts_from_empty_context():
    with run_context.run_scope("temp"):
        USAGE.add(1, 1)

    run_context.discard_run("temp")

    with run_context.run_scope("temp"):
        assert USAGE.snapshot()["calls"] == 0


def test_nested_scope_restores_outer_run():
    with run_context.run_scope("outer"):
        USAGE.add(1, 0)
        with run_context.run_scope("inner"):
            assert USAGE.snapshot()["calls"] == 0
        assert USAGE.snapshot()["calls"] == 1


def test_threads_do_not_share_usage():
    results = {}

    def worker(i):
        with run_context.run_scope(f"thread-{i}"):
            USAGE.add(10 * (i + 1), 0)
            results[i] = USAGE.snapshot()["input_tokens"]

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == {0: 10, 1: 20, 2: 30, 3: 40}


def test_fan_out_carries_run_id():
    sends = _fan_out({"run_id": "r-1", "subquestions": ["a"]})
    assert sends[0].arg["run_id"] == "r-1"
