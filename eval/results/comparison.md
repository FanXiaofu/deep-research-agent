# A/B 对比：baseline → tuned

## 配置差异

| 配置项 | baseline | tuned |
|---|---|---|
| llm_model | qwen2.5:7b-instruct | qwen2.5:7b-instruct |
| llm_base_url | http://localhost:11434/v1 | http://localhost:11434/v1 |
| max_subquestions | 2 | 2 |
| max_search_rounds | 1 | 1 |
| max_results_per_search | 5 | 5 |
| research_page_chars | 1200 | 2000 ← |
| note_min_chars | 200 | 300 ← |
| note_max_chars | 350 | 450 ← |
| writer_strict_grounding | False | True ← |
| price_input_per_m | 0.27 | 0.27 |
| price_output_per_m | 1.1 | 1.1 |

## 指标均值对比

| 指标 | baseline | tuned | Δ |
|---|---|---|---|
| 忠实度(1-5) | 4.8 | 4.8 | 0.0 |
| 覆盖度(1-5) | 4.8 | 4.75 | -0.05 |
| 引用质量(1-5) | 4.85 | 4.75 | -0.1 |
| 引用有效性(0-1) | 0.82 | 1.0 | 0.18 |
| 来源利用率(0-1) | 0.622 | 0.748 | 0.126 |
| 单题成本估算($) | 0.004 | 0.005 | 0.001 |
| 单题耗时(s) | 67.31 | 70.455 | 3.145 |

## 逐题分数

| 题目 | baseline 忠实/覆盖/引用 | tuned 忠实/覆盖/引用 |
|---|---|---|
| q01 LangGraph 和 CrewAI 有 | 5/5/5 | 5/5/5 |
| q02 什么是 Agentic RAG，与传统  | 5/5/5 | 5/5/5 |
| q03 Rust 和 Go 哪个更适合写高性能  | 5/5/5 | 4/4/4 |
| q04 MCP 协议是什么，它解决了什么问题 | 5/5/5 | 5/5/5 |
| q05 向量数据库 Milvus 和 Qdran | 5/5/5 | 5/5/5 |
| q06 2025 到 2026 年大模型参数高效 | 4/5/5 | 5/5/5 |
| q07 LLM 推理中的 KV Cache 是什 | 5/5/5 | 5/5/5 |
| q08 Tavily、Serper、Bing S | 4/4/4 | 4/4/5 |
| q09 构建 AI 应用后端时 FastAPI  | 5/5/5 | 5/5/4 |
| q10 什么是 GraphRAG，相比向量 RA | 5/5/5 | 5/5/5 |
| q11 本地部署开源大模型的方案 Ollama、 | 5/5/4 | 5/5/5 |
| q12 PostgreSQL 的 pgvecto | 5/4/4 | 5/5/5 |
| q13 Agent 的 ReAct 模式是什么， | 4/4/5 | 5/5/5 |
| q14 Langfuse 和 LangSmith | 5/5/5 | 5/5/5 |
| q15 10B 以下的小模型现在能胜任哪些生产级 | 4/4/5 | 4/4/4 |
| q16 LLM 的结构化输出是什么，主流实现方式 | 5/5/5 | 5/4/5 |
| q17 Agent 会话记忆存 Redis 还是 | 5/5/5 | 5/5/4 |
| q18 AI 编程助手 Copilot、Clau | 5/5/5 | 4/4/4 |
| q19 RAG 中的重排序 rerank 是什么 | 5/5/5 | 5/5/5 |
| q20 用 Hugging Face Trans | 5/5/5 | 5/5/5 |

> judge 模型：`qwen2.5:7b-instruct`（两批次相同）；n=20。
> 改进建议：跑全量 20 题、用更强模型复评后再写进简历。
