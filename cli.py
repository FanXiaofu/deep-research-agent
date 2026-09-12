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
import observability
from graph.build import build_research_app, make_checkpointer
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
        if node == "review_brief":
            continue  # 审核节点静默，暂停提示由 interrupt 负责
        if node == "planner" and item.get("subquestions"):
            print(f"── [planner] 拆解出 {len(item['subquestions'])} 个子问题 ({elapsed:.0f}s)")
            for i, sub in enumerate(item["subquestions"], 1):
                print(f"   {i}. {sub}")
        elif node == "researcher" and item.get("notes"):
            sub = item["notes"][0].get("subquestion", "")
            print(f"── [researcher] 完成「{sub[:36]}」({elapsed:.0f}s)")
        else:
            print(f"── [{node}] 完成 ({elapsed:.0f}s)")


def _prompt_review(brief: str) -> str | None:
    print("\n⏸  human-in-the-loop：请审核研究任务书（planner 将基于它拆解子问题）")
    print("─" * 64)
    print(brief)
    print("─" * 64)
    answer = input("直接回车 = 确认继续；输入文字 = 替换为新任务书：").strip()
    return answer or None


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
    parser.add_argument("--max-sub", type=int, help="子问题数量上限")
    parser.add_argument("--rounds", type=int, help="每个子问题最大检索轮数")
    parser.add_argument("--no-review", action="store_true", help="跳过任务书人工审核")
    parser.add_argument("--resume", nargs="?", const="__last__", help="续跑中断的研究（缺省用上一次 thread）")
    args = parser.parse_args()

    if args.max_sub:
        config.MAX_SUBQUESTIONS = args.max_sub
    if args.rounds:
        config.MAX_SEARCH_ROUNDS = args.rounds
    if not args.resume and not args.topic:
        parser.error("请提供研究主题，或使用 --resume 续跑")

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

    observability.init_session(tid)
    app = build_research_app(make_checkpointer())
    thread_cfg = {"configurable": {"thread_id": tid}}

    print(f"▶ 研究主题：{args.topic or '(续跑上次任务)'}")
    print(f"▶ 模型：{config.LLM_MODEL} @ {config.LLM_BASE_URL}")
    print(f"▶ 搜索：{config.SEARCH_PROVIDER} | researcher 并行 | thread: {tid}")
    print(f"▶ Langfuse：{'已启用 → ' + config.LANGFUSE_HOST if observability.is_enabled() else '未启用（.env 配置 key 后开启）'}\n")

    if args.resume:
        state, intr = _pending_interrupt(app, thread_cfg)
        if not state.next:
            raise SystemExit(f"thread {tid} 已完成，无需续跑")
        if intr is not None:
            answer = _prompt_review((intr.value or {}).get("brief", ""))
            payload = _resume_command(answer)
        else:
            payload = None
        print(f"↩ 从断点续跑，待执行节点：{list(state.next)}\n")
    else:
        payload = {"topic": args.topic, "notes": [], "require_review": not args.no_review}

    started = time.time()
    try:
        while True:
            interrupted = False
            for update in app.stream(payload, thread_cfg, stream_mode="updates"):
                if "__interrupt__" in update:
                    intr = update["__interrupt__"][0]
                    answer = _prompt_review((intr.value or {}).get("brief", ""))
                    payload = _resume_command(answer)
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
    print("\n──── 报告预览 ────\n" + report[:800] + ("\n..." if len(report) > 800 else ""))
    observability.flush()


if __name__ == "__main__":
    main()
