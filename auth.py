"""账号 / API token / RBAC / 配额 / 审计。

设计取舍：
- 不做浏览器登录与密码：由管理员用 `admin.py` 创建用户、签发一次性明文 token，
  服务端只存 SHA-256 哈希；适合小团队/API 型产品。
- 角色只有 admin / user：admin 可看 metrics/audit、跨 thread 访问；user 只能访问
  自己发起的 thread，并受每日研究配额约束。
- 存储跟随 `DATABASE_URL`：有 Postgres 用 Postgres，否则用 `AUTH_DB`（默认 auth.db）。
- 静态环境变量 token（APP_AUTH_TOKEN/APP_AUTH_TOKENS）仍兼容，视为 admin。
"""
import hashlib
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime

import config
import db

try:  # pg 可选：本地 SQLite 模式不需要
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

AUTH_SCHEMA_VERSION = 1
_PG_ERRORS = (psycopg.Error,) if psycopg is not None else ()
_DB_ERRORS = (sqlite3.Error, OSError, RuntimeError) + _PG_ERRORS

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT 'user',
    daily_quota INTEGER NOT NULL DEFAULT 20,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_tokens_hash ON api_tokens (token_hash);
CREATE TABLE IF NOT EXISTS thread_owners (
    thread_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_daily (
    user_id TEXT NOT NULL,
    day TEXT NOT NULL,
    runs INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    thread_id TEXT,
    request_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events (created_at DESC);
"""

SCHEMA_POSTGRES = SCHEMA_SQLITE.replace(
    "id INTEGER PRIMARY KEY AUTOINCREMENT", "id SERIAL PRIMARY KEY"
)


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    role: str
    daily_quota: int = 0
    token_id: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _pg_enabled(db_path: str | None) -> bool:
    return db_path is None and bool(config.DATABASE_URL.strip())


def _q(sql: str, pg: bool) -> str:
    return sql.replace("?", "%s") if pg else sql


def _split_schema(sql: str) -> list[str]:
    return [stmt.strip() for stmt in sql.split(";") if stmt.strip()]


def _ensure_schema(conn, pg: bool) -> None:
    for statement in _split_schema(SCHEMA_POSTGRES if pg else SCHEMA_SQLITE):
        conn.execute(statement)
    if pg:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS auth_schema (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO auth_schema (key, value) VALUES ('version', %s) "
            "ON CONFLICT (key) DO NOTHING",
            (str(AUTH_SCHEMA_VERSION),),
        )
    else:
        version = conn.execute("PRAGMA user_version").fetchone()[0] or 0
        if version < AUTH_SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {AUTH_SCHEMA_VERSION}")
    conn.commit()


_schema_lock = threading.Lock()
_schema_ready: set[str] = set()


def _schema_key(db_path: str | None) -> str:
    return f"pg:{config.DATABASE_URL}" if _pg_enabled(db_path) else f"sqlite:{db_path or config.AUTH_DB}"


def _connect(db_path: str | None = None):
    pg = _pg_enabled(db_path)
    if pg:
        if psycopg is None:
            raise RuntimeError("DATABASE_URL 已配置但未安装 psycopg")
        conn = psycopg.connect(config.DATABASE_URL)
    else:
        conn = db.connect(db_path or config.AUTH_DB, timeout=15)
    key = _schema_key(db_path)
    if key not in _schema_ready:
        with _schema_lock:
            if key not in _schema_ready:
                _ensure_schema(conn, pg)
                _schema_ready.add(key)
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_user(
    username: str,
    *,
    role: str = "user",
    daily_quota: int | None = None,
    db_path: str | None = None,
) -> dict:
    username = (username or "").strip()
    if not username:
        raise ValueError("username 不能为空")
    if role not in ("admin", "user"):
        raise ValueError("role 只能是 admin/user")
    quota = config.AUTH_DEFAULT_DAILY_QUOTA if daily_quota is None else int(daily_quota)
    if quota < 0:
        raise ValueError("daily_quota 不能为负数")

    pg = _pg_enabled(db_path)
    conn = _connect(db_path)
    try:
        try:
            conn.execute(
                _q(
                    "INSERT INTO users (username, role, daily_quota, active, created_at) "
                    "VALUES (?, ?, ?, 1, ?)",
                    pg,
                ),
                (username, role, quota, _now()),
            )
            conn.commit()
        except _DB_ERRORS as exc:
            raise ValueError(f"用户创建失败（可能已存在）：{username}") from exc
        row = conn.execute(
            _q(
                "SELECT id, username, role, daily_quota, active, created_at "
                "FROM users WHERE username = ?",
                pg,
            ),
            (username,),
        ).fetchone()
        return {
            "id": row[0],
            "username": row[1],
            "role": row[2],
            "daily_quota": row[3],
            "active": bool(row[4]),
            "created_at": row[5],
        }
    finally:
        conn.close()


def get_user(username: str, *, db_path: str | None = None) -> dict | None:
    pg = _pg_enabled(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            _q(
                "SELECT id, username, role, daily_quota, active, created_at "
                "FROM users WHERE username = ?",
                pg,
            ),
            (username,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "username": row[1],
        "role": row[2],
        "daily_quota": row[3],
        "active": bool(row[4]),
        "created_at": row[5],
    }


def list_users(*, db_path: str | None = None) -> list[dict]:
    pg = _pg_enabled(db_path)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, username, role, daily_quota, active, created_at FROM users ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r[0],
            "username": r[1],
            "role": r[2],
            "daily_quota": r[3],
            "active": bool(r[4]),
            "created_at": r[5],
        }
        for r in rows
    ]


def disable_user(username: str, *, db_path: str | None = None) -> int:
    pg = _pg_enabled(db_path)
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            _q("UPDATE users SET active = 0 WHERE username = ?", pg), (username,)
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def issue_token(
    username: str, *, name: str = "default", db_path: str | None = None
) -> tuple[str, int]:
    user = get_user(username, db_path=db_path)
    if not user or not user["active"]:
        raise ValueError(f"用户不存在或已禁用：{username}")
    token = secrets.token_urlsafe(32)
    pg = _pg_enabled(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            _q(
                "INSERT INTO api_tokens (user_id, name, token_hash, active, created_at) "
                "VALUES (?, ?, ?, 1, ?)",
                pg,
            ),
            (user["id"], name, hash_token(token), _now()),
        )
        conn.commit()
        row = conn.execute(
            _q("SELECT id FROM api_tokens WHERE token_hash = ?", pg), (hash_token(token),)
        ).fetchone()
        return token, row[0]
    finally:
        conn.close()


def list_tokens(username: str | None = None, *, db_path: str | None = None) -> list[dict]:
    pg = _pg_enabled(db_path)
    conn = _connect(db_path)
    try:
        if username:
            rows = conn.execute(
                _q(
                    """
                    SELECT t.id, u.username, t.name, t.active, t.created_at, t.last_used_at
                    FROM api_tokens t JOIN users u ON u.id = t.user_id
                    WHERE u.username = ? ORDER BY t.id DESC
                    """,
                    pg,
                ),
                (username,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT t.id, u.username, t.name, t.active, t.created_at, t.last_used_at
                FROM api_tokens t JOIN users u ON u.id = t.user_id
                ORDER BY t.id DESC
                """
            ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r[0],
            "username": r[1],
            "name": r[2],
            "active": bool(r[3]),
            "created_at": r[4],
            "last_used_at": r[5],
        }
        for r in rows
    ]


