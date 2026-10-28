"""FastAPI Web 界面：SSE 流式展示多 Agent 研究过程（W4）。

启动：python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

接口：
    GET /                          研究界面
    GET /api/research/stream       发起研究并流式返回进度（SSE）
    GET /api/resume/stream         human-in-the-loop 审核后续跑（SSE）
    GET /api/report/{thread_id}    获取某次研究的最终报告（Markdown）

并发：运行期状态（usage / 降级 / 熔断）按 thread_id 隔离（run_context）；
checkpoint 连接仍是进程级共享（SQLite 单连接）。
"""
import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import config
import markdown as md_lib
import observability
import reliability
import run_context
from budget import BUDGET
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from langgraph.types import Command

from graph.build import build_research_app, make_checkpointer, thread_config
from llm.client import USAGE


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 配置错误在启动时暴露，而不是等到检索阶段退化成空报告
    config.validate_runtime_config()
    yield


app = FastAPI(title="Deep Research Agent", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_research_app = None

NODE_LABELS = {
    "coordinator": "🎯 协调员",
    "planner": "📋 规划师",
    "researcher": "🔍 研究员",
    "compactor": "🗜️ 压缩",
    "verifier": "⚖️ 核查员",
    "writer": "✍️ 撰写人",
    "archivist": "🗄️ 归档",
}

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _new_thread_id() -> str:
    """秒级时间戳只用于可读性，UUID 后缀保证同秒并发也不会共用 checkpoint。"""
    return f"web-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"


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
            ev["replan_count"] = item.get("replan_count", 0)
            ev["memory_reused"] = item.get("memory_reused", [])
        elif node == "researcher":
            note = (item.get("notes") or [{}])[0]
            ev["subquestion"] = note.get("subquestion", "")
            ev["n_sources"] = len(note.get("sources", []))
        elif node == "compactor":
            ev["compaction"] = item.get("compaction", {})
        elif node == "verifier":
            ev["conflicts"] = item.get("conflicts", [])
            ev["gaps"] = item.get("gaps", [])
        elif node == "archivist":
            ev["memory_written"] = item.get("memory_written", 0)
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
            "budget_exceeded": state.get("budget_exceeded", ""),
            "remaining": BUDGET.remaining(),
            "compaction": state.get("compaction", {}),
            "degradations": reliability.degradations(),
            "memory_reused": len(state.get("memory_reused") or []),
            "memory_written": state.get("memory_written", 0),
            "memory_enabled": config.MEMORY_ENABLED,
        },
    })


def _stream(payload, cfg: dict, started: float):
    """公共生成器：流式执行图产出 SSE 事件；遇到 interrupt 时结束本次流，等待 /resume 续跑。

    整轮研究包在 run_scope + trace_context 里：usage/降级/熔断只属于当前 thread；
    中断时保留上下文供 /resume 续用，正常出报告后释放。
    """
    run_id = cfg["configurable"]["thread_id"]
    completed = False
    with run_context.run_scope(run_id):
        with observability.trace_context(run_id, trace_name="deep-research"):
            try:
                for update in get_research_app().stream(payload, cfg, stream_mode="updates"):
                    if "__interrupt__" in update:
                        intr = update["__interrupt__"][0]
                        value = intr.value or {}
                        yield sse({
                            "type": "interrupt",
                            "thread_id": run_id,
                            "kind": value.get("kind", "brief"),
                            "title": value.get("title", ""),
                            "content": value.get("content", ""),
                        })
                        return
                    for node, out in update.items():
                        if node in ("review_brief", "review_plan"):
                            continue  # 审核由 interrupt 事件负责
                        for ev in _describe(node, out):
                            ev.update({
                                "type": "node_done",
                                "label": NODE_LABELS.get(node, node),
                                "elapsed_s": round(time.time() - started, 1),
                            })
                            yield sse(ev)
                state = get_research_app().get_state(cfg).values or {}
                event = _report_event(state, time.time() - started)
                completed = True
                yield event
            except Exception as exc:
                yield sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            finally:
                if completed:
                    run_context.discard_run(run_id)


@app.get("/healthz")
def healthz():
    """配置自检：只读本地配置，不访问 LLM/搜索，未就绪返回 503。"""
    errors = config.runtime_config_errors()
    ready = not errors
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ok" if ready else "config_error",
            "model": config.LLM_MODEL,
            "search_provider": config.SEARCH_PROVIDER,
            "memory_enabled": config.MEMORY_ENABLED,
            "langfuse_enabled": observability.is_enabled(),
            "errors": errors,
        },
    )


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "model": config.LLM_MODEL,
            "provider": config.SEARCH_PROVIDER,
            "langfuse": observability.is_enabled(),
            "review_points": config.REVIEW_POINTS,
            "max_sub_default": config.MAX_SUBQUESTIONS,
            "max_sub_limit": config.MAX_SUBQUESTIONS_LIMIT,
            "rounds_default": config.MAX_SEARCH_ROUNDS,
            "rounds_limit": config.MAX_SEARCH_ROUNDS_LIMIT,
            "max_llm_calls": config.MAX_LLM_CALLS,
        },
    )


@app.get("/api/research/stream")
def research_stream(
    topic: str = Query(..., min_length=2),
    points: str | None = Query(None, description="审核点位，逗号分隔：brief,plan；空串=不审核"),
    max_sub: int | None = Query(None, description="子问题数量上限"),
    rounds: int | None = Query(None, description="每个子问题最大检索轮数"),
):
    # 越界参数直接 400，而不是静默降级成空报告（rounds<=0 会让 researcher 一次都不检索）
    try:
        eff_sub, eff_rounds = config.validate_scale(max_sub, rounds)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # points 缺省时跟随 .env 的 REVIEW_POINTS；显式传空串即全自动
    review_points = (
        list(config.REVIEW_POINTS) if points is None
        else [p.strip() for p in points.split(",") if p.strip()]
    )
    tid = _new_thread_id()
    cfg = thread_config(tid)
    # 规模覆盖走 state，不改全局 config —— 否则同进程内并发请求会互相覆盖
    payload = {
        "run_id": tid,
        "topic": topic,
        "notes": [],
        "review_points": review_points,
        "max_subquestions": eff_sub,
        "max_search_rounds": eff_rounds,
    }
    with run_context.run_scope(tid):
        USAGE.reset()       # 新 run 从零起算；并发请求各有独立上下文
        BUDGET.reset()
        reliability.reset_all()
    started = time.time()

    def gen():
        yield sse({"type": "start", "thread_id": tid, "topic": topic})
        yield from _stream(payload, cfg, started)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.get("/api/resume/stream")
def resume_stream(thread_id: str = Query(...), value: str = ""):
    """human-in-the-loop：value 为空表示确认原内容，非空表示整组替换（按当前中断的 kind 解释）。"""
    cfg = thread_config(thread_id)
    payload = Command(resume=value or "")
    with run_context.run_scope(thread_id):
        BUDGET.reset()              # 墙钟按这次续跑重算；usage 连续累计，不重复清零
        reliability.reset_breakers()  # 保留上一段的降级记录，只清冷却中的熔断器
    started = time.time()

    def gen():
        yield sse({"type": "resume", "thread_id": thread_id})
        yield from _stream(payload, cfg, started)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.get("/api/report/{thread_id}")
def get_report(thread_id: str):
    state = get_research_app().get_state({"configurable": {"thread_id": thread_id}}).values or {}
    return {"thread_id": thread_id, "report": state.get("report", "")}
