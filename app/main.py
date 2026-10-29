"""FastAPI Web 界面：SSE 流式展示多 Agent 研究过程（W4）。

启动：python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

接口：
    GET /                          研究界面
    GET /healthz                   配置自检
    POST /api/research/stream      发起研究并流式返回进度（SSE，JSON body）
    POST /api/resume/stream        human-in-the-loop 审核后续跑（SSE，JSON body）
    GET /api/report/{thread_id}    获取某次研究的最终报告（Markdown）

并发：运行期状态（usage / 降级 / 熔断）按 thread_id 隔离（run_context）；
checkpoint 连接仍是进程级共享（SQLite 单连接）。
"""
import json
import logging
import queue
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import auth
import config
import health
import logging_config
import markdown as md_lib
import metrics
import nh3
import observability
import reliability
import run_context
from app import security
from budget import BUDGET
from fastapi import Depends, FastAPI, HTTPException, Path as ApiPath, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from langgraph.types import Command
from pydantic import BaseModel, Field

from graph.build import build_research_app, make_checkpointer, thread_config
from llm.client import USAGE


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 配置错误在启动时暴露，而不是等到检索阶段退化成空报告
    logging_config.setup_logging(config.LOG_LEVEL, config.LOG_JSON)
    config.validate_runtime_config()
    if not config.APP_AUTH_TOKEN.strip() and Path("/.dockerenv").exists():
        logger.warning(
            "docker_without_auth_token",
            extra={"path": "/"},
        )
    yield


app = FastAPI(title="Deep Research Agent", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)

_research_app = None
logger = logging.getLogger("deep_research.web")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@app.middleware("http")
async def reject_oversized_body(request: Request, call_next):
    """便宜的 Content-Length 上限；chunked 请求体仍需在反向代理层限制。"""
    raw = request.headers.get("content-length")
    if raw:
        try:
            size = int(raw)
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Content-Length 非法"})
        if size > config.APP_MAX_REQUEST_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": f"请求体过大（上限 {config.APP_MAX_REQUEST_BYTES} 字节）"},
            )
    return await call_next(request)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    """分配/透传 request_id，记录一次请求的结构化日志。"""
    candidate = request.headers.get("x-request-id", "")
    request_id = candidate if _REQUEST_ID_RE.match(candidate) else uuid.uuid4().hex[:12]
    token = logging_config.request_id_var.set(request_id)
    started = time.perf_counter()
    client = request.client.host if request.client else "-"
    try:
        response = await call_next(request)
        route = request.scope.get("route")
        metric_path = getattr(route, "path", request.url.path)
        metrics.record_request(metric_path, response.status_code)
        if request.url.path != "/healthz":
            logger.info(
                "http_request",
                extra={
                    "method": request.method,
                    "path": metric_path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "client": client,
                    "request_id": request_id,
                    "token_id": getattr(request.state, "token_id", "-"),
                },
            )
        response.headers["X-Request-ID"] = request_id
        return response
    except Exception:
        logger.exception(
            "http_request_failed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "client": client,
                "request_id": request_id,
            },
        )
        raise
    finally:
        logging_config.request_id_var.reset(token)


class ResearchRequest(BaseModel):
    topic: str = Field(..., min_length=2, max_length=config.APP_MAX_TOPIC_CHARS)
    points: list[str] | None = Field(default=None, max_length=2)
    max_sub: int | None = None
    rounds: int | None = None


class ResumeRequest(BaseModel):
    thread_id: str = Field(..., min_length=1, max_length=128)
    value: str = Field(default="", max_length=config.APP_MAX_REVIEW_CHARS)


_ALLOWED_TAGS = {
    "a", "p", "br", "strong", "em", "del", "sup", "sub",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "code", "pre", "hr",
    "table", "thead", "tbody", "tr", "th", "td", "img",
}
_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
    "code": {"class"},
    "th": {"align"},
    "td": {"align"},
}


