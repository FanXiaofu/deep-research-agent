"""数据维护 CLI：SQLite 清理/备份/恢复 + Postgres pg_dump/pg_restore + 加密。

用法：
    python maintenance.py gen-key
    python maintenance.py prune-checkpoints --days 7
    python maintenance.py backup --encrypt --out backups
    python maintenance.py pg-backup --encrypt --out backups
    python maintenance.py restore backups/checkpoints-...db.fernet --target checkpoints.db --key-file backup.key --yes

SQLite 备份使用 sqlite3 的 backup API（会包含 WAL 中的数据），不要直接拷贝正在写入的 .db。
恢复会覆盖目标库，默认拒绝执行；确认后加 --yes，且恢复前会自动再做一份安全备份。
加密备份使用 Fernet；key 丢失后备份不可恢复。
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ormsgpack
from cryptography.fernet import Fernet

import config
import db
import memory


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


def generate_key() -> str:
    """生成 Fernet key；明文只在创建时显示一次。"""
    return Fernet.generate_key().decode("ascii")


def resolve_backup_key(key_file: str | None = None) -> bytes:
    raw = (
        Path(key_file).read_text(encoding="utf-8").strip()
        if key_file
        else config.BACKUP_ENCRYPTION_KEY.strip()
    )
    if not raw:
        raise ValueError("缺少备份加密 key：设置 BACKUP_ENCRYPTION_KEY 或传 --key-file")
    try:
        Fernet(raw.encode("ascii"))
    except Exception as exc:
        raise ValueError("备份加密 key 格式错误（需要 Fernet base64 key）") from exc
    return raw.encode("ascii")


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def encrypt_file(path: str | Path, key: bytes, *, remove_plaintext: bool = True) -> Path:
    src = Path(path)
    dest = Path(str(src) + ".fernet")
    _write_private(dest, Fernet(key).encrypt(src.read_bytes()))
    if remove_plaintext:
        src.unlink(missing_ok=True)
    return dest


def decrypt_file(path: str | Path, key: bytes, *, out: str | Path | None = None) -> Path:
    src = Path(path)
    dest = Path(out) if out else Path(str(src).removesuffix(".fernet"))
    _write_private(dest, Fernet(key).decrypt(src.read_bytes()))
    return dest


def prune_backups(
    *,
    out_dir: str | Path = "backups",
    retention_days: float = 30,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    path = Path(out_dir)
    if retention_days <= 0 or not path.exists():
        return {"removed": [], "bytes": 0}
    cutoff = (now or datetime.now(timezone.utc)).timestamp() - retention_days * 86400
    removed = []
    freed = 0
    for item in path.iterdir():
        if not item.is_file() or not item.name.endswith((".db", ".dump", ".fernet")):
            continue
        if item.stat().st_mtime >= cutoff:
            continue
        freed += item.stat().st_size
        removed.append(str(item))
        if not dry_run:
            item.unlink()
    return {"removed": removed, "bytes": freed, "dry_run": dry_run}


def _sync_remote(source: str | Path, remote: str) -> None:
    rclone = shutil.which("rclone")
    if not rclone:
        raise RuntimeError("未找到 rclone；安装后再使用 --rclone-remote")
    subprocess.run([rclone, "copy", str(source), remote], check=True)


def postgres_backup(
    database_url: str,
    *,
    out_dir: str | Path = "backups",
    key: bytes | None = None,
    remote: str | None = None,
) -> Path:
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        raise RuntimeError("未找到 pg_dump；请在装有 PostgreSQL client 的环境执行")
    if remote and key is None:
        raise ValueError("--rclone-remote 只允许与 --encrypt 一起使用，避免把明文备份同步出去")
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    dest = out_path / f"postgres-{datetime.now():%Y%m%d-%H%M%S-%f}.dump"
    subprocess.run(
        [pg_dump, "--format=custom", "--no-owner", "--file", str(dest),
         "--dbname", database_url],
        check=True,
    )
    if key is not None:
        dest = encrypt_file(dest, key)
    if remote:
        _sync_remote(dest, remote)
    return dest


def postgres_restore(
    backup: str | Path,
    database_url: str,
    *,
    key: bytes | None = None,
) -> None:
    pg_restore = shutil.which("pg_restore")
    if not pg_restore:
        raise RuntimeError("未找到 pg_restore；请在装有 PostgreSQL client 的环境执行")
    backup_path = Path(backup)
    if backup_path.suffix == ".fernet":
        if key is None:
            raise ValueError("备份已加密，需要 --key-file 或 BACKUP_ENCRYPTION_KEY")
        with tempfile.TemporaryDirectory() as tmp:
            plain = decrypt_file(backup_path, key, out=Path(tmp) / "restore.dump")
            subprocess.run(
                [pg_restore, "--clean", "--if-exists", "--no-owner",
                 "--dbname", database_url, str(plain)],
                check=True,
            )
        return
    subprocess.run(
        [pg_restore, "--clean", "--if-exists", "--no-owner",
         "--dbname", database_url, str(backup_path)],
        check=True,
    )


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
    parser = argparse.ArgumentParser(description="Deep Research Agent 数据维护")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("gen-key", help="生成备份加密 key（Fernet）")

    prune = sub.add_parser("prune-checkpoints", help="清理过期 checkpoint")
    prune.add_argument("--days", type=float, help=f"保留天数（默认 {config.CHECKPOINT_RETENTION_DAYS}，0=不清理）")
    prune.add_argument("--db", help="checkpoint 库路径（默认 config.CHECKPOINT_DB）")

    prune_backups = sub.add_parser("prune-backups", help="清理过期备份文件")
    prune_backups.add_argument("--dir", default="backups")
    prune_backups.add_argument("--days", type=float, default=30)
    prune_backups.add_argument("--dry-run", action="store_true")

    backup = sub.add_parser("backup", help="在线备份 checkpoint、memory 与 auth 库")
    backup.add_argument("--out", default="backups", help="备份目录")
    backup.add_argument("--checkpoint-db", default=config.CHECKPOINT_DB)
    backup.add_argument("--memory-db", default=config.MEMORY_DB)
    backup.add_argument("--auth-db", default=config.AUTH_DB)
    backup.add_argument("--encrypt", action="store_true", help="使用 Fernet 加密并删除明文备份")
    backup.add_argument("--key-file", help="加密 key 文件（默认读 BACKUP_ENCRYPTION_KEY）")
    backup.add_argument("--rclone-remote", help="加密后 rclone copy 到远端（需安装 rclone）")

    restore = sub.add_parser("restore", help="用备份覆盖目标库（会先做安全备份）")
    restore.add_argument("backup", help="备份文件路径（.fernet 会自动解密）")
    restore.add_argument("--target", required=True, help="要恢复的目标库路径")
    restore.add_argument("--key-file", help="加密 key 文件（默认读 BACKUP_ENCRYPTION_KEY）")
    restore.add_argument("--yes", action="store_true", help="确认覆盖目标库")

    pg_backup = sub.add_parser("pg-backup", help="pg_dump 自定义格式备份（可加密/同步远端）")
    pg_backup.add_argument("--out", default="backups")
    pg_backup.add_argument("--database-url", default=config.DATABASE_URL)
    pg_backup.add_argument("--encrypt", action="store_true")
    pg_backup.add_argument("--key-file")
    pg_backup.add_argument("--rclone-remote")

    pg_restore = sub.add_parser("pg-restore", help="pg_restore 覆盖目标 Postgres（危险）")
    pg_restore.add_argument("backup")
    pg_restore.add_argument("--database-url", default=config.DATABASE_URL)
    pg_restore.add_argument("--key-file")
    pg_restore.add_argument("--yes", action="store_true")

    migrate = sub.add_parser("migrate-memory", help="SQLite 研究档案迁移到 Postgres")
    migrate.add_argument("--from", dest="source", required=True, help="SQLite memory.db 路径")
    migrate.add_argument("--database-url", default=config.DATABASE_URL,
                         help="目标 Postgres URL（默认读 DATABASE_URL）")

    args = parser.parse_args()
    if args.command == "gen-key":
        print(json.dumps({"key": generate_key()}, ensure_ascii=False))
        return
    if args.command == "prune-backups":
        print(json.dumps(
            prune_backups(
                out_dir=args.dir, retention_days=args.days, dry_run=args.dry_run
            ),
            ensure_ascii=False,
        ))
        return
    if args.command == "pg-backup":
        if not args.database_url:
            raise SystemExit("需要 --database-url 或 DATABASE_URL")
        key = resolve_backup_key(args.key_file) if args.encrypt else None
        dest = postgres_backup(
            args.database_url, out_dir=args.out, key=key, remote=args.rclone_remote
        )
        print(json.dumps({"backup": str(dest), "encrypted": key is not None}, ensure_ascii=False))
        return
    if args.command == "pg-restore":
        if not args.database_url:
            raise SystemExit("需要 --database-url 或 DATABASE_URL")
        if not args.yes:
            raise SystemExit("pg-restore 会覆盖目标库；确认后加 --yes")
        key = resolve_backup_key(args.key_file) if Path(args.backup).suffix == ".fernet" else None
        postgres_restore(args.backup, args.database_url, key=key)
        print(json.dumps({"restored": args.backup}, ensure_ascii=False))
        return
    if args.command == "migrate-memory":
        if not args.database_url:
            raise SystemExit("需要 --database-url 或 .env DATABASE_URL")
        rows = memory.export_all(db_path=args.source)
        config.DATABASE_URL = args.database_url
        imported = memory.import_all(rows)
        print(json.dumps(
            {"exported": len(rows), "imported": imported, "target": "postgres"},
            ensure_ascii=False,
        ))
        return
    if config.DATABASE_URL.strip():
        raise SystemExit(
            "DATABASE_URL 已启用：SQLite 命令不可用；"
            "Postgres 请使用 pg-backup / pg-restore（或 pg_dump/pg_restore）。"
        )
    if args.command == "prune-checkpoints":
        print(json.dumps(
            prune_checkpoints(retention_days=args.days, db_path=args.db),
            ensure_ascii=False,
        ))
        return

    if args.command == "backup":
        key = resolve_backup_key(args.key_file) if args.encrypt else None
        if args.rclone_remote and key is None:
            raise SystemExit("--rclone-remote 只允许与 --encrypt 一起使用")
        backups = []
        for source in (args.checkpoint_db, args.memory_db, args.auth_db):
            if Path(source).exists():
                dest = backup_database(source, args.out)
                if key is not None:
                    dest = encrypt_file(dest, key)
                if args.rclone_remote:
                    _sync_remote(dest, args.rclone_remote)
                backups.append(str(dest))
        print(json.dumps({"backups": backups, "encrypted": key is not None}, ensure_ascii=False))
        return

    if not args.yes:
        raise SystemExit("恢复会覆盖目标库；确认后加 --yes")
    backup_path = Path(args.backup)
    key = None
    if backup_path.suffix == ".fernet":
        key = resolve_backup_key(args.key_file)
        with tempfile.TemporaryDirectory() as tmp:
            plain = decrypt_file(backup_path, key, out=Path(tmp) / "restore.db")
            safety = restore_database(plain, args.target)
            if safety and key is not None:
                safety = encrypt_file(safety, key)
            print(json.dumps(
                {"restored": args.target, "safety_backup": str(safety) if safety else None},
                ensure_ascii=False,
            ))
        return
    safety = restore_database(backup_path, args.target)
    print(json.dumps({"restored": args.target, "safety_backup": str(safety) if safety else None}, ensure_ascii=False))


if __name__ == "__main__":
    main()
