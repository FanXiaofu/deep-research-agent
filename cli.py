"""深度研究助手 CLI（W2：并行 fan-out + human-in-the-loop + checkpoint 断点恢复）。

用法：
    python cli.py "研究问题" [--max-sub 3] [--rounds 2] [--no-review]
    python cli.py --resume [thread_id]    # 续跑中断的研究，缺省用上一次的 thread
"""
import argparse
import re
import time
from datetime import datetime

import config
import memory
import observability
import reliability
from budget import BUDGET
from graph.build import build_research_app, make_checkpointer, thread_config
from langgraph.types import Command
from search import get_search_provider

LAST_THREAD_FILE = config.BASE_DIR / ".last_thread"


def slugify(topic: str) -> str:
    slug = re.sub(r'[\\/:*?"<>|\s]+', "-", topic)[:30].strip("-")
    return slug or "report"


def _print_progress(node: str, out, started: float) -> None:
    elapsed = time.time() - started
    outs = out if isinstance(out, list) else [out]  # 并行同名节点可能合并成列表
    for item in outs:
        if node in ("review_brief", "review_plan"):
            continue  # 审核节点静默，暂停提示由 interrupt 负责
        if node == "planner" and item.get("memory_reused"):
            for hit in item["memory_reused"]:
                print(f"── [memory] 命中历史研究：「{hit['subquestion'][:30]}」"
                      f"（相似度 {hit['similarity']}，{hit['age_days']} 天前）")
        if node == "planner" and item.get("replan_count"):
            new_subs = item.get("subquestions", [])
            print(f"── [planner] 第 {item['replan_count']} 轮重规划，新增 {len(new_subs)} 个子问题 ({elapsed:.0f}s)")
            for i, sub in enumerate(new_subs, 1):
                print(f"   {i}. {sub}")
        elif node == "planner" and item.get("subquestions"):
            print(f"── [planner] 拆解出 {len(item['subquestions'])} 个子问题 ({elapsed:.0f}s)")
            for i, sub in enumerate(item["subquestions"], 1):
                print(f"   {i}. {sub}")
        elif node == "compactor" and item.get("compaction", {}).get("applied"):
            c = item["compaction"]
            print(f"── [compactor] 笔记压缩 {c['before_chars']}→{c['after_chars']} 字 ({elapsed:.0f}s)")
        elif node == "archivist" and item.get("memory_written"):
            print(f"── [archivist] 落档 {item['memory_written']} 条笔记到研究档案 ({elapsed:.0f}s)")
        elif node == "researcher" and item.get("notes"):
            sub = item["notes"][0].get("subquestion", "")
            print(f"── [researcher] 完成「{sub[:36]}」({elapsed:.0f}s)")
        else:
            print(f"── [{node}] 完成 ({elapsed:.0f}s)")


def _prompt_review(value: dict) -> str | None:
    """渲染中断内容并读回用户输入（空串/None = 确认原内容）。

    brief 是单行文本；plan 的子问题允许逐行输入，空行结束（回车即确认原计划）。
    """
    kind = value.get("kind", "brief")
    print(f"\n⏸  human-in-the-loop：请审核{value.get('title') or kind}")
    print("─" * 64)
    print(value.get("content", ""))
    print("─" * 64)
    if kind == "plan":
        print("直接回车 = 确认按上述子问题检索；或逐行输入新子问题（空行结束）：")
        lines: list[str] = []
        while True:
            line = input("> " if not lines else "  ").strip()
            if not line:
                break
            lines.append(line)
        return "\n".join(lines) or None
    return input("直接回车 = 确认继续；输入文字 = 替换为新的任务书： ").strip() or None


def _resume_command(answer: str | None) -> Command:
    """确认时回车（None）也要传字符串：langgraph 1.2.11 对 Command(resume=None) 有
    UnboundLocalError bug（resume_is_map 只在 resume 非空时赋值）；review 节点把
    空字符串视作"保留原任务书"，语义一致。"""
    return Command(resume=answer or "")


