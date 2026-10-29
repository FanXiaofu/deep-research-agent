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
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根目录
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 直接 import run_eval 时也能找到 judge.py

import config
import memory
import observability
import reliability
import run_context
from budget import BUDGET
from graph.build import build_research_app, make_checkpointer, thread_config
from graph.state import effective_notes
from judge import citation_checks, judge_report
from llm.client import USAGE

EVAL_DIR = Path(__file__).resolve().parent
RESULTS_DIR = EVAL_DIR / "results"
DATASET_PATH = EVAL_DIR / "golden_questions.jsonl"

SCORE_KEYS = ["faithfulness", "coverage", "citation_quality", "must_cover_coverage"]
HARD_KEYS = ["citation_validity", "source_utilization"]
GROUNDING_KEYS = [
    "grounding_claims",
    "grounding_low_support",
    "grounding_numeric_mismatch",
    "grounding_semantic_unsupported",
]


SUITES = ("all", "smoke", "core", "regression", "adversarial", "holdout")


def load_questions(limit: int, offset: int, suite: str = "all") -> list[dict]:
    questions = [json.loads(line) for line in DATASET_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    if suite != "all":
        questions = [q for q in questions if suite in (q.get("suites") or [])]
    return questions[offset: offset + limit]


def count_refs(report: str) -> int:
    match = re.search(r"## 参考来源\n\n(.+)$", report, re.DOTALL)
    if not match:
        return 0
    return len([line for line in match.group(1).strip().splitlines() if re.match(r"^\d+\.\s", line.strip())])


def percentile(values: list, p: float):
    """线性插值分位数；空列表返回 None。"""
    nums = sorted(v for v in values if isinstance(v, (int, float)))
    if not nums:
        return None
    if len(nums) == 1:
        return round(nums[0], 3)
    pos = (len(nums) - 1) * p
    low = int(pos)
    high = min(low + 1, len(nums) - 1)
    return round(nums[low] + (nums[high] - nums[low]) * (pos - low), 3)


def percentile_snapshot(rows: list[dict]) -> dict:
    out = {}
    for key in ("elapsed_s", "cost_usd_est", "input_tokens", "output_tokens"):
        values = [
            r.get(key) for r in rows
            if not r.get("error") and isinstance(r.get(key), (int, float))
        ]
        out[f"{key}_p50"] = percentile(values, 0.5)
        out[f"{key}_p95"] = percentile(values, 0.95)
    return out


def apply_overrides(items: list[str] | None) -> dict:
    """按 config 现有类型解析 --set KEY=VALUE，用于单变量 A/B。"""
    applied = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--set 需要 KEY=VALUE，收到 {item!r}")
        key, raw = item.split("=", 1)
        key = key.strip()
        if not hasattr(config, key):
            raise ValueError(f"未知配置项：{key}")
        current = getattr(config, key)
        try:
            if isinstance(current, bool):
                value = raw.strip().lower() in {"1", "true", "yes", "on"}
            elif isinstance(current, int):
                value = int(raw)
            elif isinstance(current, float):
                value = float(raw)
            else:
                value = raw
        except ValueError as exc:
            raise ValueError(f"{key} 的类型不匹配：{raw!r}") from exc
        setattr(config, key, value)
        applied[key] = value
    return applied


def config_snapshot(
    max_subquestions: int, max_search_rounds: int, overrides: dict | None = None
) -> dict:
    return {
        "llm_model": config.LLM_MODEL,
        "judge_provider": config.JUDGE_PROVIDER,
        "judge_model": config.JUDGE_MODEL
        or (
            "deepseek/deepseek-v4-pro"
            if config.JUDGE_PROVIDER == "commandcode"
            else config.LLM_MODEL
        ),
        "llm_base_url": config.LLM_BASE_URL,
        "overrides": overrides or {},
        "max_subquestions": max_subquestions,
        "max_search_rounds": max_search_rounds,
        "max_results_per_search": config.MAX_RESULTS_PER_SEARCH,
        "research_page_chars": config.RESEARCH_PAGE_CHARS,
        "note_min_chars": config.NOTE_MIN_CHARS,
        "note_max_chars": config.NOTE_MAX_CHARS,
        "writer_strict_grounding": config.WRITER_STRICT_GROUNDING,
        "price_input_per_m": config.LLM_PRICE_INPUT_PER_M,
        "price_output_per_m": config.LLM_PRICE_OUTPUT_PER_M,
        # 长流程控制（A/B 时若两批不一致，对比表会标出来）
        "max_llm_calls": config.MAX_LLM_CALLS,
        "max_total_tokens": config.MAX_TOTAL_TOKENS,
        "max_wall_clock_s": config.MAX_WALL_CLOCK_S,
        "max_replan_rounds": config.MAX_REPLAN_ROUNDS,
        "compaction_enabled": config.COMPACTION_ENABLED,
        "compact_trigger_chars": config.COMPACT_TRIGGER_CHARS,
        # 研究档案（长期记忆）：开启后命中记忆的题目会跳过检索，两批次必须一致才有可比性
        "memory_enabled": config.MEMORY_ENABLED,
        "memory_similarity_threshold": config.MEMORY_SIMILARITY_THRESHOLD,
        "memory_max_age_days": config.MEMORY_MAX_AGE_DAYS,
    }


def run_question(app, q: dict, tag: str, out_dir: Path,
                 max_subquestions: int, max_search_rounds: int) -> dict:
    # 每次评测都用新 thread：同 tag 重跑不会续用上次的 checkpoint（notes 会被追加）
    tid = f"eval-{tag}-{q['id']}-{uuid.uuid4().hex[:8]}"
    run_context.discard_run(tid)
    run_context.bind_run(tid)
    USAGE.reset()
    BUDGET.reset()          # 预算与熔断按题计：上一题的降级不应污染本题的记录
    reliability.reset_all()
    thread_cfg = thread_config(tid)
    started = time.time()
    row = {"id": q["id"], "category": q["category"], "question": q["question"]}

    # 整题（研究 + 评分）包成一条 trace，便于在 Langfuse 里按 session 回看
    with observability.trace_context(
        tid, trace_name="eval-research", metadata={"question": q["question"], "tag": tag}
    ):
        try:
            final = app.invoke(
                {
                    "run_id": tid,
                    "topic": q["question"],
                    "notes": [],
                    "review_points": [],
                    "max_subquestions": max_subquestions,
                    "max_search_rounds": max_search_rounds,
                },
                thread_cfg,
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

        # 忠实度的对照物必须与 writer 实际看到的证据一致：压缩生效时用压缩视图，
        # 否则"报告 vs 未压缩原文"的比对会系统性偏松。
        evidence = effective_notes(final) or notes
        (out_dir / f"{q['id']}.md").write_text(report, encoding="utf-8")
        (out_dir / f"{q['id']}_state.json").write_text(
            json.dumps(
                {
                    "brief": final.get("brief", ""),
                    "subquestions": final.get("subquestions", []),
                    "conflicts": final.get("conflicts", []),
                    "gaps": final.get("gaps", []),
                    "replan_count": final.get("replan_count", 0),
                    "budget_exceeded": final.get("budget_exceeded", ""),
                    "remaining": BUDGET.remaining(),
                    "compaction": final.get("compaction", {}),
                    "memory_reused": final.get("memory_reused", []),
                    "memory_written": final.get("memory_written", 0),
                    "citation_audit": final.get("citation_audit", {}),
                    "grounding_audit": final.get("grounding_audit", {}),
                    "safety_audit": final.get("safety_audit", {}),
                    "degradations": reliability.degradations(),
                    "n_notes_raw": len(notes),
                    "n_notes_evidence": len(evidence),
                    "notes": evidence,
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
            notes=evidence,
            n_refs=n_refs,
            must_cover=q.get("must_cover", []),
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
                "budget_exceeded": final.get("budget_exceeded", ""),
                "replan_count": final.get("replan_count", 0),
                "compacted": bool(final.get("compaction", {}).get("applied")),
                "memory_reused": len(final.get("memory_reused") or []),
                "memory_written": final.get("memory_written", 0),
                "invalid_citations": (final.get("citation_audit") or {}).get("invalid_citations", 0),
                "unknown_links": (final.get("citation_audit") or {}).get("unknown_links", 0),
                "grounding_claims": int((final.get("grounding_audit") or {}).get("claims", 0) or 0),
                "grounding_low_support": len((final.get("grounding_audit") or {}).get("low_support") or []),
                "grounding_numeric_mismatch": int((final.get("grounding_audit") or {}).get("numeric_mismatch", 0) or 0),
                "grounding_semantic_unsupported": int(
                    ((final.get("grounding_audit") or {}).get("semantic") or {}).get("unsupported", 0) or 0
                ),
                "safety_risk": (final.get("safety_audit") or {}).get("risk", ""),
                "n_degradations": len(reliability.degradations()),
            }
        )
        (out_dir / f"{q['id']}_score.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return row


def mean(rows: list[dict], key: str):
    values = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
    return round(sum(values) / len(values), 3) if values else None


def evaluate(args, max_subquestions: int, max_search_rounds: int) -> None:
    questions = load_questions(args.limit, args.offset, args.suite)
    if not questions:
        raise SystemExit("评测集为空或 offset 越界")
    out_dir = RESULTS_DIR / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)

    judge_model = config.JUDGE_MODEL or (
        "deepseek/deepseek-v4-pro"
        if config.JUDGE_PROVIDER == "commandcode"
        else config.LLM_MODEL
    )
    print(
        f"== 评测批次 {args.tag}：{len(questions)} 题 | 研究 {config.LLM_MODEL} | "
        f"judge {judge_model} ({config.JUDGE_PROVIDER}) ==",
        flush=True,
    )
    if getattr(args, "overrides", None):
        print(f"== 单变量覆盖：{args.overrides} ==", flush=True)
    if config.MEMORY_ENABLED:
        print(
            f"== 研究档案：已启用（阈值 {config.MEMORY_SIMILARITY_THRESHOLD}，"
            f"寿命 {config.MEMORY_MAX_AGE_DAYS:g} 天）｜ 当前 {memory.stats()['entries']} 条记忆 ==",
            flush=True,
        )
    app = build_research_app(make_checkpointer())
    rows = []
    for i, q in enumerate(questions, 1):
        print(f"[{i}/{len(questions)}] {q['id']} {q['question'][:40]} ...", flush=True)
        row = run_question(app, q, args.tag, out_dir, max_subquestions, max_search_rounds)
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
            "suite": args.suite,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "config": config_snapshot(
                max_subquestions, max_search_rounds, overrides=getattr(args, "overrides", None)
            ),
            "rows": rows,
            "means": {
                k: mean(rows, k)
                for k in SCORE_KEYS + HARD_KEYS + GROUNDING_KEYS + ["cost_usd_est", "elapsed_s"]
            },
            "percentiles": percentile_snapshot(rows),
        }
        (out_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    write_summary_md(summary, out_dir)
    observability.flush()
    print(f"\n== 完成：{out_dir / 'summary.md'} ==", flush=True)


def write_summary_md(summary: dict, out_dir: Path) -> None:
    rows, means, cfg = summary["rows"], summary["means"], summary["config"]
    judge_model = cfg.get("judge_model") or cfg["llm_model"]
    judge_provider = cfg.get("judge_provider", "local")
    pct = summary.get("percentiles", {})
    lines = [
        f"# 评测批次：{summary['tag']}",
        "",
        f"- 子集：`{summary.get('suite', 'all')}`　|　时间：{summary['finished_at']}　|　研究模型：`{cfg['llm_model']}`　|　judge：`{judge_model}`（`{judge_provider}`）",
        f"- 配置：page_chars={cfg.get('research_page_chars', '-')}, "
        f"note={cfg.get('note_min_chars', '-')}~{cfg.get('note_max_chars', '-')}"
        f", strict_grounding={cfg.get('writer_strict_grounding', '-')}"
        f", max_sub={cfg.get('max_subquestions', '-')}, rounds={cfg.get('max_search_rounds', '-')}",
        f"- 预算：{cfg.get('max_llm_calls', '-')} 调用 / {cfg.get('max_total_tokens', '-')} tokens / "
        f"{cfg.get('max_wall_clock_s', '-')}s"
        f"　|　重规划上限 {cfg.get('max_replan_rounds', '-')}"
        f"　|　压缩触发 {cfg.get('compact_trigger_chars', '-')} 字"
        f"（{'开' if cfg.get('compaction_enabled') else '关/旧批次未记录'}）",
    ]
    if cfg.get("overrides"):
        lines.append(f"- 单变量覆盖：`{cfg['overrides']}`")
    lines += [
        "",
        "| 题目 | 忠实度 | 覆盖度 | 要点覆盖 | 引用(LLM) | 引用有效 | 来源利用 | tokens(入/出) | 耗时s | 状态 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r.get("error"):
            lines.append(f"| {r['id']} {r['question'][:16]} | ERROR: {r['error'][:60]} | - | - | - | - | - | - | - | - |")
            continue
        flags = []
        if r.get("budget_exceeded"):
            flags.append(f"预算:{r['budget_exceeded']}")
        if r.get("compacted"):
            flags.append("已压缩")
        if r.get("replan_count"):
            flags.append(f"重规划{r['replan_count']}")
        if r.get("memory_reused"):
            flags.append(f"记忆{r['memory_reused']}")
        if r.get("n_degradations"):
            flags.append(f"降级{r['n_degradations']}")
        if r.get("grounding_low_support"):
            flags.append(f"低支撑{r['grounding_low_support']}")
        lines.append(
            f"| {r['id']} {r['question'][:16]} | {r['faithfulness']} | {r['coverage']} "
            f"| {r.get('must_cover_coverage', '-')} | {r['citation_quality']} | {r['citation_validity']} "
            f"| {r['source_utilization']} | {r['input_tokens']}/{r['output_tokens']} | {r['elapsed_s']} "
            f"| {' / '.join(flags) or '正常'} |"
        )
    lines += [
        "",
        f"**均值**：忠实度 {means['faithfulness']} ｜ 覆盖度 {means['coverage']} ｜ 必覆盖要点覆盖率 {means.get('must_cover_coverage')}"
        f" ｜ 引用质量 {means['citation_quality']} ｜ 引用有效性 {means['citation_validity']} ｜ 来源利用率 {means['source_utilization']}"
        f" ｜ 单题成本估算 ${means['cost_usd_est']} ｜ 单题耗时 {means['elapsed_s']}s",
        "",
        f"**分位数**：耗时 p50 {pct.get('elapsed_s_p50')}s / p95 {pct.get('elapsed_s_p95')}s"
        f" ｜ 成本 p50 ${pct.get('cost_usd_est_p50')} / p95 ${pct.get('cost_usd_est_p95')}"
        f" ｜ 输入 tokens p50 {pct.get('input_tokens_p50')} / p95 {pct.get('input_tokens_p95')}",
        "",
        "> 注：judge 与研究的模型关系见上方；小模型当 judge 有系统性偏差，分数用于 A/B 相对对比；",
        "> 必覆盖要点覆盖率来自评测集 must_cover 清单（judge 逐条判定 + 代码做措辞对齐）；",
        "> 正式对外数字建议用独立强 judge 复评并跑全量 20 题；单变量实验请每次只改一个 --set。",
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
    cfg_a, cfg_b = data[tag_a]["config"], data[tag_b]["config"]
    for key in sorted(set(cfg_a) | set(cfg_b)):
        va, vb = cfg_a.get(key), cfg_b.get(key)
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
             "must_cover_coverage": "必覆盖要点覆盖率(0-1)",
             "citation_validity": "引用有效性(0-1)", "source_utilization": "来源利用率(0-1)",
             "cost_usd_est": "单题成本估算($)", "elapsed_s": "单题耗时(s)"}
    for key in SCORE_KEYS + HARD_KEYS + ["cost_usd_est", "elapsed_s"]:
        va, vb = data[tag_a]["means"].get(key), data[tag_b]["means"].get(key)
        delta = round(vb - va, 3) if isinstance(va, (int, float)) and isinstance(vb, (int, float)) else "-"
        lines.append(f"| {label.get(key, key)} | {va} | {vb} | {delta} |")

    pct_a, pct_b = data[tag_a].get("percentiles", {}), data[tag_b].get("percentiles", {})
    if pct_a or pct_b:
        lines += [
            "",
            "## 分位数对比",
            "",
            "| 指标 | " + tag_a + " | " + tag_b + " | Δ |",
            "|---|---|---|---|",
        ]
        pct_label = {
            "elapsed_s_p50": "耗时 p50(s)", "elapsed_s_p95": "耗时 p95(s)",
            "cost_usd_est_p50": "成本 p50($)", "cost_usd_est_p95": "成本 p95($)",
            "input_tokens_p50": "输入 tokens p50", "input_tokens_p95": "输入 tokens p95",
            "output_tokens_p50": "输出 tokens p50", "output_tokens_p95": "输出 tokens p95",
        }
        for key in pct_label:
            va, vb = pct_a.get(key), pct_b.get(key)
            delta = round(vb - va, 3) if isinstance(va, (int, float)) and isinstance(vb, (int, float)) else "-"
            lines.append(f"| {pct_label[key]} | {va} | {vb} | {delta} |")

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
        f"> judge 模型：`{cfg_a.get('judge_model') or cfg_a['llm_model']}`（两批次需一致）；n={len(rows_a)}。",
        "> 改进建议：跑全量 20 题、用更强模型复评后再写进简历。",
        "",
    ]
    out = RESULTS_DIR / "comparison.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"== 对比已生成：{out} ==", flush=True)


def recompute(tag: str) -> None:
    """用当前（修复后）的确定性校验规则重算既有批次的引用指标，无需重跑研究。

    适用场景：citation_checks 的正则/口径修好后，历史批次的报告文本本身没变，
    只需重算派生指标即可保持数据一致。
    """
    out_dir = RESULTS_DIR / tag
    summary_path = out_dir / "summary.json"
    if not summary_path.exists():
        raise SystemExit(f"缺少 {summary_path}，先跑该批次")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    changed = 0
    for row in summary["rows"]:
        if row.get("error"):
            continue
        report_path = out_dir / f"{row['id']}.md"
        if not report_path.exists():
            continue
        report = report_path.read_text(encoding="utf-8")
        n_refs = count_refs(report)
        fresh = citation_checks(report, n_refs)
        fresh["n_refs"] = n_refs
        for key, value in fresh.items():
            if row.get(key) != value:
                changed += 1
            row[key] = value
        (out_dir / f"{row['id']}_score.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    summary["means"] = {
        k: mean(summary["rows"], k)
        for k in SCORE_KEYS + HARD_KEYS + GROUNDING_KEYS + ["cost_usd_est", "elapsed_s"]
    }
    summary["percentiles"] = percentile_snapshot(summary["rows"])
    summary["recomputed_at"] = datetime.now().isoformat(timespec="seconds")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_summary_md(summary, out_dir)
    print(
        f"== {tag} 重算完成：{changed} 项指标修正 | 引用有效性均值 = {summary['means']['citation_validity']} "
        f"| 来源利用率 = {summary['means']['source_utilization']}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="深度研究助手评测")
    parser.add_argument("--tag", help="批次名（baseline / tuned ...）")
    parser.add_argument("--limit", type=int, default=8, help="评测题数（默认 8；core 80 / adversarial 30）")
    parser.add_argument("--offset", type=int, default=0, help="从第几题开始")
    parser.add_argument(
        "--suite",
        choices=SUITES,
        default="all",
        help="评测子集：all/smoke/core/regression/adversarial/holdout（默认 all）",
    )
    parser.add_argument("--max-sub", type=int,
                        help=f"覆盖子问题上限（1~{config.MAX_SUBQUESTIONS_LIMIT}；两批次需一致）")
    parser.add_argument("--rounds", type=int,
                        help=f"覆盖每个子问题最大检索轮数（1~{config.MAX_SEARCH_ROUNDS_LIMIT}）")
    parser.add_argument("--set", action="append", metavar="KEY=VALUE",
                        help="单变量实验覆盖配置（可重复；一次实验建议只改一项）")
    parser.add_argument("--compare", nargs=2, metavar=("TAG_A", "TAG_B"), help="生成两个批次的对比报告")
    parser.add_argument("--recompute", metavar="TAG", help="按修复后的规则重算既有批次的确定性指标")
    parser.add_argument("--memory", choices=["on", "off"], help="覆盖 MEMORY_ENABLED（A/B 用）")
    parser.add_argument("--reset-memory", action="store_true",
                        help="开跑前清空研究档案（保证两批次起点一致；跑 with-memory 批次前必做）")
    args = parser.parse_args()

    try:
        args.overrides = apply_overrides(args.set)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        max_subquestions, max_search_rounds = config.validate_scale(args.max_sub, args.rounds)
    except ValueError as exc:
        parser.error(str(exc))
    if args.memory:
        config.MEMORY_ENABLED = args.memory == "on"

    if args.recompute:
        recompute(args.recompute)
    elif args.compare:
        compare(args.compare[0], args.compare[1])
    elif args.tag:
        if args.reset_memory:
            deleted = memory.reset()
            print(f"== 已清空研究档案：{deleted} 条 ==", flush=True)
        evaluate(args, max_subquestions, max_search_rounds)
    else:
        parser.error("需要 --tag、--compare 或 --recompute")


if __name__ == "__main__":
    main()
