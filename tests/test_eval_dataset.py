"""评测集规模/分层/唯一性校验。"""
import json
from collections import Counter

from eval.run_eval import DATASET_PATH, SUITES, load_questions


def _rows():
    return [
        json.loads(line)
        for line in DATASET_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_dataset_is_stratified_and_unique():
    rows = _rows()
    assert len(rows) == 110
    assert len({r["id"] for r in rows}) == len(rows)
    assert len({r["question"] for r in rows}) == len(rows)
    counts = Counter(s for r in rows for s in r.get("suites", []))
    assert counts["core"] == 80
    assert counts["smoke"] == 10
    assert counts["regression"] == 50
    assert counts["adversarial"] == 30
    assert counts["holdout"] == 10
    for row in rows:
        assert row["question"].strip()
        assert row["must_cover"]
        assert set(row["suites"]) <= set(SUITES) - {"all"}


def test_suite_filtering():
    assert len(load_questions(999, 0, "smoke")) == 10
    assert len(load_questions(999, 0, "core")) == 80
    assert len(load_questions(999, 0, "regression")) == 50
    assert len(load_questions(999, 0, "adversarial")) == 30
    assert len(load_questions(999, 0, "holdout")) == 10
    assert len(load_questions(999, 0, "all")) == 110