def _pending_interrupt(app, thread_cfg):
    state = app.get_state(thread_cfg)
    for task in state.tasks or ():
        for intr in getattr(task, "interrupts", None) or ():
            return state, intr
    return state, None


def main() -> None:
    parser = argparse.ArgumentParser(description="多 Agent 深度研究助手（W2）")
    parser.add_argument("topic", nargs="?", help="研究主题（--resume 时可省略）")
    parser.add_argument("--max-sub", type=int,
                        help=f"子问题数量上限（1~{config.MAX_SUBQUESTIONS_LIMIT}）")
    parser.add_argument("--rounds", type=int,
                        help=f"每个子问题最大检索轮数（1~{config.MAX_SEARCH_ROUNDS_LIMIT}）")
    parser.add_argument("--no-review", action="store_true", help="跳过全部人工审核（默认按 REVIEW_POINTS 配置）")
    parser.add_argument("--memory", choices=["on", "off"],
                        help="覆盖 MEMORY_ENABLED：开启后命中的子问题直接复用历史笔记，不再检索")
    parser.add_argument("--resume", nargs="?", const="__last__", help="续跑中断的研究（缺省用上一次 thread）")
    args = parser.parse_args()

    try:
        max_sub, rounds = config.validate_scale(args.max_sub, args.rounds)
    except ValueError as exc:
        parser.error(str(exc))
    if args.memory:
        config.MEMORY_ENABLED = args.memory == "on"
    if not args.resume and not args.topic:
        parser.error("请提供研究主题，或使用 --resume 续跑")

    try:
        config.validate_runtime_config()
    except RuntimeError as exc:
        raise SystemExit(f"[配置错误] {exc}")

    if args.resume:
        tid = args.resume if args.resume != "__last__" else (
            LAST_THREAD_FILE.read_text(encoding="utf-8").strip() if LAST_THREAD_FILE.exists() else ""
        )
        if not tid:
            raise SystemExit("没有可续跑的记录（.last_thread 不存在）")
    else:
        tid = f"r-{datetime.now():%Y%m%d-%H%M%S}"
        LAST_THREAD_FILE.write_text(tid, encoding="utf-8")

    try:
        get_search_provider()
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"[配置错误] {exc}")

    app = build_research_app(make_checkpointer())
    thread_cfg = thread_config(tid)

    print(f"▶ 研究主题：{args.topic or '(续跑上次任务)'}")
    print(f"▶ 模型：{config.LLM_MODEL} @ {config.LLM_BASE_URL}")
    print(f"▶ 搜索：{config.SEARCH_PROVIDER} | researcher 并行 | thread: {tid}")
    if not args.resume:
        print(f"▶ 规模：子问题上限 {max_sub} × 检索轮数 {rounds}"
              f"（--max-sub / --rounds 可覆盖，范围 1~{config.MAX_SUBQUESTIONS_LIMIT} / 1~{config.MAX_SEARCH_ROUNDS_LIMIT}）")
    print(
        f"▶ 预算：{config.MAX_LLM_CALLS} 次调用 / {config.MAX_TOTAL_TOKENS} tokens / "
        f"{config.MAX_WALL_CLOCK_S:.0f}s / ${config.MAX_BUDGET_USD}（超限即停止新增分支并收敛出报告）"
    )
    if config.MEMORY_ENABLED:
        mem = memory.stats()
        print(
            f"▶ 研究档案：已启用（{mem['entries']} 条记忆，其中 {mem['used_entries']} 条被复用过 "
            f"{mem['total_hits']} 次）｜ 阈值 {config.MEMORY_SIMILARITY_THRESHOLD} / 寿命 {config.MEMORY_MAX_AGE_DAYS:g} 天"
        )
    else:
        print("▶ 研究档案：未启用（--memory on 或 .env 设 MEMORY_ENABLED=1 可复用历史研究）")
    print(f"▶ Langfuse：{'已启用 → ' + config.LANGFUSE_HOST if observability.is_enabled() else '未启用（.env 配置 key 后开启）'}\n")

    if args.resume:
        state, intr = _pending_interrupt(app, thread_cfg)
        if not state.next:
            raise SystemExit(f"thread {tid} 已完成，无需续跑")
        if intr is not None:
            payload = _resume_command(_prompt_review(intr.value or {}))
        else:
            payload = None
        print(f"↩ 从断点续跑，待执行节点：{list(state.next)}\n")
    else:
        payload = {
            "topic": args.topic,
            "notes": [],
            "review_points": [] if args.no_review else list(config.REVIEW_POINTS),
            "max_subquestions": max_sub,
            "max_search_rounds": rounds,
        }

    BUDGET.reset()          # 预算与熔断按"一次运行"计：续跑时重新起算
    reliability.reset_all()
    started = time.time()
    try:
        with observability.trace_context(
            tid,
            trace_name="deep-research",
            metadata={"topic": args.topic or "", "resumed": bool(args.resume), "model": config.LLM_MODEL},
        ):
            while True:
                interrupted = False
                for update in app.stream(payload, thread_cfg, stream_mode="updates"):
                    if "__interrupt__" in update:
                        intr = update["__interrupt__"][0]
                        payload = _resume_command(_prompt_review(intr.value or {}))
                        interrupted = True
                        break
                    for node, out in update.items():
                        _print_progress(node, out, started)
                if not interrupted:
                    break
    except KeyboardInterrupt:
        print(f"\n⏹ 已手动中断，断点已保存。用 --resume {tid} 续跑")
        observability.flush()
        return

    final = app.get_state(thread_cfg).values or {}
    report = final.get("report", "")
    if not report:
        raise SystemExit("[异常] 未生成报告")

    config.REPORTS_DIR.mkdir(exist_ok=True)
    out_path = config.REPORTS_DIR / (
        f"{datetime.now():%Y%m%d_%H%M}_{slugify(final.get('topic', args.topic or 'report'))}.md"
    )
    out_path.write_text(report, encoding="utf-8")

    print(f"\n✅ 报告已保存：{out_path}")
    print(f"   子问题 {len(final.get('subquestions', []))} 个 | 笔记 {len(final.get('notes', []))} 条 | 总耗时 {time.time() - started:.0f}s")
    usage = BUDGET.usage()
    print(
        f"   用量：{usage['calls']} 次调用 | {usage['input_tokens']}+{usage['output_tokens']} tokens "
        f"| ${usage['cost_usd_est']} | 剩余额度 {BUDGET.remaining()}"
    )
    if final.get("budget_exceeded"):
        print(f"   ⚠ 本轮触发预算上限（{final['budget_exceeded']}），已提前收敛，结论覆盖可能不完整")
    if final.get("compaction", {}).get("applied"):
        c = final["compaction"]
        print(f"   🗜 笔记压缩生效：{c['before_chars']}→{c['after_chars']} 字（{c['ratio']}×，{c['n_notes']} 条）")
    reused = final.get("memory_reused") or []
    if reused:
        print(f"   🧠 复用历史研究 {len(reused)} 条子问题（未重新检索）：")
        for hit in reused[:5]:
            print(f"      · {hit['subquestion'][:40]}（相似度 {hit['similarity']}，{hit['age_days']} 天前）")
    if final.get("memory_written"):
        print(f"   📝 落档 {final['memory_written']} 条笔记到研究档案（下次同题研究可复用）")
    events = reliability.degradations()
    if events:
        print(f"   ⚠ 依赖降级 {len(events)} 次：")
        for ev in events[:6]:
            print(f"      · {ev['component']} — {ev['reason']}")
    print("\n──── 报告预览 ────\n" + report[:800] + ("\n..." if len(report) > 800 else ""))
    observability.flush()


if __name__ == "__main__":
    main()
