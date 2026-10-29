"""研究档案：跨运行的长期记忆。

为什么需要：一轮研究要几分钟、几分钱 token，但结论不落档 —— 同一个问题（或高度相似的
子问题）第二次研究仍然从零检索。这里把"子问题 → 笔记摘要 → 来源"存进 SQLite/Postgres，
规划阶段先查记忆：命中的子问题直接复用笔记（不再派检索分支），未命中的才开分支。

三个克制的设计：
- **相似度用字符 bigram Jaccard**：中文不需要分词、不引新依赖、纯函数可单测；
- **记忆有寿命**（MEMORY_MAX_AGE_DAYS）：过期不复用。刷新只发生在"重新检索"时
  （updated_at），反复复用不会让旧结论变成永久记忆 —— 这是遗忘策略，不是 bug；
- **只复用笔记，不复用报告**：结论仍由 writer 基于证据重写，报告里注明复用来源与日期。

阈值是量出来的，而且量出来发现不能两全（实测 bigram Jaccard）：

    同一句 1.00 ｜ 近似重复（"…对比" vs "…对比分析"）0.87 ｜ 子集（"索引下推是什么" vs "索引下推"）0.73
    同义改写（"和/与、对比/差异"）0.39 ｜ 同题不同维度（"并发模型对比" vs "错误处理对比"）0.44 ｜ 无关 0.00

注意 0.39 < 0.44：**"该复用"和"绝不该复用"在 0.4 附近完全重叠**，任何阈值都跨不过去。
代价不对称（误复用 = 拿 A 维度的旧结论回答 B 维度；漏复用 = 多搜一次，即开启记忆前的行为），
因此阈值定在重叠区之上（默认 0.62），明确选择"宁可漏复用，不可误复用"。
要覆盖同义改写需要语义匹配（本地 embedding），当前刻意不做。

存储：默认 SQLite；设置 `DATABASE_URL` 后同一套接口走 Postgres（单实例迁移用）。
并发：查询发生在 planner（单节点），落档发生在 archivist（writer 之后的单节点），
并行 researcher 不碰记忆库。
"""
import json
import sqlite3
from datetime import datetime, timedelta

import config
import db

try:  # pg 是可选依赖：本地 SQLite 模式不需要
    import psycopg
except ImportError:  # pragma: no cover - 本地未安装
    psycopg = None

SCHEMA_VERSION = 1

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS research_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    topic_norm TEXT NOT NULL,
    subquestion TEXT NOT NULL,
    subquestion_norm TEXT NOT NULL,
    note TEXT NOT NULL,
    sources TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    used_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    UNIQUE (topic_norm, subquestion_norm)
);
CREATE INDEX IF NOT EXISTS idx_memory_updated ON research_memory (updated_at DESC);
"""

SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS research_memory (
    id SERIAL PRIMARY KEY,
    topic TEXT NOT NULL,
    topic_norm TEXT NOT NULL,
    subquestion TEXT NOT NULL,
    subquestion_norm TEXT NOT NULL,
    note TEXT NOT NULL,
    sources TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    used_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    UNIQUE (topic_norm, subquestion_norm)
);
CREATE INDEX IF NOT EXISTS idx_memory_updated ON research_memory (updated_at DESC);
"""

_PG_ERRORS = (psycopg.Error,) if psycopg is not None else ()
_DB_ERRORS = (sqlite3.Error, OSError, RuntimeError) + _PG_ERRORS

_PUNCT = str.maketrans("", "", " \t\r\n，。；：、！？,.;:!?（）()【】[]{}<>《》“”\"'`·-—_/+\\|*#@$%^&=")


def normalize(text: str) -> str:
    """归一化：去空白与标点、小写。中文不需要分词，字符 bigram 已足够表达相似度。"""
    return (text or "").lower().translate(_PUNCT)


def bigrams(text: str) -> set:
    norm = normalize(text)
    if len(norm) <= 1:
        return {norm} if norm else set()
    return {norm[i:i + 2] for i in range(len(norm) - 1)}


def bigram_jaccard(a: str, b: str) -> float:
    """字符 bigram 的 Jaccard 相似度（0~1），用于子问题级去重与记忆命中判定。"""
    set_a, set_b = bigrams(a), bigrams(b)
    if not set_a or not set_b:
        return 0.0
    return round(len(set_a & set_b) / len(set_a | set_b), 4)


def _pg_enabled(db_path: str | None) -> bool:
    return db_path is None and bool(config.DATABASE_URL.strip())


