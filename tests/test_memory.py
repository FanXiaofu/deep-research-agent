"""研究档案（长期记忆）：相似度、命中、寿命、覆盖写、命中统计。"""
import sqlite3
from datetime import datetime, timedelta

import pytest

import config
import memory


@pytest.fixture(autouse=True)
def _temp_memory(tmp_path, monkeypatch):
    """每个用例用独立的记忆库文件，绝不碰仓库里的 memory.db。"""
    db = tmp_path / "memory_test.db"
    monkeypatch.setattr(config, "MEMORY_DB", str(db))
    monkeypatch.setattr(config, "MEMORY_SIMILARITY_THRESHOLD", 0.62)
    monkeypatch.setattr(config, "MEMORY_MAX_AGE_DAYS", 30)
    yield db


def _note(sub: str, content: str = "笔记正文" * 10, urls=("https://a.test/1",)):
    return {
        "subquestion": sub,
        "content": content,
        "sources": [{"title": u, "url": u} for u in urls],
    }


# ---------- 相似度 ----------

def test_jaccard_identical_is_one():
    assert memory.bigram_jaccard("Rust 和 Go 的并发模型对比", "Rust 和 Go 的并发模型对比") == 1.0


def test_jaccard_ignores_punctuation_and_case():
    assert memory.bigram_jaccard("Redis 分布式锁!", "redis分布式锁") == 1.0


def test_jaccard_unrelated_is_low():
    assert memory.bigram_jaccard("Redis 分布式锁怎么实现", "Kubernetes 探针有哪几种") < 0.2


def test_jaccard_partial_overlap_between_unrelated_and_identical():
    score = memory.bigram_jaccard("Rust 和 Go 的并发模型对比", "Rust 与 Go 并发模型差异")
    assert 0.3 < score < 1.0


def test_normalize_handles_empty():
    assert memory.bigram_jaccard("", "任意") == 0.0


# ---------- 落档与命中 ----------

def test_remember_then_lookup_hits():
    stored = memory.remember("测试主题", [_note("Rust 和 Go 的并发模型对比")])
    assert stored == 1

    hits = memory.lookup(["Rust 和 Go 的并发模型对比"])
    assert len(hits) == 1

    hit = hits["Rust 和 Go 的并发模型对比"]
    assert hit["note"].startswith("笔记正文")
    assert hit["sources"][0]["url"] == "https://a.test/1"
    assert hit["similarity"] == 1.0
    assert hit["age_days"] >= 0


def test_lookup_fuzzy_match_above_threshold():
    """近似重复（多了个尾巴）应当命中：这是真实复用场景里最常见的形态。"""
    memory.remember("主题A", [_note("Rust 和 Go 的并发模型对比")])

    hits = memory.lookup(["Rust 和 Go 的并发模型对比分析"])
    assert len(hits) == 1
    assert hits["Rust 和 Go 的并发模型对比分析"]["similarity"] >= 0.62


def test_lookup_matches_subset_phrasing():
    memory.remember("主题A", [_note("MySQL 索引下推是什么")])
    assert memory.lookup(["MySQL 索引下推"]) != {}


def test_deliberate_miss_on_same_topic_different_dimension():
    """
    同题材不同维度（并发模型 vs 错误处理）实测相似度 0.44，而真正该复用的同义改写只有 0.39 ——
    两个类别在 0.4 附近重叠，阈值跨不过去。这里固化取舍：**宁可漏复用，不可误复用**。
    """
    memory.remember("主题A", [_note("Rust 和 Go 的并发模型对比")])

    assert memory.bigram_jaccard("Rust 和 Go 的并发模型对比", "Rust 和 Go 的错误处理对比") < 0.62
    assert memory.lookup(["Rust 和 Go 的错误处理对比"]) == {}


def test_deliberate_miss_on_paraphrase():
    """同义改写（和/与、对比/差异）只有 0.39，同样会被漏掉 —— 需要语义匹配才能覆盖。"""
    memory.remember("主题A", [_note("Rust 和 Go 的并发模型对比")])
    assert memory.lookup(["Rust 与 Go 并发模型差异"]) == {}


def test_lookup_misses_unrelated_subquestion():
    memory.remember("主题A", [_note("Rust 和 Go 的并发模型对比")])
    assert memory.lookup(["Kubernetes 探针有哪几种"]) == {}


def test_lookup_cross_topic_reuse_is_intended():
    """跨主题复用是有意为之：同一个子问题在不同主题下同样有效。"""
    memory.remember("主题A", [_note("向量数据库选型对比")])
    assert memory.lookup(["向量数据库选型对比"]) != {}


def test_remember_upserts_same_key():
    memory.remember("主题A", [_note("同一个子问题", content="旧结论" * 10)])
    memory.remember("主题A", [_note("同一个子问题", content="新结论" * 10)])

    assert memory.stats()["entries"] == 1
    assert memory.lookup(["同一个子问题"])["同一个子问题"]["note"].startswith("新结论")


def test_remember_skips_empty_notes():
    assert memory.remember("主题A", [_note("空笔记", content="   ")]) == 0
    assert memory.stats()["entries"] == 0


def test_expired_memory_is_not_reused(_temp_memory):
    memory.remember("主题A", [_note("Rust 和 Go 的并发模型对比")])

    stale = (datetime.now() - timedelta(days=45)).isoformat(timespec="seconds")
    conn = sqlite3.connect(_temp_memory)
    conn.execute("UPDATE research_memory SET updated_at = ?", (stale,))
    conn.commit()
    conn.close()

    assert memory.lookup(["Rust 和 Go 的并发模型对比"]) == {}
    assert memory.stats()["entries"] == 1  # 过期的记忆仍在库里，只是不再复用


def test_mark_used_counts_without_renewing_expiry(_temp_memory):
    memory.remember("主题A", [_note("Rust 和 Go 的并发模型对比")])
    hit = memory.lookup(["Rust 和 Go 的并发模型对比"])["Rust 和 Go 的并发模型对比"]

    memory.mark_used([hit["id"]])

    stats = memory.stats()
    assert stats["total_hits"] == 1
    assert stats["used_entries"] == 1

    # 复用不刷新 updated_at：否则旧结论会被反复使用而永不过期
    conn = sqlite3.connect(_temp_memory)
    before = conn.execute("SELECT updated_at FROM research_memory").fetchone()[0]
    conn.close()
    memory.mark_used([hit["id"]])
    conn = sqlite3.connect(_temp_memory)
    after = conn.execute("SELECT updated_at FROM research_memory").fetchone()[0]
    conn.close()
    assert before == after


def test_reset_clears_memory():
    memory.remember("主题A", [_note("子问题一"), _note("子问题二")])
    assert memory.reset() == 2
    assert memory.stats()["entries"] == 0


def test_lookup_on_missing_db_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DB", str(tmp_path / "nested" / "none.db"))
    # 目录不存在时 sqlite 打不开：必须降级为空结果，而不是抛异常
    assert memory.lookup(["任意子问题"]) == {}
