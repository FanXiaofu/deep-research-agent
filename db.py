"""SQLite 连接工厂：统一开启 WAL、busy_timeout 与 NORMAL 同步。

checkpoint 与 memory 都是单文件 SQLite；Web 并发写时默认的 rollback journal
容易直接报 "database is locked"。WAL 允许读写并行，busy_timeout 则给短事务
留出重试窗口。备份请使用 sqlite3 的 backup API（maintenance.py），不要直接
拷贝正在写入的 .db 文件。
"""
import sqlite3


def connect(path, *, check_same_thread: bool = True, timeout: float = 30.0) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=check_same_thread, timeout=timeout)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass  # :memory: / 只读库不允许切换，保持原模式
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn
