"""P1.5：备份加密/解密、key 文件与过期备份清理。"""
import os
import sqlite3
import time

import pytest
from cryptography.fernet import InvalidToken

import maintenance


def _make_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO t (value) VALUES ('secret')")
        conn.commit()
    finally:
        conn.close()


def test_encrypted_backup_roundtrip(tmp_path):
    source = tmp_path / "checkpoints.db"
    _make_db(source)
    key = maintenance.generate_key().encode("ascii")

    plain = maintenance.backup_database(source, tmp_path / "backups")
    encrypted = maintenance.encrypt_file(plain, key)
    assert not plain.exists()
    assert encrypted.name.endswith(".fernet")

    restored = maintenance.decrypt_file(encrypted, key, out=tmp_path / "restored.db")
    conn = sqlite3.connect(restored)
    try:
        assert conn.execute("SELECT value FROM t").fetchone()[0] == "secret"
    finally:
        conn.close()


def test_wrong_key_is_rejected(tmp_path):
    source = tmp_path / "source.db"
    _make_db(source)
    encrypted = maintenance.encrypt_file(
        maintenance.backup_database(source, tmp_path / "backups"),
        maintenance.generate_key().encode("ascii"),
    )
    with pytest.raises(InvalidToken):
        maintenance.decrypt_file(
            encrypted,
            maintenance.generate_key().encode("ascii"),
            out=tmp_path / "bad.db",
        )


def test_key_file_and_prune(tmp_path, monkeypatch):
    key = maintenance.generate_key()
    key_file = tmp_path / "backup.key"
    key_file.write_text(key + "\n", encoding="utf-8")
    monkeypatch.setattr(maintenance.config, "BACKUP_ENCRYPTION_KEY", "")
    assert maintenance.resolve_backup_key(str(key_file)) == key.encode("ascii")

    backups = tmp_path / "backups"
    backups.mkdir()
    old = backups / "old.db"
    old.write_bytes(b"old")
    old_time = time.time() - 40 * 86400
    os.utime(old, (old_time, old_time))
    kept = backups / "new.db"
    kept.write_bytes(b"new")

    result = maintenance.prune_backups(out_dir=backups, retention_days=30)
    assert str(old) in result["removed"]
    assert not old.exists()
    assert kept.exists()
