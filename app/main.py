"""FastAPI Web 界面：SSE 流式展示多 Agent 研究过程（W4）。

启动：python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

接口：
    GET /                          研究界面
    GET /api/research/stream       发起研究并流式返回进度（SSE）
    GET /api/resume/stream         human-in-the-loop 审核后续跑（SSE）
    GET /api/report/{thread_id}    获取某次研究的最终报告（Markdown）

单用户本地工具假设：USAGE 用量统计与 checkpoint 连接为进程级共享。
"""
import json
import time
from datetime import datetime
from pathlib import Path

import config
import markdown as md_lib
import observability
from fastapi import FastAPI, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.templating import Jinja2Templates
from langgraph.types import Command

from graph.build import build_research_app, make_checkpointer
from llm.client import USAGE

app = FastAPI(title="Deep Research Agent")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_research_app = None

NODE_LABELS = {
    "coordinator": "🎯 协调员",
    "planner": "📋 规划师",
    "researcher": "🔍 研究员",
    "verifier": "⚖️ 核查员",
    "writer": "✍️ 撰写人",
}

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def get_research_app():
    global _research_app
    if _research_app is None:
        _research_app = build_research_app(make_checkpointer())
    return _research_app


def sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _describe(node: str, out) -> list[dict]:
    """把节点输出转成前端可渲染的事件载荷（并行同名节点输出可能是列表）。"""
    events = []
    for item in out if isinstance(out, list) else [out]:
        ev: dict = {"node": node}
        if node == "coordinator":
            ev["brief"] = item.get("brief", "")
        elif node == "planner":
            ev["subquestions"] = item.get("subquestions", [])
        elif node == "researcher":
            note = (item.get("notes") or [{}])[0]
            ev["subquestion"] = note.get("subquestion", "")
            ev["n_sources"] = len(note.get("sources", []))
        elif node == "verifier":
            ev["conflicts"] = item.get("conflicts", [])
        events.append(ev)
    return events


def _report_event(state: dict, elapsed: float) -> str:
    report = state.get("report", "")
    usage = USAGE.snapshot()
    cost = (
        usage["input_tokens"] / 1e6 * config.LLM_PRICE_INPUT_PER_M
        + usage["output_tokens"] / 1e6 * config.LLM_PRICE_OUTPUT_PER_M
    )
    return sse({
        "type": "report",
        "markdown": report,
        "html": md_lib.markdown(report, extensions=["extra"]),
        "stats": {
            "subquestions": len(state.get("subquestions", [])),
            "notes": len(state.get("notes", [])),
            "llm_calls": usage["calls"],
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "cost_usd_est": round(cost, 4),
            "elapsed_s": round(elapsed, 1),
        },
    })


def _stream(payload, cfg: dict, started: float):
    """公共生成器：流式执行图产出 SSE 事件；遇到 interrupt 时结束本次流，等待 /resume 续跑。

    整轮研究包在 trace_context 里，Langfuse 中表现为一条 trace（含所有 LLM span）。
    """
    with observability.trace_context(cfg["configurable"]["thread_id"], trace_name="deep-research"):
        try:
            for update in get_research_app().stream(payload, cfg, stream_mode="updates"):
                if "__interrupt__" in update:
                    intr = update["__interrupt__"][0]
                    yield sse({
                        "type": "interrupt",
                        "thread_id": cfg["configurable"]["thread_id"],
                        "brief": (intr.value or {}).get("brief", ""),
                    })
                    return
                for node, out in update.items():
                    if node == "review_brief":
                        continue  # 审核由 interrupt 事件负责
                    for ev in _describe(node, out):
                        ev.update({
                            "type": "node_done",
                            "label": NODE_LABELS.get(node, node),
                            "elapsed_s": round(time.time() - started, 1),
                        })
                        yield sse(ev)
            state = get_research_app().get_state(cfg).values or {}
            yield _report_event(state, time.time() - started)
        except Exception as exc:
            yield sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "model": config.LLM_MODEL,
            "provider": config.SEARCH_PROVIDER,
            "langfuse": observability.is_enabled(),
        },
    )


@app.get("/api/research/stream")
def research_stream(
    topic: str = Query(..., min_length=2),
    review: bool = False,
    max_sub: int | None = None,
    rounds: int | None = None,
):
    if max_sub:
        config.MAX_SUBQUESTIONS = max_sub
    if rounds:
        config.MAX_SEARCH_ROUNDS = rounds
    tid = f"web-{datetime.now():%Y%m%d-%H%M%S}"
    cfg = {"configurable": {"thread_id": tid}}
    payload = {"topic": topic, "notes": [], "require_review": review}
    USAGE.reset()
    started = time.time()

    def gen():
        yield sse({"type": "start", "thread_id": tid, "topic": topic})
        yield from _stream(payload, cfg, started)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.get("/api/resume/stream")
def resume_stream(thread_id: str = Query(...), value: str = ""):
    """human-in-the-loop：value 为空表示确认原任务书，非空表示替换。"""
    cfg = {"configurable": {"thread_id": thread_id}}
    payload = Command(resume=value or "")
    started = time.time()

    def gen():
        yield sse({"type": "resume", "thread_id": thread_id})
        yield from _stream(payload, cfg, started)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.get("/api/report/{thread_id}")
def get_report(thread_id: str):
    state = get_research_app().get_state({"configurable": {"thread_id": thread_id}}).values or {}
    return {"thread_id": thread_id, "report": state.get("report", "")}
