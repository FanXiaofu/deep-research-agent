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
| 忠实度(1-5) | 4.75 | 4.875 | 0.125 |
| 覆盖度(1-5) | 4.75 | 4.75 | 0.0 |
| 引用质量(1-5) | 4.5 | 4.625 | 0.125 |
| 引用有效性(0-1) | 0.875 | 1.0 | 0.125 |
| 来源利用率(0-1) | 0.621 | 0.85 | 0.229 |
| 单题成本估算($) | 0.003 | 0.004 | 0.001 |
| 单题耗时(s) | 112.613 | 107.25 | -5.363 |

## 逐题分数

| 题目 | baseline 忠实/覆盖/引用 | tuned 忠实/覆盖/引用 |
|---|---|---|
| q01 LangGraph 和 CrewAI 有 | 5/5/5 | 5/4/4 |
| q02 什么是 Agentic RAG，与传统  | 5/5/5 | 5/5/5 |
| q03 Rust 和 Go 哪个更适合写高性能  | 5/5/4 | 5/5/4 |
| q04 MCP 协议是什么，它解决了什么问题 | 5/5/5 | 5/5/5 |
| q05 向量数据库 Milvus 和 Qdran | 5/5/5 | 5/5/5 |
| q06 2025 到 2026 年大模型参数高效 | 4/4/4 | 5/5/5 |
| q07 LLM 推理中的 KV Cache 是什 | 5/5/5 | 5/5/5 |
| q08 Tavily、Serper、Bing S | 4/4/3 | 4/4/4 |

> judge 模型：`qwen2.5:7b-instruct`（两批次相同）；n=8。
> 改进建议：跑全量 20 题、用更强模型复评后再写进简历。
