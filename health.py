"""就绪检查：给负载均衡器和容器编排使用。

`/healthz` 是存活/配置检查（不访问外部依赖）；`/readyz` 额外检查
checkpoint/auth 数据库，并把 Redis 状态标为 ok/degraded/disabled。
Redis 故障时应用仍可回退进程内限流/指标，因此不阻止流量。
"""
import sqlite3
from pathlib import Path

import config
import db

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None
try:
    import redis
except ImportError:  # pragma: no cover
    redis = None


def _check_database() -> str:
    if config.DATABASE_URL.strip():
        if psycopg is None:
            raise RuntimeError("psycopg 未安装")
        conn = psycopg.connect(config.DATABASE_URL, connect_timeout=2)
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
        return "ok"
    conn = db.connect(config.CHECKPOINT_DB, timeout=2)
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return "ok"


def _check_auth() -> str:
    if config.DATABASE_URL.strip():
        return "ok"  # auth 表与 checkpoint 共用 Postgres，上面已检查
    path = Path(config.AUTH_DB)
    if not path.exists():
        return "not_initialized"
    conn = sqlite3.connect(str(path), timeout=2)
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return "ok"


def _check_redis() -> str:
    if not config.REDIS_URL.strip():
        return "disabled"
    if redis is None:
        return "degraded"
    client = redis.from_url(
        config.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    try:
        client.ping()
        return "ok"
    except Exception:
        return "degraded"
    finally:
        try:
            client.close()
        except Exception:
            pass


def readiness() -> tuple[dict, int]:
    checks: dict[str, str] = {}
    ready = True

    try:
        checks["database"] = _check_database()
    except Exception as exc:
        checks["database"] = f"error:{type(exc).__name__}"
        ready = False

    try:
        checks["auth"] = _check_auth()
    except Exception as exc:
        checks["auth"] = f"error:{type(exc).__name__}"
        ready = False

    try:
        checks["redis"] = _check_redis()
    except Exception as exc:
        checks["redis"] = f"degraded:{type(exc).__name__}"

    if not ready:
        status = "not_ready"
    elif checks["redis"].startswith("degraded"):
        status = "degraded"
    else:
        status = "ok"
    return {"status": status, "checks": checks}, 200 if ready else 503
