# P1.7 真实模型 smoke 评测

日期：2026-10-29

## 目的

验证真实模型链路、grounding 审计与报告出口，而不是继续用 mock 代表质量。

## 命令

```bash
python eval/run_eval.py \
  --tag p17_real_smoke6 \
  --limit 1 \
  --max-sub 2 \
  --rounds 1 \
  --memory off
```

- 研究模型：`qwen2.5:7b-instruct`（本机 Ollama）
- judge：同一 `qwen2.5:7b-instruct`（**不是独立强 judge**）
- 题目：`q01` LangGraph vs CrewAI
- 规模：2 个子问题、1 轮检索、memory 关闭

## 结果

| 指标 | 值 |
|---|---|
| faithfulness | 4 / 5 |
| coverage | 4 / 5 |
| must_cover_coverage | 0.667 |
| citation_quality | 5 / 5 |
| citation_validity | 1.0 |
| source_utilization | 0.75 |
| 引用数 / 来源数 | 37 / 8 |
| LLM 调用 | 6 |
| tokens 输入/输出 | 6474 / 1493 |
| 耗时 | 195.1s |
| 成本估算 | $0.0034（本机 Ollama 实际成本为 0；这是按 `.env` 价格估算） |
| grounding claims / low_support / numeric_mismatch | 7 / 1 / 0 |
| 降级事件 | 2（fetch 熔断） |

## 本次暴露并修复的问题

1. **引用标记被当成论断**：引用常写成“句子。[1][4]”，原按句号切分会把 `[1][4]` 切成一条假论断；改为按行/段落提取，并剔除引用编号后再做数字一致性比较。
2. **模型自造参考来源**：7B 模型未遵守“不要输出参考来源”，出现过 `## 参考来源` 和 `--- [n1] ...` 两种形式，导致重复来源、grounding 只审计前半段；现在统一剥离模型生成的来源清单，只保留代码追加的一份。
3. **无 H1 正文被截断**：`_strip_preamble` 原来从第一个 `#` 行截断，正文没有 H1 时会把 `## 参考来源` 当成正文起点；现在只从一级标题 `# ` 截断。

## 结论与限制

- 真实链路可跑通，引用编号校验为 1.0，grounding 发现 1 条低词面覆盖句。
- judge 与研究模型相同，`faithfulness=4` 只能作为相对信号，不能对外宣称绝对质量。
- 只跑了 1 题、2 子问题、1 轮，不是完整 benchmark。
- 交付报告仍出现 2 次 fetch 熔断；说明外部网页抓取稳定性仍需关注。
- 语义 grounding judge 未启用；本次只有词面/数字审计。
