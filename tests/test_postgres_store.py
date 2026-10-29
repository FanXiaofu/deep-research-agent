"""P1.1：Postgres 存储后端（无 TEST_DATABASE_URL 时跳过）。"""
import os

import pytest

import config
import memory
from graph.build import make_checkpointer

TEST_URL = os.getenv("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL 未配置")


@pytest.fixture(autouse=True)
def _pg(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", TEST_URL)
    memory.reset()
    yield
    memory.reset()


def _note(subquestion="Q", content="Postgres 记忆", sources=None):
    return {
        "subquestion": subquestion,
        "content": content,
        "sources": sources or [{"title": "src", "url": "https://example.com"}],
    }


def test_memory_roundtrip_on_postgres():
    assert memory.remember("主题", [_note()]) == 1

    hits = memory.lookup(["Q"], threshold=0.0)
    assert hits["Q"]["note"] == "Postgres 记忆"
    assert hits["Q"]["sources"][0]["url"] == "https://example.com"

    stats = memory.stats()
    assert stats["entries"] == 1
    assert stats["schema_version"] == memory.SCHEMA_VERSION

    assert memory.reset() >= 1
    assert memory.stats()["entries"] == 0


def test_checkpointer_setup_on_postgres():
    saver = make_checkpointer()
    assert saver is not None
    # setup() 已在 make_checkpointer 内执行；重复调用应保持幂等
    saver.setup()


def test_sqlite_memory_can_migrate_to_postgres(tmp_path):
    sqlite_path = tmp_path / "memory.db"
    memory.remember("旧主题", [_note("迁移问题", "旧笔记")], db_path=str(sqlite_path))

    rows = memory.export_all(db_path=str(sqlite_path))
    imported = memory.import_all(rows)

    assert imported == 1
    hits = memory.lookup(["迁移问题"], threshold=0.0)
    assert hits["迁移问题"]["note"] == "旧笔记"
    assert memory.stats()["entries"] == 1
