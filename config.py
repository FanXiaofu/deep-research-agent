"""全局配置：从 .env 读取，命名风格与 job-radar 保持一致。"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _secret(name: str, default: str = "") -> str:
    """优先读 <NAME>_FILE（Docker/K8s secret），否则读环境变量。"""
    path = os.getenv(f"{name}_FILE", "").strip()
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"无法读取 {name}_FILE: {path}") from exc
    return os.getenv(name, default)


# ==== LLM（OpenAI 兼容接口，默认 DeepSeek）====
LLM_API_KEY = _secret("LLM_API_KEY")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
# 评测 judge 可单独指定更强模型；留空表示复用 LLM_MODEL
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
# 单次调用上限与重试次数；实际 timeout 还会被"剩余墙钟预算 / 尝试次数"进一步压低。
LLM_CALL_TIMEOUT_S = float(os.getenv("LLM_CALL_TIMEOUT_S", "300"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))

# ==== 搜索（tavily / duckduckgo）====
SEARCH_PROVIDER = os.getenv("SEARCH_PROVIDER", "tavily").lower()
SEARCH_API_KEY = _secret("SEARCH_API_KEY")

# ==== Langfuse 可观测（可选，两个 key 都配置才启用）====
LANGFUSE_PUBLIC_KEY = _secret("LANGFUSE_PUBLIC_KEY")
LANGFUSE_SECRET_KEY = _secret("LANGFUSE_SECRET_KEY")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "http://localhost:3000")

# ==== 存储（默认 SQLite；配置 DATABASE_URL 后 checkpoint/memory 走 Postgres）====
DATABASE_URL = _secret("DATABASE_URL")
# 多实例共享限流与指标；留空使用进程内实现
REDIS_URL = _secret("REDIS_URL")
# 账号/API token/审计库（DATABASE_URL 存在时复用 Postgres）
AUTH_DB = os.getenv("AUTH_DB") or str(BASE_DIR / "auth.db")
AUTH_DEFAULT_DAILY_QUOTA = int(os.getenv("AUTH_DEFAULT_DAILY_QUOTA", "20"))
# 备份加密 key（Fernet，base64）；留空时 backup --encrypt 会拒绝执行
BACKUP_ENCRYPTION_KEY = _secret("BACKUP_ENCRYPTION_KEY")

# ==== Checkpoint（断点恢复）====
CHECKPOINT_DB = os.getenv("CHECKPOINT_DB", str(BASE_DIR / "checkpoints.db"))
# 0 = 不自动清理；>0 时由 maintenance.py prune-checkpoints 按最后 checkpoint 时间清理旧 thread
CHECKPOINT_RETENTION_DAYS = float(os.getenv("CHECKPOINT_RETENTION_DAYS", "7"))

# ==== 研究规模控制（同时是 token 成本的闸门）====
MAX_SUBQUESTIONS = int(os.getenv("MAX_SUBQUESTIONS", "4"))
MAX_SEARCH_ROUNDS = int(os.getenv("MAX_SEARCH_ROUNDS", "2"))
MAX_RESULTS_PER_SEARCH = int(os.getenv("MAX_RESULTS_PER_SEARCH", "5"))
MAX_PAGE_CHARS = int(os.getenv("MAX_PAGE_CHARS", "6000"))
FETCH_TIMEOUT = float(os.getenv("FETCH_TIMEOUT", "15"))

# 自定义规模的合法范围：Web/CLI 允许按次覆盖，但越界即报错而不是静默降级
# （rounds<=0 会让 researcher 循环一次都不跑，直接产出空笔记 + 空报告）
MAX_SUBQUESTIONS_LIMIT = int(os.getenv("MAX_SUBQUESTIONS_LIMIT", "8"))
MAX_SEARCH_ROUNDS_LIMIT = int(os.getenv("MAX_SEARCH_ROUNDS_LIMIT", "5"))


def validate_scale(max_sub: int | None = None, rounds: int | None = None) -> tuple[int, int]:
    """校验按次覆盖的规模参数，返回生效的 (子问题上限, 检索轮数)；缺省回落 .env 配置。

    这是系统边界（用户输入）上的唯一一道校验，越界/非正数抛 ValueError；
    节点侧因此不必重复防御 —— 也顺手修掉了 `if max_sub:` 把 0 当 falsy 静默忽略的问题。
    """
    sub = MAX_SUBQUESTIONS if max_sub is None else int(max_sub)
    rnd = MAX_SEARCH_ROUNDS if rounds is None else int(rounds)
    if not 1 <= sub <= MAX_SUBQUESTIONS_LIMIT:
        raise ValueError(f"子问题上限需在 1~{MAX_SUBQUESTIONS_LIMIT} 之间（收到 {sub}）")
    if not 1 <= rnd <= MAX_SEARCH_ROUNDS_LIMIT:
        raise ValueError(f"检索轮数需在 1~{MAX_SEARCH_ROUNDS_LIMIT} 之间（收到 {rnd}）")
    return sub, rnd


# ==== 全局预算（长流程硬闸门：任一维度超限即停止新增分支，收敛出报告）====
# MAX_SUBQUESTIONS 只约束单次规划，这里约束整轮研究的总消耗（含重规划新增的分支）。
MAX_LLM_CALLS = int(os.getenv("MAX_LLM_CALLS", "40"))
MAX_TOTAL_TOKENS = int(os.getenv("MAX_TOTAL_TOKENS", "120000"))
MAX_WALL_CLOCK_S = float(os.getenv("MAX_WALL_CLOCK_S", "900"))
MAX_BUDGET_USD = float(os.getenv("MAX_BUDGET_USD", "0.5"))

# ==== 重规划（verifier 报告缺口/冲突后回到 planner 追加子问题）====
MAX_REPLAN_ROUNDS = int(os.getenv("MAX_REPLAN_ROUNDS", "1"))
MAX_FOLLOWUP_SUBQUESTIONS = int(os.getenv("MAX_FOLLOWUP_SUBQUESTIONS", "2"))

# ==== 人工审核点位（human-in-the-loop）====
# 逗号分隔，取值 brief / plan（none 或留空 = 全自动不中断）：
#   brief = coordinator 之后审核研究任务书；
#   plan  = 首次 planner 拆解之后、fan-out 之前审核子问题——这里是"花钱前"的最后一道关，
#           重规划（verifier 回边）不再中断，避免一次研究被反复打断。
REVIEW_POINTS = [p.strip() for p in os.getenv("REVIEW_POINTS", "brief").split(",") if p.strip()]
# plan 审核的最小触发规模：子问题数达到该值才暂停（默认 0 = 总是暂停），避免小任务被无谓打断
PLAN_REVIEW_MIN_SUBQUESTIONS = int(os.getenv("PLAN_REVIEW_MIN_SUBQUESTIONS", "0"))

# ==== 上下文压缩（笔记正文超阈值时压缩后再进消费节点，压缩不改来源列表）====
COMPACTION_ENABLED = os.getenv("COMPACTION_ENABLED", "1") == "1"
COMPACT_TRIGGER_CHARS = int(os.getenv("COMPACT_TRIGGER_CHARS", "12000"))
COMPACT_TARGET_CHARS = int(os.getenv("COMPACT_TARGET_CHARS", "4000"))

# ==== 外部依赖容错（搜索/抓取的重试与熔断）====
SEARCH_RETRIES = int(os.getenv("SEARCH_RETRIES", "2"))
BREAKER_FAILURE_THRESHOLD = int(os.getenv("BREAKER_FAILURE_THRESHOLD", "3"))
BREAKER_COOLDOWN_S = float(os.getenv("BREAKER_COOLDOWN_S", "60"))
GRAPH_RECURSION_LIMIT = int(os.getenv("GRAPH_RECURSION_LIMIT", "50"))

# ==== 研究档案（长期记忆）====
# 默认关闭：开启后同一（或相似）子问题会直接复用历史笔记、不再检索，
# 这会改变评测的对照条件 —— 做 A/B 时必须显式打开并先重置记忆库（run_eval.py --reset-memory）。
MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "0") == "1"
MEMORY_DB = os.getenv("MEMORY_DB", str(BASE_DIR / "memory.db"))
MEMORY_SIMILARITY_THRESHOLD = float(os.getenv("MEMORY_SIMILARITY_THRESHOLD", "0.62"))
MEMORY_MAX_AGE_DAYS = float(os.getenv("MEMORY_MAX_AGE_DAYS", "30"))
MEMORY_MAX_HITS = int(os.getenv("MEMORY_MAX_HITS", "4"))

# ==== W3 A/B 调优开关（eval 对比用；改 .env 即可切换实验组）====
RESEARCH_PAGE_CHARS = int(os.getenv("RESEARCH_PAGE_CHARS", "1200"))  # 网页正文进入提示词的上限
NOTE_MIN_CHARS = int(os.getenv("NOTE_MIN_CHARS", "200"))             # 研究笔记字数下限
NOTE_MAX_CHARS = int(os.getenv("NOTE_MAX_CHARS", "350"))             # 研究笔记字数上限
WRITER_STRICT_GROUNDING = os.getenv("WRITER_STRICT_GROUNDING", "0") == "1"  # writer 严格 grounding 规则

# ==== 成本估算（USD / 1M tokens；ollama 本地模型时成本为 0，价格仅用于估算展示）====
LLM_PRICE_INPUT_PER_M = float(os.getenv("LLM_PRICE_INPUT_PER_M", "0.27"))
LLM_PRICE_OUTPUT_PER_M = float(os.getenv("LLM_PRICE_OUTPUT_PER_M", "1.10"))

# ==== 模型行为微调 ====
# qwen3 系列的思考开关软指令（附加到每条提示词末尾）：/no_think 可将延迟降 3 倍以上；
# DeepSeek 等其他模型留空即可。A/B 两批次需保持一致。
LLM_PROMPT_SUFFIX = os.getenv("LLM_PROMPT_SUFFIX", "")
# 走 Ollama 原生 /api/chat（自动 think:false）。qwen3 的思考 token 不计入 usage 却主导延迟，
# OpenAI 兼容层关不掉，必须走原生接口；DeepSeek 等标准 OpenAI 兼容服务保持 0。
LLM_NATIVE_OLLAMA = os.getenv("LLM_NATIVE_OLLAMA", "0") == "1"
# Ollama 原生路径的上下文窗口：提示词超过该窗口会被服务端静默截断（曾导致空回复），
# 客户端在估算用量接近上限时显式告警。换模型时按模型实际窗口调整。
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))

# ==== 日志（Web 默认 JSON 行，便于容器采集）====
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_JSON = os.getenv("LOG_JSON", "1") == "1"

# ==== Web 安全（公网部署时 APP_AUTH_TOKEN 必填）====
# 留空只允许本机回环访问；对外部署必须设置一个足够长的随机 token（Bearer）。
APP_AUTH_TOKEN = _secret("APP_AUTH_TOKEN")
# 多个客户端 token，逗号分隔；与 APP_AUTH_TOKEN 合并生效
APP_AUTH_TOKENS = _secret("APP_AUTH_TOKENS")
APP_RATE_LIMIT_PER_MINUTE = int(os.getenv("APP_RATE_LIMIT_PER_MINUTE", "30"))
APP_MAX_TOPIC_CHARS = int(os.getenv("APP_MAX_TOPIC_CHARS", "500"))
APP_MAX_REVIEW_CHARS = int(os.getenv("APP_MAX_REVIEW_CHARS", "5000"))
APP_MAX_REQUEST_BYTES = int(os.getenv("APP_MAX_REQUEST_BYTES", "65536"))
# 抓取侧：只允许 http/https、80/443，拒绝私网/链路本地/保留地址；响应超过该字节数即截断。
MAX_FETCH_BYTES = int(os.getenv("MAX_FETCH_BYTES", "2000000"))
FETCH_MAX_REDIRECTS = int(os.getenv("FETCH_MAX_REDIRECTS", "3"))

# ==== 启动自检 ====
# 只做"不联网就能判定"的校验：缺 key、空模型名、非正数预算等。目的是让配置错误在
# 启动时就暴露，而不是等到检索/撰写阶段退化成"未收集到资料"的空报告（那会白花 token，
# 用户也看不到明确原因）。本地 Ollama 允许空 key；远程/局域网服务请填非空 key（可填 EMPTY）。
_LOCAL_LLM_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal")


def _is_local_llm() -> bool:
    base = LLM_BASE_URL.lower()
    return LLM_NATIVE_OLLAMA or any(host in base for host in _LOCAL_LLM_HOSTS)


def runtime_config_errors() -> list[str]:
    """返回启动前即可判定的配置问题；空列表表示配置可用于发起研究。"""
    errors: list[str] = []

    if not LLM_BASE_URL.strip():
        errors.append("LLM_BASE_URL 不能为空")
    if not LLM_MODEL.strip():
        errors.append("LLM_MODEL 不能为空")
    if not LLM_API_KEY.strip() and not _is_local_llm():
        errors.append(
            "LLM_API_KEY 未配置：远程 LLM 服务需要 key；本地 Ollama 可留空（或填 EMPTY）"
        )

    if SEARCH_PROVIDER not in ("tavily", "duckduckgo"):
        errors.append(f"SEARCH_PROVIDER={SEARCH_PROVIDER!r} 不支持，可选：tavily / duckduckgo")
    if SEARCH_PROVIDER == "tavily" and not SEARCH_API_KEY.strip():
        errors.append("SEARCH_API_KEY 未配置：SEARCH_PROVIDER=tavily 时需要 Tavily key")

    positive = {
        "MAX_SUBQUESTIONS": MAX_SUBQUESTIONS,
        "MAX_SEARCH_ROUNDS": MAX_SEARCH_ROUNDS,
        "MAX_RESULTS_PER_SEARCH": MAX_RESULTS_PER_SEARCH,
        "MAX_PAGE_CHARS": MAX_PAGE_CHARS,
        "FETCH_TIMEOUT": FETCH_TIMEOUT,
        "MAX_LLM_CALLS": MAX_LLM_CALLS,
        "MAX_TOTAL_TOKENS": MAX_TOTAL_TOKENS,
        "MAX_WALL_CLOCK_S": MAX_WALL_CLOCK_S,
        "MAX_BUDGET_USD": MAX_BUDGET_USD,
        "LLM_CALL_TIMEOUT_S": LLM_CALL_TIMEOUT_S,
        "GRAPH_RECURSION_LIMIT": GRAPH_RECURSION_LIMIT,
        "APP_RATE_LIMIT_PER_MINUTE": APP_RATE_LIMIT_PER_MINUTE,
        "APP_MAX_TOPIC_CHARS": APP_MAX_TOPIC_CHARS,
        "APP_MAX_REVIEW_CHARS": APP_MAX_REVIEW_CHARS,
        "APP_MAX_REQUEST_BYTES": APP_MAX_REQUEST_BYTES,
        "MAX_FETCH_BYTES": MAX_FETCH_BYTES,
    }
    for name, value in positive.items():
        if value <= 0:
            errors.append(f"{name} 必须大于 0（当前 {value}）")

    if MAX_SUBQUESTIONS > MAX_SUBQUESTIONS_LIMIT:
        errors.append(
            f"MAX_SUBQUESTIONS={MAX_SUBQUESTIONS} 超过上限 "
            f"MAX_SUBQUESTIONS_LIMIT={MAX_SUBQUESTIONS_LIMIT}"
        )
    if MAX_SEARCH_ROUNDS > MAX_SEARCH_ROUNDS_LIMIT:
        errors.append(
            f"MAX_SEARCH_ROUNDS={MAX_SEARCH_ROUNDS} 超过上限 "
            f"MAX_SEARCH_ROUNDS_LIMIT={MAX_SEARCH_ROUNDS_LIMIT}"
        )
    if not 0 <= MEMORY_SIMILARITY_THRESHOLD <= 1:
        errors.append(
            f"MEMORY_SIMILARITY_THRESHOLD 需在 0~1 之间（当前 {MEMORY_SIMILARITY_THRESHOLD}）"
        )
    if FETCH_MAX_REDIRECTS < 0:
        errors.append(f"FETCH_MAX_REDIRECTS 不能为负数（当前 {FETCH_MAX_REDIRECTS}）")
    if CHECKPOINT_RETENTION_DAYS < 0:
        errors.append(
            f"CHECKPOINT_RETENTION_DAYS 不能为负数（当前 {CHECKPOINT_RETENTION_DAYS}）"
        )
    if LLM_MAX_RETRIES < 0:
        errors.append(f"LLM_MAX_RETRIES 不能为负数（当前 {LLM_MAX_RETRIES}）")
    if LOG_LEVEL not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        errors.append(f"LOG_LEVEL={LOG_LEVEL!r} 不支持")
    if DATABASE_URL and not DATABASE_URL.startswith(("postgresql://", "postgres://")):
        errors.append("DATABASE_URL 必须以 postgresql:// 或 postgres:// 开头")
    if REDIS_URL and not REDIS_URL.startswith(("redis://", "rediss://")):
        errors.append("REDIS_URL 必须以 redis:// 或 rediss:// 开头")
    if AUTH_DEFAULT_DAILY_QUOTA < 0:
        errors.append(
            f"AUTH_DEFAULT_DAILY_QUOTA 不能为负数（当前 {AUTH_DEFAULT_DAILY_QUOTA}）"
        )
    return errors


def validate_runtime_config() -> None:
    """启动时调用；有问题直接抛 RuntimeError，让 CLI/服务在花钱前失败。"""
    errors = runtime_config_errors()
    if errors:
        raise RuntimeError("配置校验失败：\n- " + "\n- ".join(errors))


REPORTS_DIR = BASE_DIR / "reports"
