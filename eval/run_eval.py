"""评测 runner：对评测集逐题跑研究流水线 → LLM-as-judge 评分 → 汇总。

用法：
    python eval/run_eval.py --tag baseline --limit 8      # 跑前 8 题，结果存 results/baseline/
    python eval/run_eval.py --tag tuned --limit 8         # 同一批题的对照批次
    python eval/run_eval.py --compare baseline tuned      # 生成 results/comparison.md

评分维度：
    faithfulness      报告论断能否在研究笔记中找到依据（LLM judge，1~5）
    coverage          报告对子问题/任务书维度的覆盖度（LLM judge，1~5）
    citation_quality  引用标注规范性（LLM judge，1~5）
    citation_validity 正文 [n] 引用未越界比例（确定性校验，0~1）
    source_utilization 参考来源被实际引用的比例（确定性校验，0~1）
    tokens / cost_usd 单次研究的 token 用量与成本估算
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 以脚本方式运行时把项目根目录加入可导入路径

import config
from graph.build import build_research_app, make_checkpointer
from judge import judge_report
from llm.client import USAGE

EVAL_DIR = Path(__file__).resolve().parent
RESULTS_DIR = EVAL_DIR / "results"
DATASET_PATH = EVAL_DIR / "golden_questions.jsonl"

SCORE_KEYS = ["faithfulness", "coverage", "citation_quality"]
HARD_KEYS = ["citation_validity", "source_utilization"]


def load_questions(limit: int, offset: int) -> list[dict]:
    questions = [json.loads(line) for line in DATASET_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    return questions[offset: offset + limit]


def count_refs(report: str) -> int:
    match = re.search(r"## 参考来源\n\n(.+)$", report, re.DOTALL)
    if not match:
        return 0
    return len([line for line in match.group(1).strip().splitlines() if re.match(r"^\d+\.\s", line.strip())])


def config_snapshot() -> dict:
    return {
        "llm_model": config.LLM_MODEL,
        "llm_base_url": config.LLM_BASE_URL,
        "max_subquestions": config.MAX_SUBQUESTIONS,
        "max_search_rounds": config.MAX_SEARCH_ROUNDS,
        "max_results_per_search": config.MAX_RESULTS_PER_SEARCH,
        "research_page_chars": config.RESEARCH_PAGE_CHARS,
        "note_min_chars": config.NOTE_MIN_CHARS,
        "note_max_chars": config.NOTE_MAX_CHARS,
        "writer_strict_grounding": config.WRITER_STRICT_GROUNDING,
        "price_input_per_m": config.LLM_PRICE_INPUT_PER_M,
        "price_output_per_m": config.LLM_PRICE_OUTPUT_PER_M,
    }


def run_question(app, q: dict, tag: str, out_dir: Path) -> dict:
    USAGE.reset()
    tid = f"eval-{tag}-{q['id']}"
    thread_cfg = {"configurable": {"thread_id": tid}}
    started = time.time()
    row = {"id": q["id"], "category": q["category"], "question": q["question"]}

    try:
        final = app.invoke(
            {"topic": q["question"], "notes": [], "require_review": False}, thread_cfg
        )
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["elapsed_s"] = round(time.time() - started, 1)
        return row

    report = final.get("report", "")
    notes = final.get("notes", [])
    if not report:
        row["error"] = "未生成报告"
        return row

    (out_dir / f"{q['id']}.md").write_text(report, encoding="utf-8")
    (out_dir / f"{q['id']}_state.json").write_text(
        json.dumps(
            {
                "brief": final.get("brief", ""),
                "subquestions": final.get("subquestions", []),
                "conflicts": final.get("conflicts", []),
                "notes": notes,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    n_refs = count_refs(report)
    usage = USAGE.snapshot()
    cost = (
        usage["input_tokens"] / 1e6 * config.LLM_PRICE_INPUT_PER_M
        + usage["output_tokens"] / 1e6 * config.LLM_PRICE_OUTPUT_PER_M
    )
    scores = judge_report(
        question=q["question"],
        brief=final.get("brief", ""),
        subquestions=final.get("subquestions", []),
        report=report,
        notes=notes,
        n_refs=n_refs,
    )
    row.update(scores)
    row.update(
        {
            "n_refs": n_refs,
            "llm_calls": usage["calls"],
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "cost_usd_est": round(cost, 4),
            "elapsed_s": round(time.time() - started, 1),
        }
    )
    (out_dir / f"{q['id']}_score.json").write_text(
        json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return row


def mean(rows: list[dict], key: str):
    values = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
    return round(sum(values) / len(values), 3) if values else None


def evaluate(args) -> None:
    questions = load_questions(args.limit, args.offset)
    if not questions:
        raise SystemExit("评测集为空或 offset 越界")
    out_dir = RESULTS_DIR / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"== 评测批次 {args.tag}：{len(questions)} 题 | 模型 {config.LLM_MODEL} ==", flush=True)
    app = build_research_app(make_checkpointer())
    rows = []
    for i, q in enumerate(questions, 1):
        print(f"[{i}/{len(questions)}] {q['id']} {q['question'][:40]} ...", flush=True)
        row = run_question(app, q, args.tag, out_dir)
        if row.get("error"):
            print(f"    ✗ 失败：{row['error'][:120]}", flush=True)
        else:
            print(
                f"    ✓ faithful={row['faithfulness']} coverage={row['coverage']} "
                f"cite={row['citation_quality']}/{row['citation_validity']} "
                f"tokens={row['input_tokens']}+{row['output_tokens']} {row['elapsed_s']}s",
                flush=True,
            )
        rows.append(row)
        # 每题落盘一次，中途中断也有部分结果
        summary = {
            "tag": args.tag,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "config": config_snapshot(),
            "rows": rows,
            "means": {k: mean(rows, k) for k in SCORE_KEYS + HARD_KEYS + ["cost_usd_est", "elapsed_s"]},
        }
        (out_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    write_summary_md(summary, out_dir)
    print(f"\n== 完成：{out_dir / 'summary.md'} ==", flush=True)


def write_summary_md(summary: dict, out_dir: Path) -> None:
    rows, means, cfg = summary["rows"], summary["means"], summary["config"]
    lines = [
        f"# 评测批次：{summary['tag']}",
        "",
        f"- 时间：{summary['finished_at']}　|　研究模型：`{cfg['llm_model']}`　|　judge：同模型",
        f"- 配置：page_chars={cfg['research_page_chars']}, note={cfg['note_min_chars']}~{cfg['note_max_chars']}"
        f", strict_grounding={cfg['writer_strict_grounding']}, max_sub={cfg['max_subquestions']}, rounds={cfg['max_search_rounds']}",
        "",
        "| 题目 | 忠实度 | 覆盖度 | 引用(LLM) | 引用有效 | 来源利用 | tokens(入/出) | 耗时s |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r.get("error"):
            lines.append(f"| {r['id']} {r['question'][:16]} | ERROR: {r['error'][:60]} | - | - | - | - | - | - |")
            continue
        lines.append(
            f"| {r['id']} {r['question'][:16]} | {r['faithfulness']} | {r['coverage']} "
            f"| {r['citation_quality']} | {r['citation_validity']} | {r['source_utilization']} "
            f"| {r['input_tokens']}/{r['output_tokens']} | {r['elapsed_s']} |"
        )
    lines += [
        "",
        f"**均值**：忠实度 {means['faithfulness']} ｜ 覆盖度 {means['coverage']} ｜ 引用质量 {means['citation_quality']}"
        f" ｜ 引用有效性 {means['citation_validity']} ｜ 来源利用率 {means['source_utilization']}"
        f" ｜ 单题成本估算 ${means['cost_usd_est']} ｜ 单题耗时 {means['elapsed_s']}s",
        "",
        "> 注：judge 与研究使用同一模型，小模型当 judge 有系统性偏差，分数用于 A/B 相对对比；",
        "> 正式对外数字建议用更强模型（如 DeepSeek-V3）复评并跑全量 20 题。",
        "",
    ]
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def compare(tag_a: str, tag_b: str) -> None:
    data = {}
    for tag in (tag_a, tag_b):
        path = RESULTS_DIR / tag / "summary.json"
        if not path.exists():
            raise SystemExit(f"缺少 {path}，先跑该批次")
        data[tag] = json.loads(path.read_text(encoding="utf-8"))

    lines = [
        f"# A/B 对比：{tag_a} → {tag_b}",
        "",
        "## 配置差异",
        "",
        "| 配置项 | " + tag_a + " | " + tag_b + " |",
        "|---|---|---|",
    ]
    for key in data[tag_a]["config"]:
        va, vb = data[tag_a]["config"][key], data[tag_b]["config"][key]
        mark = " ←" if va != vb else ""
        lines.append(f"| {key} | {va} | {vb}{mark} |".replace(" |←", " ←|"))

    lines += [
        "",
        "## 指标均值对比",
        "",
        "| 指标 | " + tag_a + " | " + tag_b + " | Δ |",
        "|---|---|---|---|",
    ]
    label = {"faithfulness": "忠实度(1-5)", "coverage": "覆盖度(1-5)", "citation_quality": "引用质量(1-5)",
             "citation_validity": "引用有效性(0-1)", "source_utilization": "来源利用率(0-1)",
             "cost_usd_est": "单题成本估算($)", "elapsed_s": "单题耗时(s)"}
    for key in SCORE_KEYS + HARD_KEYS + ["cost_usd_est", "elapsed_s"]:
        va, vb = data[tag_a]["means"].get(key), data[tag_b]["means"].get(key)
        delta = round(vb - va, 3) if isinstance(va, (int, float)) and isinstance(vb, (int, float)) else "-"
        lines.append(f"| {label.get(key, key)} | {va} | {vb} | {delta} |")

    lines += ["", "## 逐题分数", "", "| 题目 | " + f"{tag_a} 忠实/覆盖/引用 | {tag_b} 忠实/覆盖/引用 |", "|---|---|---|"]
    rows_a = {r["id"]: r for r in data[tag_a]["rows"]}
    rows_b = {r["id"]: r for r in data[tag_b]["rows"]}
    for qid in rows_a:
        ra, rb = rows_a[qid], rows_b.get(qid, {})
        def fmt(r):
            if r.get("error"):
                return "ERROR"
            return f"{r.get('faithfulness')}/{r.get('coverage')}/{r.get('citation_quality')}"
        lines.append(f"| {qid} {rows_a[qid]['question'][:20]} | {fmt(ra)} | {fmt(rb)} |")

    lines += [
        "",
        f"> judge 模型：`{data[tag_a]['config']['llm_model']}`（两批次相同）；n={len(rows_a)}。",
        "> 改进建议：跑全量 20 题、用更强模型复评后再写进简历。",
        "",
    ]
    out = RESULTS_DIR / "comparison.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"== 对比已生成：{out} ==", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="深度研究助手评测")
    parser.add_argument("--tag", help="批次名（baseline / tuned ...）")
    parser.add_argument("--limit", type=int, default=8, help="评测题数（默认 8，全集 20 题）")
    parser.add_argument("--offset", type=int, default=0, help="从第几题开始")
    parser.add_argument("--max-sub", type=int, help="覆盖 config.MAX_SUBQUESTIONS（两批次需一致）")
    parser.add_argument("--rounds", type=int, help="覆盖 config.MAX_SEARCH_ROUNDS")
    parser.add_argument("--compare", nargs=2, metavar=("TAG_A", "TAG_B"), help="生成两个批次的对比报告")
    args = parser.parse_args()

    if args.max_sub:
        config.MAX_SUBQUESTIONS = args.max_sub
    if args.rounds:
        config.MAX_SEARCH_ROUNDS = args.rounds

    if args.compare:
        compare(args.compare[0], args.compare[1])
    elif args.tag:
        evaluate(args)
    else:
        parser.error("需要 --tag 或 --compare")


if __name__ == "__main__":
    main()
