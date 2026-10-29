# P1.7 / P1.8 真实模型评测记录

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

## 全量 20 题（同一 judge，降低单题规模）

```bash
python eval/run_eval.py \
  --tag p17_full20_samejudge \
  --limit 20 \
  --max-sub 1 \
  --rounds 1 \
  --memory off
```

- 20/20 完成，0 题报错；**不是默认 4 子问题 × 2 轮**，是为了控制本地耗时降到 1 × 1；
- 研究/judge 都是 `qwen2.5:7b-instruct`，**不是独立强 judge**；
- 均值：faithfulness 4.75 / coverage 4.05 / must_cover 0.5 / citation_quality 4.55 / citation_validity 1.0 / source_utilization 0.752；
- 单题耗时均值 79.7s，p50 74.0s，p95 132.5s；单题成本估算均值 $0.003；
- grounding：共检查 123 条带引用句，39 条低词面覆盖，19 条数字未在来源摘要中找到；语义 judge 未启用；
- 8 次依赖降级，主要是 fetch 熔断；
- 低支撑句分布在 10 道题；词面覆盖只是启发式，不能把这 39 条直接等同于 39 个事实错误。

## kill/restart `--resume` 演示

- thread：`r-20261029-175219-e0c2f2`
- 启动 CLI 后在 `review_brief` 的 human-in-the-loop 暂停；
- 在暂停处强制结束进程，再用 `cli.py --resume` 从 checkpoint 继续；
- 续跑完成并落盘：`reports/20261029_1753_checkpoint-resume-test2.md`；
- 续跑用量：4 次 LLM 调用、4982+863 tokens、54s；
- 首次演示还暴露并修复了：CLI 进度打印遇到 `None` 节点输出崩溃、Langfuse context manager 异常时二次 `yield` 掩盖原始异常。

## 独立 judge 单题抽样

用 Command Code Go 套餐的 `deepseek/deepseek-v4-pro` 通过 `cmdc` headless 评审 baseline q01（不是完整批次）：

| 指标 | 同模型 qwen judge | 独立 DeepSeek V4 Pro |
|---|---:|---:|
| faithfulness | 5 | 4 |
| coverage | 5 | 4 |
| citation_quality | 5 | 2 |

baseline q01 正文实际 **0 条引用**，同模型 judge 仍给了 citation_quality=5；独立 judge 指出没有正文引用标记、无法核对来源，给了 2。这是单题抽样，但清楚地说明同模型 judge 有系统性偏差。

接入方式：`JUDGE_PROVIDER=commandcode`、`JUDGE_MODEL=deepseek/deepseek-v4-pro`，并设置 `COMMAND_CODE_API_KEY`（本机已登录 CLI 时也可不设）。

## 独立 judge smoke 10

同一批 smoke 题（q01–q10，1×1 规模）分别用同模型 qwen 和独立 DeepSeek V4 Pro 评分：

| 指标 | 同模型 qwen | 独立 DeepSeek V4 Pro |
|---|---:|---:|
| faithfulness | 4.6 | **3.6** |
| coverage | 4.1 | **2.7** |
| citation_quality | 4.5 | **2.4** |
| must_cover_coverage | 0.733 | **0.5** |
| source_utilization | 0.756 | **0.589** |
| 单题耗时 | — | 205.9s（含 3 次 CLI judge） |

10/10 完成，0 题报错。独立 judge 明显更严格，尤其是指出报告论断与研究笔记无依据/矛盾；这证明同模型 judge 的分数偏高，不能作为对外质量结论。

注意：`cmdc` 是编码 Agent，不是专用 judge；它可能比理想 judge 更严格，这仍是“另一个 judge 的信号”，不是人工真值。

## API 研究模型单题验证

配置改为研究 `deepseek/deepseek-v4-pro`、judge `Qwen/Qwen3.8-Max`（均走 Command Code Provider API），跑 q01：

| 指标 | 值 |
|---|---:|
| faithfulness / coverage / citation_quality | 4 / 3 / 3 |
| must_cover_coverage | 1.0 |
| citation_validity | 1.0 |
| source_utilization | 0.538 |
| 引用数 / 来源数 | 38 / 13 |
| LLM 调用 | 9 |
| tokens 输入/输出 | 18277 / 17541 |
| 耗时 | 517.9s |
| 研究成本估算 | $0.0242（judge 另计） |

单题结果，不是完整批次。API 模型明显比本地 qwen 慢、token 更多；下一步应先确认成本和延迟是否可接受，再跑 smoke 10。

## 结论与限制

- 真实链路可跑通，引用编号校验为 1.0；全量 20 题同 judge 结果是可复现基线，但不是独立质量结论。
- judge 与研究模型相同，`faithfulness` 只能作为相对信号。
- 全量 20 题的 1×1 规模低于默认配置；不能直接代表默认规模和更长检索轮次的质量。
- 交付报告仍出现 fetch 熔断；外部网页抓取稳定性仍需关注。
- 语义 grounding judge、内容安全 judge、独立强 judge 均未启用。