def disable_token(token_id: int, *, db_path: str | None = None) -> int:
    pg = _pg_enabled(db_path)
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            _q("UPDATE api_tokens SET active = 0 WHERE id = ?", pg), (int(token_id),)
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def authenticate_token(token: str, *, db_path: str | None = None) -> Principal | None:
    if not token:
        return None
    pg = _pg_enabled(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            _q(
                """
                SELECT t.id, u.id, u.username, u.role, u.daily_quota
                FROM api_tokens t JOIN users u ON u.id = t.user_id
                WHERE t.token_hash = ? AND t.active = 1 AND u.active = 1
                """,
                pg,
            ),
            (hash_token(token),),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            _q("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", pg),
            (_now(), row[0]),
        )
        conn.commit()
        return Principal(
            user_id=str(row[1]),
            username=row[2],
            role=row[3],
            daily_quota=int(row[4]),
            token_id=str(row[0]),
        )
    finally:
        conn.close()


def claim_thread(principal: Principal, thread_id: str, *, db_path: str | None = None) -> None:
    pg = _pg_enabled(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            _q(
                "INSERT INTO thread_owners (thread_id, user_id, created_at) "
                "VALUES (?, ?, ?) ON CONFLICT (thread_id) DO NOTHING",
                pg,
            ),
            (thread_id, principal.user_id, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def assert_thread_access(
    principal: Principal, thread_id: str, *, db_path: str | None = None
) -> None:
    if principal.is_admin:
        return
    pg = _pg_enabled(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            _q("SELECT user_id FROM thread_owners WHERE thread_id = ?", pg), (thread_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row or str(row[0]) != principal.user_id:
        raise PermissionError("无权访问该研究 thread")


def consume_quota(
    principal: Principal, *, day: str | None = None, db_path: str | None = None
) -> tuple[bool, int]:
    """消耗一次每日研究配额；返回 (是否允许, 剩余次数)。admin/0 配额表示不限。"""
    if principal.is_admin or principal.daily_quota <= 0:
        return True, -1
    day = day or _today()
    pg = _pg_enabled(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            _q(
                """
                INSERT INTO usage_daily (user_id, day, runs) VALUES (?, ?, 1)
                ON CONFLICT (user_id, day) DO UPDATE
                SET runs = usage_daily.runs + 1
                WHERE usage_daily.runs < ?
                RETURNING runs
                """,
                pg,
            ),
            (principal.user_id, day, principal.daily_quota),
        ).fetchone()
        conn.commit()
        if not row:
            return False, 0
        return True, principal.daily_quota - int(row[0])
    finally:
        conn.close()


def audit(
    user_id: str | None,
    action: str,
    *,
    detail: str = "",
    thread_id: str | None = None,
    request_id: str | None = None,
    db_path: str | None = None,
) -> None:
    pg = _pg_enabled(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            _q(
                "INSERT INTO audit_events "
                "(user_id, action, detail, thread_id, request_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                pg,
            ),
            (user_id, action, detail, thread_id, request_id, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def list_audit(limit: int = 50, *, db_path: str | None = None) -> list[dict]:
    pg = _pg_enabled(db_path)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            _q(
                "SELECT id, user_id, action, detail, thread_id, request_id, created_at "
                "FROM audit_events ORDER BY id DESC LIMIT ?",
                pg,
            ),
            (max(1, min(int(limit), 500)),),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r[0],
            "user_id": r[1],
            "action": r[2],
            "detail": r[3],
            "thread_id": r[4],
            "request_id": r[5],
            "created_at": r[6],
        }
        for r in rows
    ]


def stats(*, db_path: str | None = None) -> dict:
    pg = _pg_enabled(db_path)
    conn = _connect(db_path)
    try:
        users = conn.execute("SELECT count(*) FROM users WHERE active = 1").fetchone()[0]
        tokens = conn.execute(
            "SELECT count(*) FROM api_tokens t JOIN users u ON u.id = t.user_id "
            "WHERE t.active = 1 AND u.active = 1"
        ).fetchone()[0]
        events = conn.execute("SELECT count(*) FROM audit_events").fetchone()[0]
    finally:
        conn.close()
    return {"users": users, "tokens": tokens, "audit_events": events}


__all__ = [
    "Principal", "create_user", "get_user", "list_users", "disable_user",
    "issue_token", "list_tokens", "disable_token", "authenticate_token",
    "claim_thread", "assert_thread_access", "consume_quota", "audit",
    "list_audit", "stats", "hash_token",
]