def render_report_html(report: str) -> str:
    """Markdown → HTML 后按白名单清洗：报告内容含检索结果，不能直接 innerHTML。"""
    html = md_lib.markdown(report, extensions=["extra"])
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        clean_content_tags={"script", "style"},
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer",
    )

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
        "html": render_report_html(report),
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
            "citation_audit": state.get("citation_audit") or {},
            "request_id": logging_config.get_request_id(),
        },
    })


def _stream(
    payload, cfg: dict, started: float, request_id: str | None = None, user_id: str | None = None
):
    """公共生成器：流式执行图产出 SSE 事件；遇到 interrupt 时结束本次流，等待 /resume 续跑。

    图在独立 worker 线程里执行，事件经队列回到 SSE 生成器：sync 生成器可能被
    Starlette 在线程池里分多次 next() 推进，ContextVar token 不能跨 context 重置，
    因此 run_scope、request/thread 日志上下文和 trace_context 必须固定在 worker 的同一个 context 内。
    中断时保留上下文供 /resume 续用，正常出报告后释放。
    """
    run_id = cfg["configurable"]["thread_id"]
    request_id = request_id or logging_config.get_request_id()
    events: queue.Queue = queue.Queue()
    completed = False

    def worker():
        outcome = "failed"
        metrics.research_started()
        with (
            logging_config.request_scope(request_id),
            logging_config.thread_scope(run_id),
            run_context.run_scope(run_id),
        ):
            with observability.trace_context(run_id, trace_name="deep-research"):
                logger.info("research_started", extra={"thread_id": run_id})
                try:
                    for update in get_research_app().stream(payload, cfg, stream_mode="updates"):
                        events.put(("update", update))
                        if "__interrupt__" in update:
                            outcome = "interrupted"
                            break
                    else:
                        state = get_research_app().get_state(cfg).values or {}
                        events.put(("report", _report_event(state, time.time() - started)))
                        outcome = "completed"
                except Exception as exc:
                    logger.exception("research_failed", extra={"thread_id": run_id})
                    events.put(("error", f"{type(exc).__name__}: {exc}"))
                finally:
                    logger.info(
                        "research_finished",
                        extra={"thread_id": run_id, "outcome": outcome},
                    )
                    metrics.research_finished(outcome)
                    if user_id:
                        try:
                            auth.audit(
                                user_id,
                                "research_finished",
                                detail=outcome,
                                thread_id=run_id,
                                request_id=request_id,
                            )
                        except Exception:
                            logger.warning("audit_failed", extra={"thread_id": run_id})
                    events.put(("done", None))

    threading.Thread(target=worker, daemon=True).start()

    while True:
        try:
            kind, data = events.get(timeout=15)
        except queue.Empty:
            # Render/反向代理会断开长时间无数据的连接；SSE 注释帧不触发前端事件。
            yield ": heartbeat\n\n"
            continue
        if kind == "done":
            break
        if kind == "error":
            yield sse({
                "type": "error",
                "message": data,
                "thread_id": run_id,
                "request_id": request_id,
            })
            continue
        if kind == "report":
            completed = True
            yield data
            continue

        update = data
        if "__interrupt__" in update:
            intr = update["__interrupt__"][0]
            value = intr.value or {}
            yield sse({
                "type": "interrupt",
                "thread_id": run_id,
                "request_id": request_id,
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
                    "request_id": request_id,
                })
                yield sse(ev)

    if completed:
        run_context.discard_run(run_id)


@app.get("/metrics", dependencies=[Depends(security.require_admin)])
def metrics_endpoint():
    """Prometheus 文本格式的运行指标（admin）。"""
    return PlainTextResponse(
        metrics.render(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/metrics.json", dependencies=[Depends(security.require_admin)])
def metrics_json():
    """便于 curl/人工排查的 JSON 快照（admin）。"""
    return metrics.snapshot()


@app.get("/api/audit", dependencies=[Depends(security.require_admin)])
def audit_log(limit: int = 50):
    """最近的管理/研究审计事件（admin）。"""
    return {"events": auth.list_audit(max(1, min(limit, 200)))}


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
            "auth_required": bool(security.configured_tokens())
            or Path(config.AUTH_DB).exists()
            or bool(config.DATABASE_URL),
            "errors": errors,
        },
    )


