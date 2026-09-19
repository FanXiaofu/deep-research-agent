# deep-research-agent

多 Agent 深度研究助手：输入一个研究问题，Agent 团队自动完成 **澄清 → 规划 → 并行检索 → 交叉验证 → 撰写报告**，输出带引用编号的 Markdown 研究报告。

对标 OpenAI Deep Research / 字节 DeerFlow 的开源复刻，差异化点：**自建评测体系 + token 成本控制 + 全链路可观测**。

## 架构

```
topic
  │
  ▼
coordinator ── 生成研究任务书（brief）
  │
  ▼
review_brief ─ human-in-the-loop：interrupt 暂停，人工审核/改写任务书（--no-review 跳过）
  │
  ▼
planner ────── 拆解为 3~5 个子问题（JSON 输出）
  │
  ├─ Send API 并行 fan-out ────────────────┐
  ▼                    ▼                   ▼
researcher₁         researcher₂        researcherₙ   ← 上下文完全隔离
（搜索→抓取→摘要→反思循环，各分支只回传一条精炼笔记，notes 经 reducer 归并）
  │
  ▼
verifier ───── 交叉比对，标记来源冲突
  │
  ▼
writer ────── 汇总为 Markdown 报告（引用编号由代码确定性生成，防编造）

全图挂 SqliteSaver checkpoint：每步落盘，崩溃/中断后 --resume 精确续跑
Langfuse（可选）：每个节点、每次 LLM 调用的 span 自动上报，token 用量可查
```

## 技术栈

Python 3.11 · LangGraph（状态机编排）· DeepSeek（OpenAI 兼容接口，可插拔）· Tavily · httpx + BeautifulSoup · Langfuse（W2）· FastAPI + SSE（W4）

## 快速开始

虚拟环境统一放在 `E:\AIProject\environment\deep-research-agent-venv`。

### Windows 一键启动（推荐）

- **`start.bat`** — 双击即可：自动检查并拉起本地 Ollama → 打开浏览器 → 启动 Web 服务。按 `Ctrl+C` 停止。
- **`research.bat "你的研究问题"`** — 命令行跑一次研究，报告存到 `reports/`。
  - `research.bat "问题" --max-sub 4 --rounds 2`　指定子问题数与检索轮数
  - `research.bat "问题" --no-review`　跳过人工审核，全自动
  - `research.bat --resume`　崩溃/中断后从断点续跑

### 手动方式

```bash
# Git Bash 激活
source /e/AIProject/environment/deep-research-agent-venv/Scripts/activate

# PowerShell 激活
E:\AIProject\environment\deep-research-agent-venv\Scripts\Activate.ps1

# 1. 已默认配置：本地 Ollama(qwen3:4b) + Tavily，开箱即跑（需先启动 ollama serve）
#    换 DeepSeek：编辑 .env 中注释的 DeepSeek 段并填 key
# 2. 运行（默认会先弹出任务书供人工审核）
python cli.py "LangGraph 和 CrewAI 有什么区别，各适合什么场景" --max-sub 3

# 常用变体
python cli.py "研究问题" --no-review          # 跳过人工审核，全自动
python cli.py --resume                       # 崩溃/中断后从断点续跑（自动读上次 thread）
python cli.py --resume r-20260912-033100     # 续跑指定 thread
```

报告输出到 `reports/` 目录。

## Web 界面（SSE 流式）

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# 打开 http://127.0.0.1:8000
```

- 实时时间线展示每个 Agent 的动作（协调员→规划师→并行研究员→核查员→撰写人），含耗时
- 勾选"研究前人工审核"时，任务书生成后会暂停，可在网页上确认或改写后继续（interrupt → resume）
- 报告渲染 + 统计卡片（子问题数/LLM 调用/token/成本估算/耗时），一键复制 Markdown

## Docker 部署

```bash
docker compose up -d
# Web 界面  → http://localhost:8000
# Langfuse  → http://localhost:3000（注册后把 key 填入 .env 重启生效）
```

本地 Ollama 用户需在 `docker-compose.yml` 的 app 服务中取消 `LLM_BASE_URL: http://host.docker.internal:11434/v1` 的注释（容器内访问宿主机 Ollama）。镜像构建约 436MB，容器内已实测跑通完整研究流程。

更多实现细节与踩坑记录见技术博客：[docs/blog.md](docs/blog.md)。

## Langfuse 全链路追踪

### 启动（Windows 一键）

双击 **`langfuse.bat`**：自动启动 6 个容器并在浏览器打开 `http://localhost:3000`。

- 登录账号：`demo@local.dev`，密码见 `.env` 的 `LANGFUSE_INIT_USER_PASSWORD`
- 其它命令：`langfuse.bat stop`（停止并释放内存）、`langfuse.bat status`、`langfuse.bat logs`

### 启动（手动）

```bash
docker compose up -d langfuse-web langfuse-worker   # 起 Langfuse（v3 架构）
docker compose stop langfuse-web langfuse-worker clickhouse minio redis postgres   # 停止
```

### 说明

