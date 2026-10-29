"""SQLite 维护 CLI：checkpoint 清理、备份、恢复。

用法：
    python maintenance.py prune-checkpoints --days 7
    python maintenance.py backup --out backups
    python maintenance.py restore backups/checkpoints-20261029-...db --target checkpoints.db --yes

备份使用 sqlite3 的 backup API（会包含 WAL 中的数据），不要直接拷贝正在写入的 .db。
恢复会覆盖目标库，默认拒绝执行；确认后加 --yes，且恢复前会自动再做一份安全备份。
"""
import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ormsgpack

import config
import db


def _checkpoint_ts(blob: bytes):
    """从 LangGraph checkpoint 的 msgpack 里取 ts；解析失败返回 None（保守不删）。"""
    try:
        obj = ormsgpack.unpackb(blob, ext_hook=lambda code, data: data)
        raw = obj.get("ts")
        if not raw:
            return None
        parsed = datetime.fromisoformat(str(raw))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def prune_checkpoints(
    *,
    retention_days: float | None = None,
    db_path: str | None = None,
    now: datetime | None = None,
) -> dict:
    """删除最后一轮 checkpoint 早于 cutoff 的 thread（整条 thread 一起删）。"""
    retention = config.CHECKPOINT_RETENTION_DAYS if retention_days is None else float(retention_days)
    path = Path(db_path or config.CHECKPOINT_DB)
    result = {"threads": 0, "checkpoints": 0, "writes": 0}
    if retention <= 0:
        return {**result, "skipped": "retention_days<=0"}
    if not path.exists():
        return {**result, "skipped": f"db not found: {path}"}

    conn = db.connect(path)
    try:
        if "checkpoints" not in _tables(conn):
            return {**result, "skipped": "checkpoints table not found"}
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention)
        threads = [row[0] for row in conn.execute("SELECT DISTINCT thread_id FROM checkpoints")]
        stale = []
        for thread_id in threads:
            row = conn.execute(
                "SELECT checkpoint FROM checkpoints WHERE thread_id=? ORDER BY rowid DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
            ts = _checkpoint_ts(row[0]) if row else None
            if ts is not None and ts < cutoff:
                stale.append(thread_id)

        with conn:
            for thread_id in stale:
                result["writes"] += conn.execute(
                    "DELETE FROM writes WHERE thread_id=?", (thread_id,)
                ).rowcount
                result["checkpoints"] += conn.execute(
                    "DELETE FROM checkpoints WHERE thread_id=?", (thread_id,)
                ).rowcount
        result["threads"] = len(stale)
        result["cutoff"] = cutoff.isoformat()
        return result
    finally:
        conn.close()


def backup_database(source: str | Path, out_dir: str | Path = "backups") -> Path:
    """在线备份单个 SQLite 库；目标文件名带微秒时间戳，避免同秒覆盖。"""
    src_path = Path(source)
    if not src_path.exists():
        raise FileNotFoundError(f"数据库不存在：{src_path}")
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    dest = out_path / f"{src_path.stem}-{datetime.now():%Y%m%d-%H%M%S-%f}{src_path.suffix}"

    src = db.connect(src_path)
    dst = sqlite3.connect(dest)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def restore_database(backup: str | Path, target: str | Path) -> Path | None:
    """用备份覆盖目标库：先校验备份，再给现有目标做安全备份；返回安全备份路径。"""
    backup_path = Path(backup)
    target_path = Path(target)
    if not backup_path.exists():
        raise FileNotFoundError(f"备份不存在：{backup_path}")

    check = sqlite3.connect(backup_path)
    try:
        verdict = check.execute("PRAGMA integrity_check").fetchone()[0]
        if verdict != "ok":
            raise ValueError(f"备份 integrity_check 未通过：{verdict}")
    finally:
        check.close()

    safety = None
    if target_path.exists():
        safety = backup_database(target_path, target_path.parent / "backups")

    src = db.connect(backup_path)
    dst = db.connect(target_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    return safety


def main() -> None:
    parser = argparse.ArgumentParser(description="Deep Research Agent SQLite 维护")
    sub = parser.add_subparsers(dest="command", required=True)

    prune = sub.add_parser("prune-checkpoints", help="清理过期 checkpoint")
    prune.add_argument("--days", type=float, help=f"保留天数（默认 {config.CHECKPOINT_RETENTION_DAYS}，0=不清理）")
    prune.add_argument("--db", help="checkpoint 库路径（默认 config.CHECKPOINT_DB）")

    backup = sub.add_parser("backup", help="在线备份 checkpoint 与 memory 库")
    backup.add_argument("--out", default="backups", help="备份目录")
    backup.add_argument("--checkpoint-db", default=config.CHECKPOINT_DB)
    backup.add_argument("--memory-db", default=config.MEMORY_DB)

    restore = sub.add_parser("restore", help="用备份覆盖目标库（会先做安全备份）")
    restore.add_argument("backup", help="备份文件路径")
    restore.add_argument("--target", required=True, help="要恢复的目标库路径")
    restore.add_argument("--yes", action="store_true", help="确认覆盖目标库")

    args = parser.parse_args()
    if args.command == "prune-checkpoints":
        print(json.dumps(
            prune_checkpoints(retention_days=args.days, db_path=args.db),
            ensure_ascii=False,
        ))
        return

    if args.command == "backup":
        backups = []
        for source in (args.checkpoint_db, args.memory_db):
            if Path(source).exists():
                backups.append(str(backup_database(source, args.out)))
        print(json.dumps({"backups": backups}, ensure_ascii=False))
        return

    if not args.yes:
        raise SystemExit("恢复会覆盖目标库；确认后加 --yes")
    safety = restore_database(args.backup, args.target)
    print(json.dumps({"restored": args.target, "safety_backup": str(safety) if safety else None}, ensure_ascii=False))


if __name__ == "__main__":
    main()
