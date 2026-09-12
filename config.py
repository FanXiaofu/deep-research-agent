"""全局配置：从 .env 读取，命名风格与 job-radar 保持一致。"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# ==== LLM（OpenAI 兼容接口，默认 DeepSeek）====
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))

# ==== 搜索（tavily / duckduckgo）====
SEARCH_PROVIDER = os.getenv("SEARCH_PROVIDER", "tavily").lower()
SEARCH_API_KEY = os.getenv("SEARCH_API_KEY", "")

# ==== Langfuse 可观测（可选，两个 key 都配置才启用）====
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "http://localhost:3000")

# ==== Checkpoint（断点恢复）====
CHECKPOINT_DB = os.getenv("CHECKPOINT_DB", str(BASE_DIR / "checkpoints.db"))

# ==== 研究规模控制（同时是 token 成本的闸门）====
MAX_SUBQUESTIONS = int(os.getenv("MAX_SUBQUESTIONS", "4"))
MAX_SEARCH_ROUNDS = int(os.getenv("MAX_SEARCH_ROUNDS", "2"))
MAX_RESULTS_PER_SEARCH = int(os.getenv("MAX_RESULTS_PER_SEARCH", "5"))
MAX_PAGE_CHARS = int(os.getenv("MAX_PAGE_CHARS", "6000"))
FETCH_TIMEOUT = float(os.getenv("FETCH_TIMEOUT", "15"))

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

REPORTS_DIR = BASE_DIR / "reports"
