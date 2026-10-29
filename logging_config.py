"""结构化日志：JSON 行 + request_id / thread_id 上下文。

Web 请求由中间件分配 request_id；研究线程由 run_scope 绑定 thread_id。
日志统一输出到 stdout，容器里可直接被 docker logs / 日志采集器读取。
"""
import contextvars
import json
import logging
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

request_id_var = contextvars.ContextVar("request_id", default="-")
thread_id_var = contextvars.ContextVar("thread_id", default="-")

_EXTRA_FIELDS = (
    "method", "path", "status", "duration_ms", "client",
    "topic", "thread_id", "prompt_tokens", "num_ctx",
)


def get_request_id() -> str:
    return request_id_var.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id_var.get()),
            "thread_id": getattr(record, "thread_id", thread_id_var.get()),
        }
        for field in _EXTRA_FIELDS:
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        request_id = getattr(record, "request_id", request_id_var.get())
        thread_id = getattr(record, "thread_id", thread_id_var.get())
        line = (
            f"{self.formatTime(record, '%Y-%m-%dT%H:%M:%S')} "
            f"{record.levelname} {record.name} req={request_id} thread={thread_id} "
            f"{record.getMessage()}"
        )
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging(level: str = "INFO", json_format: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_format else TextFormatter())
    root.addHandler(handler)


@contextmanager
def request_scope(request_id: str):
    token = request_id_var.set(request_id)
    try:
        yield
    finally:
        request_id_var.reset(token)


@contextmanager
def thread_scope(thread_id: str | None):
    token = thread_id_var.set(thread_id or "-")
    try:
        yield
    finally:
        thread_id_var.reset(token)
