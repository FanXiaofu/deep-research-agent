# 评测集 v2（110 题）

`eval/golden_questions.jsonl` 现在按子集分层，而不是把所有题都塞进一次评测。

| 子集 | 题数 | 用途 |
|---|---:|---|
| `smoke` | 10 | 每次提交的快速回归 |
| `core` | 80 | 发布前核心质量评估（包含 smoke 与 holdout） |
| `regression` | 50 | 合并前/ nightly 回归（不含 holdout） |
| `adversarial` | 30 | prompt injection、隐私、引用造假、错误前提、SSRF/XSS 等 |
| `holdout` | 10 | 只看结果，不用于调参 |
| `all` | 110 | 全量 |

## 用法

```bash
# 快速 smoke：10 题
python eval/run_eval.py --suite smoke --limit 10 --tag smoke-20261029

# 核心集：80 题
python eval/run_eval.py --suite core --limit 80 --tag core-20261029

# 对抗集：30 题
python eval/run_eval.py --suite adversarial --limit 30 --tag adversarial-20261029

# holdout：10 题（只在最终评估时跑）
python eval/run_eval.py --suite holdout --limit 10 --tag holdout-20261029
```

`--offset` 在子集过滤后生效；`--limit` 也按子集计数。

## 核心集覆盖

除原有的 AI/技术题外，新增了：

- 后端/数据库/消息队列/缓存/容量规划
- 数据工程/可观测性/安全/基础设施即代码
- 成本估算、尾延迟、分布式锁、schema 迁移
- 信息不足/应拒答、来源冲突、争议分析、非技术沟通
- 时效性问题（要求标注日期和不确定性）

## 对抗集覆盖

- 系统提示词/secret 索取
- 忽略指令、网页内容注入、格式优先绕过
- 伪造引用/URL、越界编号
- 错误前提、绝对化断言、隐藏冲突
- 隐私抓取、内网/元数据地址、XSS/危险链接
- 自伤、违法、敏感推断等安全边界

## 解读规则

- 110 题仍然不是“生产真理”；它比 20 题更适合回归和分层分析。
- `core` 与 `holdout` 有重叠（holdout 是 core 的子集），报告数字要说明用的是哪个子集。
- `adversarial` 的 `must_cover` 是期望行为清单，不等于模型一定执行；建议再配独立安全 judge 或人工复核。
- 同一 judge 跑 110 题仍可能有系统性偏差；独立强 judge 和人工抽检仍是必要步骤。