@app.get("/readyz")
def readyz():
    """就绪检查：数据库不可用时 503；Redis 故障标 degraded 但仍可服务。"""
    body, status = health.readiness()
    return JSONResponse(status_code=status, content=body)


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
            "auth_required": bool(security.configured_tokens())
            or Path(config.AUTH_DB).exists()
            or bool(config.DATABASE_URL),
        },
    )


@app.post(
    "/api/research/stream",
    dependencies=[Depends(security.require_auth), Depends(security.enforce_rate_limit)],
)
def research_stream(payload: ResearchRequest, request: Request):
    request_id = logging_config.get_request_id()
    principal = request.state.principal
    # 越界参数直接 400，而不是静默降级成空报告（rounds<=0 会让 researcher 一次都不检索）
    try:
        eff_sub, eff_rounds = config.validate_scale(payload.max_sub, payload.rounds)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # points 缺省时跟随 .env 的 REVIEW_POINTS；显式传空列表即全自动
    review_points = (
        list(config.REVIEW_POINTS) if payload.points is None
        else [p.strip() for p in payload.points if p.strip()]
    )
    allowed, quota_remaining = auth.consume_quota(principal)
    if not allowed:
        raise HTTPException(status_code=429, detail="今日研究额度已用完")

    tid = _new_thread_id()
    cfg = thread_config(tid)
    # 规模覆盖走 state，不改全局 config —— 否则同进程内并发请求会互相覆盖
    state = {
        "run_id": tid,
        "topic": payload.topic,
        "notes": [],
        "review_points": review_points,
        "max_subquestions": eff_sub,
        "max_search_rounds": eff_rounds,
    }
    auth.claim_thread(principal, tid)
    auth.audit(
        principal.user_id,
        "research_start",
        thread_id=tid,
        request_id=request_id,
        detail=payload.topic[:120],
    )
    with run_context.run_scope(tid):
        USAGE.reset()       # 新 run 从零起算；并发请求各有独立上下文
        BUDGET.reset()
        reliability.reset_all()
    started = time.time()

    def gen():
        yield sse({
            "type": "start",
            "thread_id": tid,
            "topic": payload.topic,
            "request_id": request_id,
            "quota_remaining": quota_remaining,
        })
        yield from _stream(
            state, cfg, started, request_id=request_id, user_id=principal.user_id
        )

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.post(
    "/api/resume/stream",
    dependencies=[Depends(security.require_auth), Depends(security.enforce_rate_limit)],
)
def resume_stream(payload: ResumeRequest, request: Request):
    """human-in-the-loop：value 为空表示确认原内容，非空表示整组替换（按当前中断的 kind 解释）。"""
    request_id = logging_config.get_request_id()
    principal = request.state.principal
    try:
        auth.assert_thread_access(principal, payload.thread_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    auth.audit(
        principal.user_id,
        "research_resume",
        thread_id=payload.thread_id,
        request_id=request_id,
    )
    cfg = thread_config(payload.thread_id)
    command = Command(resume=payload.value or "")
    with run_context.run_scope(payload.thread_id):
        BUDGET.reset()              # 墙钟按这次续跑重算；usage 连续累计，不重复清零
        reliability.reset_breakers()  # 保留上一段的降级记录，只清冷却中的熔断器
    started = time.time()

    def gen():
        yield sse({"type": "resume", "thread_id": payload.thread_id, "request_id": request_id})
        yield from _stream(
            command, cfg, started, request_id=request_id, user_id=principal.user_id
        )

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.get("/api/report/{thread_id}", dependencies=[Depends(security.require_auth)])
def get_report(request: Request, thread_id: str = ApiPath(..., max_length=128)):
    principal = request.state.principal
    try:
        auth.assert_thread_access(principal, thread_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    state = get_research_app().get_state({"configurable": {"thread_id": thread_id}}).values or {}
    report = state.get("report", "")
    return {
        "thread_id": thread_id,
        "report": report,
        "html": render_report_html(report) if report else "",
    }
