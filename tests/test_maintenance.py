"""C1：WAL/schema 版本、checkpoint 保留、备份与恢复。"""
from datetime import datetime, timezone

import ormsgpack
import pytest

import db
import maintenance
import memory


def _make_checkpoint_db(path, entries):
    conn = db.connect(path)
    conn.executescript(
        """
        CREATE TABLE checkpoints (
            thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT,
            parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
        );
        CREATE TABLE writes (
            thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, task_id TEXT,
            idx INTEGER, channel TEXT, type TEXT, value BLOB,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
        );
        """
    )
    for thread_id, ts in entries:
        blob = ormsgpack.packb({"v": 4, "ts": ts, "id": "cp-" + thread_id})
        conn.execute(
            "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?, ?)",
            (thread_id, "", "cp-" + thread_id, None, "msgpack", blob, b"{}"),
        )
        conn.execute(
            "INSERT INTO writes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (thread_id, "", "cp-" + thread_id, "task", 0, "notes", "json", b"[]"),
        )
    conn.commit()
    conn.close()


def test_prune_removes_only_expired_threads(tmp_path):
    path = tmp_path / "checkpoints.db"
    _make_checkpoint_db(path, [
        ("old", "2020-01-01T00:00:00+00:00"),
        ("new", "2026-10-29T00:00:00+00:00"),
    ])

    result = maintenance.prune_checkpoints(
        retention_days=7,
        db_path=str(path),
        now=datetime(2026, 10, 29, tzinfo=timezone.utc),
    )

    assert result["threads"] == 1
    assert result["checkpoints"] == 1
    assert result["writes"] == 1
    conn = db.connect(path)
    try:
        assert [r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM checkpoints")] == ["new"]
        assert conn.execute("SELECT count(*) FROM writes").fetchone()[0] == 1
    finally:
        conn.close()


def test_prune_is_disabled_when_retention_is_zero(tmp_path):
    path = tmp_path / "checkpoints.db"
    _make_checkpoint_db(path, [("old", "2020-01-01T00:00:00+00:00")])

    result = maintenance.prune_checkpoints(retention_days=0, db_path=str(path))

    assert result["skipped"] == "retention_days<=0"
    conn = db.connect(path)
    try:
        assert conn.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 1
    finally:
        conn.close()


def test_memory_uses_wal_and_records_schema_version(tmp_path):
    path = tmp_path / "memory.db"

    conn = memory._connect(str(path))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == memory.SCHEMA_VERSION
    finally:
        conn.close()


def test_checkpointer_uses_wal(tmp_path, monkeypatch):
    import config
    from graph.build import make_checkpointer

    monkeypatch.setattr(config, "CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    saver = make_checkpointer()
    try:
        assert saver.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        saver.conn.close()


def _note(content):
    return {"subquestion": "Q", "content": content, "sources": []}


def test_backup_and_restore_round_trip(tmp_path):
    path = tmp_path / "memory.db"
    memory.remember("T", [_note("旧结论")], db_path=str(path))

    backup = maintenance.backup_database(path, tmp_path / "backups")
    assert backup.exists()

    memory.remember("T", [_note("新结论")], db_path=str(path))
    hits = memory.lookup(["Q"], db_path=str(path), threshold=0.0)
    assert hits["Q"]["note"] == "新结论"

    safety = maintenance.restore_database(backup, path)

    assert safety is not None and safety.exists()
    restored = memory.lookup(["Q"], db_path=str(path), threshold=0.0)
    assert restored["Q"]["note"] == "旧结论"


def test_restore_rejects_bad_backup(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_text("not a sqlite database", encoding="utf-8")

    with pytest.raises(Exception):
        maintenance.restore_database(bad, tmp_path / "target.db")