- **无需手动注册**：首次启动时 `LANGFUSE_INIT_*` 环境变量会自动创建组织、项目、账号，密钥与 app 共用 `.env` 中的同一对，开箱即用。
- **追踪结构**：一轮研究 = 一条 trace（根 span 名为 `deep-research`，类型 `agent`），内部每个 LLM 调用是一个 `llm.chat` 子 span，共享同一 `session_id`。在 UI 的 Sessions 页可按 session 查看完整协作过程，Sessions/Traces 页可查每次调用的 token 用量与延迟。
- **资源占用**：6 个容器常驻约 2~4GB 内存，不用时 `langfuse.bat stop` 释放（数据保留在 docker 卷中）。
- **版本匹配**：Python SDK 为 v4（走 OpenTelemetry 摄取），需要 Langfuse server ≥ 3.60，因此 compose 使用 `langfuse/langfuse:3` 与 `langfuse-worker:3`（两个镜像都要，worker 负责异步落库）。

> 若想免部署，也可以注册 [Langfuse Cloud](https://cloud.langfuse.com)（有免费额度），把项目的 Public/Secret Key 填进 `.env` 的 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` 即可，无需 `ENCRYPTION_KEY`。

## 配置说明（.env）

| 变量 | 说明 |
|---|---|
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | 任意 OpenAI 兼容服务；本地 Ollama 示例见 `.env.example` |
| `SEARCH_PROVIDER` | `tavily`（推荐）或 `duckduckgo`（免费兜底） |
| `MAX_SUBQUESTIONS` | 子问题上限，直接控制成本 |
| `MAX_SEARCH_ROUNDS` | 每个子问题反思循环的最大轮数 |
| `LANGFUSE_PUBLIC_KEY/SECRET_KEY` | 都配置才启用追踪，留空自动降级关闭 |

## 里程碑

- [x] **W1 最小闭环**：LangGraph 线性流水线 + Tavily 检索 + CLI，产出带引用报告
- [x] **W2 并行与工程化**：researcher 并行 fan-out（Send API）、human-in-the-loop（interrupt）、SQLite checkpoint 断点恢复（--resume）、Langfuse 全链路追踪
  - 实测：researcher 阶段 321s → 180s（本地单模型有排队损耗）；强杀进程后 `--resume` 从断点精确恢复未完成的并行分支
- [x] **W3 评测体系**：20 题评测集 + LLM-as-judge（忠实度/覆盖度/引用质量）+ 引用确定性校验 + token 成本统计，A/B 调优对比（**全量 20 题**）
  - 实测（qwen2.5:7b 本地，judge 同模型，两批次同题同参数）：**引用有效性 0.82 → 1.00**（baseline 有 3/20 题报告完全无引用，tuned 零失败）、**来源利用率 0.622 → 0.748（+20%）**；LLM judge 主观分持平（4.8 vs 4.8，7B judge 分辨力所限），单题成本 +$0.001、耗时 +3s
  - 附带修复了评测工具自身的一个 bug：引用正则原先只识别单个 `[n]`，会把 `[2, 6, 12]` 这类复合引用误判为"无引用"，系统性低估 strict grounding 的效果（详见下方"评测体系"）
- [x] **W4 交付上线**：FastAPI + SSE 流式界面（网页端 human-in-the-loop 审核）、Dockerfile + docker-compose（app + Langfuse 自托管，容器内实测跑通）、技术博客 [docs/blog.md](docs/blog.md)
  - 实测：Web 端一次完整研究 67s（并行 + think:false）；容器内 44.7s
  - 待补：README 演示 GIF（可用 ScreenToGif 录制研究过程后替换本行）

## 评测体系（W3）

```bash
python eval/run_eval.py --tag baseline --limit 8 --max-sub 2 --rounds 1   # 跑评测集
python eval/run_eval.py --tag tuned     --limit 8 --max-sub 2 --rounds 1   # 调优后对照批次
python eval/run_eval.py --compare baseline tuned                           # 生成 results/comparison.md
```

- 评测集：`eval/golden_questions.jsonl`（20 题，覆盖概念解释/技术对比/工具选型/技术趋势）
- 评分维度：忠实度（论断能否在笔记中找到依据）、覆盖度（子问题覆盖）、引用质量（LLM judge 1~5）+ 引用有效性/来源利用率（确定性校验 0~1）+ token 用量与成本估算
- A/B 开关全在 `.env`（`RESEARCH_PAGE_CHARS` / `NOTE_MIN~MAX_CHARS` / `WRITER_STRICT_GROUNDING`），换配置即换实验组，两批次同模型同题同参数，对比可复现
- `--recompute TAG`：确定性校验规则修正后重算历史批次，无需重跑研究
- ⚠️ 评测工具本身也要被验证：早期版本的引用正则只匹配单个 `[n]`，把 `[2, 6, 12]` 复合引用判为"无引用"，导致 tuned 被低估（0.90 → 修正后 1.00）。**指标口径错误会直接误导调优方向**，确定性指标尤其要用边界用例自测
- 注意：judge 与研究共用同一模型时存在系统性偏差，分数用于相对对比；对外数字建议用更强模型（如 DeepSeek-V3）复评全量 20 题