def _q(sql: str, pg: bool) -> str:
    return sql.replace("?", "%s") if pg else sql


def _executemany(conn, sql: str, rows: list[tuple]) -> None:
    # sqlite3.Cursor 不支持 with 语句；psycopg 的 cursor 可以，这里统一手动 close
    cur = conn.cursor()
    try:
        cur.executemany(sql, rows)
    finally:
        cur.close()


def _split_schema(sql: str) -> list[str]:
    return [stmt.strip() for stmt in sql.split(";") if stmt.strip()]


def _ensure_postgres(conn) -> None:
    for statement in _split_schema(SCHEMA_POSTGRES):
        conn.execute(statement)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memory_schema (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO memory_schema (key, value) VALUES ('version', %s) "
        "ON CONFLICT (key) DO NOTHING",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _migrate_sqlite(conn: sqlite3.Connection) -> None:
    """用 PRAGMA user_version 记录 schema 版本；后续版本在这里追加 ALTER。"""
    version = conn.execute("PRAGMA user_version").fetchone()[0] or 0
    if version < SCHEMA_VERSION:
        # v1：SCHEMA 已经包含全部表，旧库只需补写版本号
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()


def _schema_version(conn, pg: bool) -> int:
    if pg:
        row = conn.execute("SELECT value FROM memory_schema WHERE key='version'").fetchone()
        return int(row[0]) if row else 0
    return conn.execute("PRAGMA user_version").fetchone()[0] or 0


def _connect(db_path: str | None = None):
    """显式 db_path 时走 SQLite（测试/本地）；否则 DATABASE_URL 存在就走 Postgres。"""
    if _pg_enabled(db_path):
        if psycopg is None:
            raise RuntimeError(
                "DATABASE_URL 已配置但未安装 psycopg（pip install 'psycopg[binary]'）"
            )
        conn = psycopg.connect(config.DATABASE_URL)
        _ensure_postgres(conn)
        return conn
    conn = db.connect(db_path or config.MEMORY_DB, timeout=15)
    conn.executescript(SCHEMA_SQLITE)
    _migrate_sqlite(conn)
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _age_days(stamp: str) -> float:
    try:
        return round((datetime.now() - datetime.fromisoformat(stamp)).total_seconds() / 86400, 2)
    except (TypeError, ValueError):
        return 0.0


def lookup(subquestions: list, *, db_path: str | None = None, threshold: float | None = None,
           max_age_days: float | None = None, max_rows: int = 500) -> dict:
    """
    为每个子问题找一条可复用的历史笔记。

    返回 {子问题: {"id", "note", "sources", "topic", "updated_at", "age_days", "similarity"}}，
    未命中的子问题不出现在结果里。相似度取全局最优（跨主题复用是有意为之：
    "Rust 和 Go 的并发模型对比"这类子问题在不同主题下同样有效）。
    """
    if not subquestions:
        return {}

    threshold = config.MEMORY_SIMILARITY_THRESHOLD if threshold is None else threshold
    max_age_days = config.MEMORY_MAX_AGE_DAYS if max_age_days is None else max_age_days
    cutoff = datetime.now() - timedelta(days=max_age_days)
    pg = _pg_enabled(db_path)

    try:
        conn = _connect(db_path)
    except _DB_ERRORS:
        return {}

    try:
        rows = conn.execute(
            _q(
                """
                SELECT id, topic, subquestion, note, sources, updated_at, used_count
                FROM research_memory
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                pg,
            ),
            (max_rows,),
        ).fetchall()
    finally:
        conn.close()

    candidates = []
    for row in rows:
        try:
            if datetime.fromisoformat(row[5]) < cutoff:
                continue  # 过期记忆不复用
        except (TypeError, ValueError):
            continue
        candidates.append(row)

    hits = {}
    for sub in subquestions:
        best, best_score = None, 0.0
        for row in candidates:
            score = bigram_jaccard(sub, row[2])
            if score > best_score:
                best, best_score = row, score
        if best is not None and best_score >= threshold:
            hits[sub] = {
                "id": best[0],
                "topic": best[1],
                "subquestion": best[2],
                "note": best[3],
                "sources": json.loads(best[4] or "[]"),
                "updated_at": best[5],
                "age_days": _age_days(best[5]),
                "used_count": best[6],
                "similarity": best_score,
            }
    return hits


def remember(topic: str, notes: list, *, db_path: str | None = None) -> int:
    """
    落档：把本轮研究的笔记写入记忆库（同一主题+子问题则覆盖为最新结论）。

    只存有正文的笔记；来源列表原样保存，保证复用时的引用编号仍然完整。
    """
    rows = []
    now = _now()
    for note in notes or []:
        subquestion = str(note.get("subquestion") or "").strip()
        content = str(note.get("content") or "").strip()
        if not subquestion or not content:
            continue
        rows.append((
            topic or "",
            normalize(topic or ""),
            subquestion,
            normalize(subquestion),
            content,
            json.dumps(note.get("sources") or [], ensure_ascii=False),
            now,
            now,
        ))

    if not rows:
        return 0

    pg = _pg_enabled(db_path)
    try:
        conn = _connect(db_path)
    except _DB_ERRORS:
        return 0

    try:
        _executemany(
            conn,
            _q(
                """
                INSERT INTO research_memory (
                    topic, topic_norm, subquestion, subquestion_norm,
                    note, sources, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (topic_norm, subquestion_norm) DO UPDATE SET
                    note = excluded.note,
                    sources = excluded.sources,
                    updated_at = excluded.updated_at
                """,
                pg,
            ),
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    return len(rows)


def mark_used(ids: list, *, db_path: str | None = None) -> int:
    """记录一次记忆命中（用于统计命中率；不刷新 updated_at，避免旧结论被反复使用而永不过期）。"""
    ids = [int(i) for i in (ids or [])]
    if not ids:
        return 0

    pg = _pg_enabled(db_path)
    try:
        conn = _connect(db_path)
    except _DB_ERRORS:
        return 0

    try:
        _executemany(
            conn,
            _q(
                "UPDATE research_memory SET used_count = used_count + 1, last_used_at = ? "
                "WHERE id = ?",
                pg,
            ),
            [(_now(), i) for i in ids],
        )
        conn.commit()
    finally:
        conn.close()

    return len(ids)


def stats(*, db_path: str | None = None) -> dict:
    """记忆库概况：条目数、被复用过的条目数、命中次数、最老/最新时间。"""
    pg = _pg_enabled(db_path)
    try:
        conn = _connect(db_path)
    except _DB_ERRORS:
        return {"entries": 0, "used_entries": 0, "total_hits": 0, "schema_version": 0}

    try:
        row = conn.execute(
            """
            SELECT count(*), sum(CASE WHEN used_count > 0 THEN 1 ELSE 0 END),
                   sum(used_count), min(updated_at), max(updated_at)
            FROM research_memory
            """
        ).fetchone()
        version = _schema_version(conn, pg)
    finally:
        conn.close()

    return {
        "entries": row[0] or 0,
        "used_entries": row[1] or 0,
        "total_hits": row[2] or 0,
        "oldest_update": row[3],
        "newest_update": row[4],
        "schema_version": version,
    }


_EXPORT_COLUMNS = (
    "topic", "topic_norm", "subquestion", "subquestion_norm",
    "note", "sources", "created_at", "updated_at", "used_count", "last_used_at",
)


def export_all(*, db_path: str) -> list[tuple]:
    """导出 SQLite 记忆库全部记录（用于迁移到 Postgres）。"""
    conn = _connect(db_path)
    try:
        return conn.execute(
            f"SELECT {', '.join(_EXPORT_COLUMNS)} FROM research_memory"
        ).fetchall()
    finally:
        conn.close()


def import_all(rows: list[tuple], *, db_path: str | None = None) -> int:
    """把 export_all 的记录 upsert 到目标库；db_path=None 时走 DATABASE_URL。"""
    if not rows:
        return 0
    pg = _pg_enabled(db_path)
    conn = _connect(db_path)
    try:
        _executemany(
            conn,
            _q(
                f"""
                INSERT INTO research_memory ({', '.join(_EXPORT_COLUMNS)})
                VALUES ({', '.join(['?'] * len(_EXPORT_COLUMNS))})
                ON CONFLICT (topic_norm, subquestion_norm) DO UPDATE SET
                    note = excluded.note,
                    sources = excluded.sources,
                    updated_at = excluded.updated_at,
                    used_count = excluded.used_count,
                    last_used_at = excluded.last_used_at
                """,
                pg,
            ),
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def reset(*, db_path: str | None = None) -> int:
    """清空记忆库（评测前重置，保证两批次起点一致）。"""
    pg = _pg_enabled(db_path)
    try:
        conn = _connect(db_path)
    except _DB_ERRORS:
        return 0

    try:
        deleted = conn.execute(_q("DELETE FROM research_memory", pg)).rowcount
        conn.commit()
    finally:
        conn.close()

    return deleted
