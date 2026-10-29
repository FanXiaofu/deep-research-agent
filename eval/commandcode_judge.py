"""Command Code headless judge adapter.

只用于评测：通过 `cmdc -p` 调 Command Code 套餐模型，prompt 走 stdin，
从 NDJSON 的 final result 里取 finalText 并解析 JSON。key 只从环境变量继承，
不写入文件、不写入日志。
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import config

DEFAULT_MODEL = "deepseek/deepseek-v4-pro"


def _cli() -> str:
    if config.COMMAND_CODE_CLI.strip():
        path = Path(config.COMMAND_CODE_CLI).expanduser()
        if path.exists():
            return str(path)
        raise RuntimeError(f"COMMAND_CODE_CLI 不存在：{path}")
    for name in ("cmdc", "command-code"):
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        fallback = Path(os.environ.get("APPDATA", "")) / "npm" / "cmdc.cmd"
        if fallback.exists():
            return str(fallback)
    raise RuntimeError("未找到 cmdc；请安装 command-code CLI 或设置 COMMAND_CODE_CLI")


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"judge 未返回 JSON：{text[:200]}")
    return json.loads(text[start:end + 1])


def judge_json(prompt: str, *, model: str | None = None, timeout: int = 240) -> dict:
    model = model or config.JUDGE_MODEL or DEFAULT_MODEL
    cli = _cli()
    cmd = [
        cli,
        "--model", model,
        "--effort", config.JUDGE_EFFORT,
        "--output-format", "json",
        "--skip-onboarding",
        "--max-turns", "2",
        "-p",
    ]
    if os.name == "nt" and cli.lower().endswith(".cmd"):
        cmd = ["cmd", "/c", *cmd]
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            cwd=tmp,
            env=os.environ.copy(),
            timeout=timeout,
        )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-800:]
        raise RuntimeError(f"cmdc judge failed (exit {proc.returncode}): {tail}")

    final_text = ""
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            final_text = str(event.get("finalText", ""))
            break
    if not final_text:
        raise RuntimeError("cmdc judge 没有返回 result/finalText")
    return _extract_json(final_text)
