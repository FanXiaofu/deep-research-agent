# deep-research-agent

[![ci](https://github.com/FanXiaofu/deep-research-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/FanXiaofu/deep-research-agent/actions/workflows/ci.yml)

多 Agent 深度研究助手：输入一个研究问题，Agent 团队自动完成 **澄清 → 规划 → 并行检索 → 交叉验证 → 撰写报告**，输出带引用编号的 Markdown 研究报告。

对标 OpenAI Deep Research / 字节 DeerFlow 的开源复刻，差异化点：**自建评测体系 + token 成本控制 + 全链路可观测**。

## 应用场景

**给谁用**：需要产出"可溯源结论"的人——做技术选型的研发、写竞品分析的产品、整理文献综述的学生，以及任何要为自己的判断给出出处的人。

**为什么需要**：用通用搜索或对话模型做调研，答案来得快，但真正的成本在**核对**——这句话出自哪篇文档、是不是最新版本、有没有被二手转述扭曲。搜索引擎只给链接列表，得自己一篇篇翻；对话模型给的结论常常没有出处，或者出处点开一看对不上。

这个项目把"检索 → 筛选 → 交叉核对 → 成稿"这条人工流程交给一组 Agent：

- **规划**：先把问题拆成 3~5 个可独立检索的子问题，避免问得太宽导致检索发散
- **检索**：每个子问题由独立 Agent 并行搜索抓取，各自只回传一条精简笔记（上下文隔离，长流程不互相干扰）
- **核对**：专门的核查角色交叉比对各笔记，把来源之间的矛盾**单列出来**，而不是替你挑一个看起来更顺眼的说法
- **成稿**：引用编号由代码从来源列表生成，不是模型编的；writer 出口再剔除越界 `[n]` 与不在来源清单中的正文链接，报告末尾附完整来源清单

**可溯源怎么验证**：项目自带 20 题评测集与 A/B 对照脚本，引用有效性、来源利用率由脚本逐字校验（不依赖模型自评），数字见[评测体系](#评测体系w3)。

**与在线版 Deep Research 的取舍**：整套流程可以本地跑——模型用本机 Ollama、检索走自己的 key，研究过程与数据不出内网，适合资料敏感、或需要反复跑同一问题做对比的场景。

**不适用**：需要实时行情、需要专业数据库权限、或必须给出唯一确定答案的场景。它擅长把公开资料整理成有据可查的报告，不替代权威信源。

## 架构

```
topic
  │
  ▼
coordinator ── 生成研究任务书（brief）
  │
  ▼
review_brief ─ human-in-the-loop①：审核/改写任务书（REVIEW_POINTS 含 brief 时中断）
  │
  ▼
planner ────── 拆解为 3~5 个子问题（JSON 输出）
  │            ├─ 预算检查：超限则直接收敛
  │            └─ 查研究档案：命中的子问题直接复用历史笔记（不再检索）
  │
  ▼
review_plan ── human-in-the-loop②：审核子问题（"花钱前"最后一道关；仅首次规划，重规划不中断）
  │
  ├─ Send API 并行 fan-out（只派发未命中的子问题）──┐
  ▼                    ▼                   ▼
researcher₁         researcher₂        researcherₙ   ← 上下文完全隔离
（搜索→抓取→摘要→反思循环，搜索走重试+熔断降级，各分支只回传一条精炼笔记，notes 经 reducer 归并）
  │
  ▼
compactor ──── 笔记正文超阈值时压缩（来源列表不变，引用编号不受影响）
  │
  ▼
verifier ───── 交叉比对：标记来源冲突 + 标记尚未回答的缺口
  │
  ├─ 有缺口/冲突且未超重规划上限 ──► 回到 planner（只对新增子问题并行检索）
  ▼
writer ────── 汇总为 Markdown 报告（引用编号由代码确定性生成，防编造；降级/复用历史研究都如实标注）
  │
  ▼
archivist ─── 笔记落档到研究档案（长期记忆，下次同题研究可直接复用）

全图挂 SqliteSaver checkpoint：每步落盘，崩溃/中断后 --resume 精确续跑
全局预算闸门：LLM 调用数 / token / 成本 / 墙钟任一超限即停止新增分支并收敛出报告
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
  - `research.bat "问题" --memory on`　开启研究档案：命中的子问题直接复用历史笔记（同题第二次研究省掉检索）
  - `research.bat --resume`　崩溃/中断后从断点续跑

### 手动方式

```bash
# Git Bash 激活
source /e/AIProject/environment/deep-research-agent-venv/Scripts/activate

# PowerShell 激活
E:\AIProject\environment\deep-research-agent-venv\Scripts\Activate.ps1

# 安装锁定依赖（与 CI / Docker 完全一致，避免版本漂移）
python -m pip install -r requirements.lock

# 1. 已默认配置：本地 Ollama(qwen3:4b) + Tavily，开箱即跑（需先启动 ollama serve）
#    换 DeepSeek：编辑 .env 中注释的 DeepSeek 段并填 key
# 2. 运行（默认会先弹出任务书供人工审核）
python cli.py "LangGraph 和 CrewAI 有什么区别，各适合什么场景" --max-sub 3

# 常用变体
python cli.py "研究问题" --no-review          # 跳过全部人工审核（点位由 REVIEW_POINTS 控制）
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
- 子问题上限 / 检索轮数可自由填写（不再是固定下拉），旁边实时提示"预计 ≈ N 次 LLM 调用"与是否超预算
- 勾选"审核任务书"/"审核子问题"时，对应节点后暂停，可在网页上确认或整组改写后继续（interrupt → resume）
- 报告渲染 + 统计卡片（子问题数/LLM 调用/token/成本估算/耗时），一键复制 Markdown
- 健康检查：`GET /healthz` 返回配置自检结果（模型/搜索/记忆/Langfuse）；缺 key 或数值非法时返回 503 与具体错误项，不发起外部请求
- 请求隔离：每个请求使用独立 thread_id（时间戳 + UUID），usage / 预算 / 降级 / 熔断按 run 隔离；HITL 续跑保留 usage 连续计数，只重算墙钟
- 结构化日志：默认 JSON 行输出到 stdout，含 `request_id` / `thread_id`；响应头透传 `X-Request-ID`，SSE 事件也带这两个 id，便于按一次请求/一轮研究检索日志
- 访问控制：设置 `APP_AUTH_TOKEN` 后所有研究 API 要求 `Authorization: Bearer <token>`；可用 `APP_AUTH_TOKENS` 配置多个 token，限流按 token 分桶；留空时只允许本机回环访问。页面顶部有令牌输入框，令牌只保存在内存中，不写 localStorage
- 运行指标：`GET /metrics` 输出 Prometheus 文本；`/metrics.json` 输出 JSON 快照（请求计数 + research 状态）；需认证
- API 写操作改为 POST JSON（`/api/research/stream`、`/api/resume/stream`），GET 返回 405；浏览器端用 `fetch` + ReadableStream 消费 SSE，Bearer header 天然规避 CSRF
- 输出安全：报告 Markdown 转 HTML 后经 `nh3` 白名单清洗（去 script/事件属性/javascript: 链接）；抓取只允许 http/https 80/443，拒绝私网/链路本地/保留地址，逐跳校验重定向并限制响应字节数

## Docker 部署

```bash
docker compose up -d
# Web 界面  → http://localhost:8000
# Langfuse  → http://localhost:3000（注册后把 key 填入 .env 重启生效）
```

本地 Ollama 用户需在 `docker-compose.yml` 的 app 服务中取消 `LLM_BASE_URL: http://host.docker.internal:11434/v1` 的注释（容器内访问宿主机 Ollama）。镜像从 `requirements.lock` 安装，构建约 413MB，容器内 `/healthz` 已实测 200。

> 公网部署前必须设置 `APP_AUTH_TOKEN`（足够长的随机值），并把 8000 端口放在反向代理/TLS 后面。compose 默认只发布到 `127.0.0.1`，Langfuse 管理面同理；未配置 token 时服务只接受本机回环请求。反向代理如果与服务在同一台机器的 loopback 上，也必须配置 token，否则代理转发来的远程请求会被当作本机访问。
>
> Docker 端口映射后，容器内看到的客户端 IP 不是回环地址；**本地 compose 也必须设置 `APP_AUTH_TOKEN`**，否则研究/续跑/指标接口会返回 403（`/healthz` 仍可用）。镜像以非 root 用户 `appuser`（UID 10001）运行；Linux 上挂载 `./data` 时需 `sudo chown -R 10001:10001 data`。

## CI 与可复现构建（E1）

- `requirements.lock`：85 个依赖锁定到当前测试通过的版本；`requirements.txt` 只保留直接依赖清单。
- `.github/workflows/ci.yml`：`test`（pytest）、`docker`（镜像构建）、`runtime`（容器健康检查 + 非 root）、`postgres`（Postgres 存储集成测试）。
- `Dockerfile` 用 `requirements.lock` 安装，并内置 `/healthz` HEALTHCHECK。
- 本地构建：`docker build -t deep-research-agent .`；国内网络可加 `--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`。

## Postgres 后端（P1.1）

默认 SQLite；设置 `DATABASE_URL` 后，LangGraph checkpoint 与 memory 都切到 Postgres：

```bash
# 本地 compose 起一个 Postgres（也可指向外部托管库）
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d

# 把已有 SQLite 研究档案迁到 Postgres（checkpoint 旧 thread 不迁移，先让在途任务跑完）
python maintenance.py migrate-memory --from memory.db \
  --database-url postgresql://app:app@127.0.0.1:5432/deep_research
```

- 首次连接会自动建 `research_memory`、`memory_schema` 与 LangGraph checkpoint 表；
- `maintenance.py backup/prune/restore` 只维护 SQLite；Postgres 用 `pg_dump` / `pg_restore`；
- 多实例时 checkpoint 与 memory 已可共享，但限流/指标仍是进程级，水平扩展前还需 Redis/负载均衡。

## 数据维护（C1）

checkpoint 与 memory 默认都是 SQLite，统一开启 WAL + `busy_timeout`。备份使用 sqlite3 的 backup API（包含 WAL 中的数据），不要直接拷贝正在写入的 `.db` 文件。

```bash
python maintenance.py prune-checkpoints --days 7   # 0 = 不清理
python maintenance.py backup --out backups
python maintenance.py restore backups/checkpoints-20261029-112638-590354.db --target checkpoints.db --yes
```

- `restore` 前先停止服务；它会先校验备份 `integrity_check`，并自动给现有目标库做一份安全备份。
- Docker 的 `./data` 卷同时保存 `checkpoints.db` 与 `memory.db`；容器重建不会丢研究档案。
- `CHECKPOINT_RETENTION_DAYS` 默认 7 天；生产环境建议用 cron / Task Scheduler 定期执行 `prune-checkpoints`。

更多实现细节与踩坑记录见技术博客：[docs/blog.md](docs/blog.md)；阶段目标、改动、验证与遗留见 [docs/engineering-log.md](docs/engineering-log.md)；升级/回滚/备份/排障见 [docs/runbook.md](docs/runbook.md)；单实例公网部署目标与边界见 [docs/production.md](docs/production.md)，TLS 反代示例见 [deploy/Caddyfile](deploy/Caddyfile)。

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
| `JUDGE_MODEL` | 评测 judge 模型；留空复用 `LLM_MODEL`，建议对外数字用更强模型 |
| `LLM_CALL_TIMEOUT_S` / `LLM_MAX_RETRIES` | 单次调用上限与重试次数；实际 timeout 会按剩余墙钟 / 尝试次数进一步压低 |
| `SEARCH_PROVIDER` | `tavily`（推荐）或 `duckduckgo`（免费兜底） |
| `MAX_SUBQUESTIONS` | 子问题上限，直接控制成本（可在 CLI/Web/评测按次覆盖，范围见 `MAX_SUBQUESTIONS_LIMIT`） |
| `MAX_SEARCH_ROUNDS` | 每个子问题反思循环的最大轮数（同样可按次覆盖，范围见 `MAX_SEARCH_ROUNDS_LIMIT`） |
| `MAX_SUBQUESTIONS_LIMIT` / `MAX_SEARCH_ROUNDS_LIMIT` | 按次覆盖的合法上限（默认 8 / 5）；越界在入口直接报错（CLI 退出、Web 400），不静默降级 |
| `MAX_LLM_CALLS` / `MAX_TOTAL_TOKENS` / `MAX_WALL_CLOCK_S` / `MAX_BUDGET_USD` | 全局预算：任一超限即停止新增分支、把已有笔记收敛成报告，并在报告里标注受限原因 |
| `MAX_REPLAN_ROUNDS` / `MAX_FOLLOWUP_SUBQUESTIONS` | 重规划轮数上限与每轮新增子问题数上限 |
| `REVIEW_POINTS` / `PLAN_REVIEW_MIN_SUBQUESTIONS` | 人工审核点位：`brief`（任务书）/ `plan`（子问题），逗号分隔，空=全自动；plan 点位仅首次规划生效，子问题数低于阈值时不暂停 |
| `COMPACTION_ENABLED` / `COMPACT_TRIGGER_CHARS` / `COMPACT_TARGET_CHARS` | 笔记压缩开关、触发阈值、压缩后总字数目标 |
| `SEARCH_RETRIES` / `BREAKER_FAILURE_THRESHOLD` / `BREAKER_COOLDOWN_S` | 搜索重试次数、熔断阈值、熔断冷却时间 |
| `LOG_LEVEL` / `LOG_JSON` | 日志级别与格式；默认 `INFO` + JSON 行，本地想看可读文本设 `LOG_JSON=0` |
| `APP_AUTH_TOKEN` | Bearer 访问令牌；留空仅允许本机回环，公网部署必须设置足够长的随机值 |
| `APP_AUTH_TOKENS` | 多客户端 token，逗号分隔；与 `APP_AUTH_TOKEN` 合并，限流按 token 分桶 |
| `APP_RATE_LIMIT_PER_MINUTE` | 单进程每 IP 每分钟研究/续跑请求上限（默认 30）；多 worker 需换共享限流 |
| `APP_MAX_TOPIC_CHARS` / `APP_MAX_REVIEW_CHARS` | 研究主题与人工审核内容的长度上限 |
| `APP_MAX_REQUEST_BYTES` | 单请求 Content-Length 上限（默认 64KB）；chunked 请求体需在反向代理层限制 |
| `MAX_FETCH_BYTES` / `FETCH_MAX_REDIRECTS` | 单页最多读取字节数（默认 2MB）与最大重定向次数（默认 3） |
| `MEMORY_ENABLED` / `MEMORY_DB` | 研究档案（长期记忆）开关与库文件；**默认关闭**，开启后命中记忆的子问题不再检索 |
| `MEMORY_SIMILARITY_THRESHOLD` / `MEMORY_MAX_AGE_DAYS` / `MEMORY_MAX_HITS` | 记忆命中阈值（默认 0.62）、记忆寿命、单轮最多复用条数 |
| `OLLAMA_NUM_CTX` / `LLM_NATIVE_OLLAMA` | Ollama 原生路径的上下文窗口与开关（原生 `/api/chat` 才能关掉 qwen3 thinking）；提示词逼近上限时在 stderr 告警 |
| `DATABASE_URL` | 配置后 checkpoint 与 memory 走 Postgres；留空使用 SQLite。例：`postgresql://app:app@127.0.0.1:5432/deep_research` |
| `CHECKPOINT_DB` | SQLite checkpoint 文件路径（默认 `checkpoints.db`）；容器部署时应放到持久化卷 |
| `CHECKPOINT_RETENTION_DAYS` | checkpoint 保留天数（默认 7；0=不清理），由 `maintenance.py prune-checkpoints` 执行 |
| `LLM_PRICE_INPUT_PER_M` / `LLM_PRICE_OUTPUT_PER_M` | 成本估算单价（USD / 1M tokens）；本地 Ollama 想显示 0 成本时显式设为 0 |
| `LANGFUSE_PUBLIC_KEY/SECRET_KEY` | 都配置才启用追踪，留空自动降级关闭 |

### 启动自检

CLI 与 Web 启动时都会调用 `config.validate_runtime_config()`，以下情况直接失败，而不是带病启动后在检索阶段退化成空报告：

- `LLM_BASE_URL` / `LLM_MODEL` 为空；
- 远程 LLM 未配置 `LLM_API_KEY`（本地 Ollama 走 `localhost` / `127.0.0.1` / `host.docker.internal` 时可留空，或填 `EMPTY`）；
- `SEARCH_PROVIDER=tavily` 但 `SEARCH_API_KEY` 为空；
- 预算、规模、递归上限等数值非正数，或默认规模超过按次覆盖上限。

`GET /healthz` 返回 `200` 与 `{"status":"ok",...}`；未就绪返回 `503` 与 `errors` 列表。它只读本地配置，不访问 LLM/搜索。

## 记忆分层（W6）

三层的职责与边界分得很清，面试里最容易被追问的正是这里：

| 层 | 实现 | 解决的问题 | 不解决什么 |
|---|---|---|---|
| 工作记忆 | `graph/state.py` 的 `ResearchState` + `notes`；`compactor` 节点做压缩 | 并行分支的中间产物归并、长流程上下文不爆 | 不做跨会话记忆 |
| 会话记忆 | `SqliteSaver` checkpoint（thread_id + `--resume`） | 进程被杀后精确恢复，已完成分支不重复花钱 | **checkpoint ≠ memory**：它是状态持久化，不改变未来决策 |
| 长期记忆 | `memory.py` 研究档案 + `archivist` 节点 + `planner` 复用 | 跨运行复用历史笔记：同题/近似题第二次研究直接省掉检索 | 不替代本轮核验（复用仍经 verifier 与 writer，报告标注来源与日期） |

研究档案的设计取舍（细节见 `memory.py` 顶部注释）：

- **相似度用字符 bigram Jaccard，不引依赖**；阈值是量出来的：同一句 1.00 / 近似重复 0.87 / 子集 0.73 / 同义改写 0.39 / 同题不同维度 0.44 / 无关 0.00。
- 注意 **0.39 < 0.44**：该复用与绝不该复用的情况在 0.4 附近重叠，阈值定在重叠区之上（0.62），明确选择**宁可漏复用，不可误复用**（误复用 = 拿 A 维度旧结论回答 B 维度；漏复用 = 多搜一次）。要覆盖同义改写需要语义匹配，当前刻意不做。
- **记忆有寿命**：超过 `MEMORY_MAX_AGE_DAYS` 不再复用；且只有"重新检索"才刷新时间 —— 反复复用不会让旧结论变成永久记忆（遗忘策略）。
- **回放可见**：报告里附「复用历史研究说明」节，列出复用子问题、相似度、记忆时间；CLI/前端也有对应提示与统计卡。

```bash
# 有记忆的 A/B：先重置 → 第一轮落档 → 第二轮复用
python eval/run_eval.py --tag mem1 --limit 8 --memory on --reset-memory   # 起点为空，行为同无记忆
python eval/run_eval.py --tag mem2 --limit 8 --memory on                  # 第二轮命中记忆
python eval/run_eval.py --compare mem1 mem2                              # 看 token/耗时下降与质量是否守住
```

## 长流程控制（W5）

长流程的失败模式不是"某一步报错"，而是"跑到一半没预算了 / 依赖挂了 / 上下文塞爆了却没人知道"。四类控制都做成可配置 + 可审计：

| 控制 | 机制 | 可观测 |
|---|---|---|
| 全局预算 | planner / researcher 在开新分支前查预算（LLM 调用数 / token / 成本 / 墙钟），超限即写入 `budget_exceeded` 并收敛到 writer；单次 LLM timeout 与重试按剩余墙钟均摊，预算耗尽前不再发起调用 | CLI 与前端显示剩余额度；报告附"研究受限说明"节；评测逐题记录 |
| 依赖熔断 | 搜索重试 `SEARCH_RETRIES` 次；连续失败达阈值后熔断 `BREAKER_COOLDOWN_S` 秒（closed → open → half-open 探活），抓取同样按结果成功率熔断 | 每次降级写入降级日志（组件 + 原因），CLI/前端/评测状态文件都能看到；不再"悄悄跳过" |
| 运行时压缩 | 笔记正文超 `COMPACT_TRIGGER_CHARS` 时，compactor 节点先压缩再交给核查/撰写；**来源列表原样保留**，所以引用编号注册表不受影响；LLM 不可用时按目标字数确定性截断兜底 | 状态里记录压缩前后字数与倍数；前端时间线出现"压缩"节点 |
| 动态重规划 | verifier 除冲突外还报告"尚未回答的缺口"；有缺口/冲突且未超 `MAX_REPLAN_ROUNDS` 时回到 planner，**只对新增子问题**做 fan-out（已完成分支不重跑） | CLI 打印"第 N 轮重规划，新增 M 个子问题"；评测记录 `replan_count` |
| 长期记忆 | planner 查研究档案，命中的子问题直接复用历史笔记（不开检索分支）；archivist 在报告后落档；记忆有寿命、只复用笔记不复用报告 | CLI/前端显示"复用历史研究 N 条"；报告的「复用历史研究说明」节；评测记录 `memory_reused` |
| 引用后校验 | writer 出口按来源注册表剔除越界 `[n]`、把未知正文链接降级为纯文本；结果附「引用校验说明」 | CLI/前端显示移除计数；评测记录 `invalid_citations` / `unknown_links` |

配套的工程化改动：节点级 `retry_policy`、`GRAPH_RECURSION_LIMIT`（防循环失控）、`chat_safe`/`chat_json_safe` 降级包装（单节点失败不再毁掉整轮研究，writer 在模型不可用时用笔记兜底出报告）。

运行期状态（usage / 预算时钟 / 降级事件 / 熔断器）按 `run_id`（通常等于 `thread_id`）隔离：同一进程内的并发请求、评测不同题、HITL 续跑互不污染；续跑不重置 usage，只重算墙钟。节点入口通过 `run_context.run_scope(state["run_id"])` 绑定，并行 researcher 也能读到同一份 run 状态。

```bash
python -m pytest tests -q     # 158 个用例 + 3 个 Postgres 用例（未配置 TEST_DATABASE_URL 时跳过）：预算、超时/重试、熔断、路由、压缩、记忆、审核、规模/配置、请求隔离、认证/限流/XSS/SSRF、WAL/备份恢复、引用后校验、日志/指标、评测分位数、Postgres 存储
```

## 里程碑

- [x] **W1 最小闭环**：LangGraph 线性流水线 + Tavily 检索 + CLI，产出带引用报告
- [x] **W2 并行与工程化**：researcher 并行 fan-out（Send API）、human-in-the-loop（interrupt）、SQLite checkpoint 断点恢复（--resume）、Langfuse 全链路追踪
  - 实测：researcher 阶段 321s → 180s（本地单模型有排队损耗）；强杀进程后 `--resume` 从断点精确恢复未完成的并行分支
- [x] **W3 评测体系**：20 题评测集 + LLM-as-judge（忠实度/覆盖度/引用质量）+ 引用确定性校验 + token 成本统计，A/B 调优对比（**全量 20 题**）
  - 实测（qwen2.5:7b 本地，judge 同模型，两批次同题同模型）：**引用有效性 0.82 → 1.00**（baseline 有 3/20 题报告完全无引用，tuned 零失败）、**来源利用率 0.622 → 0.748（+20%）**；忠实度持平（4.8 vs 4.8），覆盖度 4.80 → 4.75、引用质量 4.85 → 4.75 小幅波动（7B judge 分辨力所限），单题成本 +$0.001、耗时 +3s
  - 口径说明：该 A/B 同时调整了 `RESEARCH_PAGE_CHARS`、`NOTE_MIN/MAX_CHARS`、`WRITER_STRICT_GROUNDING` 四个参数；代码侧引用编号注册表在 baseline 批次就已存在，因此 0.82 → 1.00 不能单独归因于"代码生成编号"，需后续单变量实验拆分贡献
  - 附带修复了评测工具自身的一个 bug：引用正则原先只识别单个 `[n]`，会把 `[2, 6, 12]` 这类复合引用误判为"无引用"，系统性低估 strict grounding 的效果（详见下方"评测体系"）
- [x] **W4 交付上线**：FastAPI + SSE 流式界面（网页端 human-in-the-loop 审核）、Dockerfile + docker-compose（app + Langfuse 自托管，容器内实测跑通）、技术博客 [docs/blog.md](docs/blog.md)
  - 实测：Web 端一次完整研究 67s（并行 + think:false）；容器内 44.7s
  - 待补：README 演示 GIF（可用 ScreenToGif 录制研究过程后替换本行）
- [x] **W5 长流程控制**：全局预算闸门（4 维度）+ 依赖熔断与降级留痕 + 运行时笔记压缩 + verifier→planner 动态重规划回边 + 节点级重试 + 28 个单元测试
  - 端到端实测（桩 LLM/搜索，覆盖真实 LangGraph 拓扑）：正常+重规划路径（3 条笔记 / 参考来源 6 条 / 冲突展示去重）、预算超限收敛（不再开分支、报告标注受限原因）、压缩 1600→428 字（来源零丢失、引用编号仍连续）、搜索全挂时分支不炸且仍产出报告
  - 评测口径补充：`must_cover` 清单接入覆盖度评分（此前该字段存在于数据集却从未参与评分），新增 `must_cover_coverage` 指标与逐题降级/预算/压缩标记
- [x] **W6 记忆分层**：研究档案（长期记忆）+ archivist 落档节点 + planner 记忆复用 + 报告「复用历史研究说明」+ 记忆寿命与命中统计 + 26 个记忆相关用例（合计 54）
  - 阈值不是拍脑袋：实测同一句 1.00 / 近似重复 0.87 / 子集 0.73 / 同义改写 0.39 / 同题不同维度 0.44，**两类在 0.4 附近重叠**，因此阈值定在 0.62 并明确取舍"宁可漏复用，不可误复用"（取舍已固化为测试）
  - 图级验证：命中记忆时检索分支完全不发起（用一个"被调用就抛异常"的假 provider 证明）、来源列表随记忆完整复用、复用笔记同样受压缩管辖、未命中时行为与开启前一致
- [x] **W7 规划审核点**：human-in-the-loop 泛化为 `REVIEW_POINTS`（`brief` / `plan`），在 planner 拆解之后、fan-out 之前增加第二个中断点（"花钱前"的关）；重规划不再中断，`PLAN_REVIEW_MIN_SUBQUESTIONS` 支持按规模条件触发；中断协议统一为 `{kind, title, content}`，CLI 与 Web 共用一套渲染（合计 60）
  - 取舍：任务书只是原始问题的改写、出错概率低；子问题的拆解才是成本决策点（N 个子问题 = N 条并行检索分支），因此在付费前拦截回报最高，同时闸门化避免小任务被无谓打断
- [x] **W8 规模可自定义**：子问题上限 / 检索轮数从固定下拉改为自由填写；入口统一校验（`config.validate_scale`，CLI 报错退出 / Web 400）+ 范围上限（`MAX_SUBQUESTIONS_LIMIT` / `MAX_SEARCH_ROUNDS_LIMIT`）；per-run 覆盖改走 state（`effective_max_subquestions` / `effective_max_search_rounds`）而非改全局 config，修掉同进程并发请求互相覆盖的隐患；前端实时提示"预计 ≈ N 次 LLM 调用"及超预算预警（合计 74）
  - 踩坑：`rounds <= 0` 会让 `researcher` 的 `range()` 一次都不跑 → 直接产出空笔记 + 空报告**且不报错**；`max_sub = 0` 又会被 `if max_sub:` 当 falsy 静默忽略。两者都在入口被显式拦下，并固化为测试
- [x] **A1 交付基线**：启动配置自检（`config.validate_runtime_config`）+ `GET /healthz` + 清理死文件；测试 95 个
  - 目标：配置错误在启动时暴露，而不是等到检索/撰写阶段退化成"未收集到资料"的空报告；`/healthz` 只读本地配置，不发起外部请求
- [x] **B1 请求隔离**：usage / 预算时钟 / 降级事件 / 熔断器按 `thread_id` 隔离（`run_context.py`）；Web thread_id 加 UUID 后缀；HITL 续跑保留 usage、只重算墙钟；评测每题独立 thread，同 tag 可重跑；测试 106 个
  - 验证：三路并发 SSE 流各自只统计自己的调用；同 thread 重跑不再追加旧笔记；续跑 `BUDGET.reset()` 不会清零已用调用数
- [x] **D1 安全边界**：Bearer 认证 + 本机回环兜底；研究/续跑改 POST JSON（Bearer header 天然免 CSRF）；报告 HTML 经 `nh3` 白名单清洗；抓取侧 SSRF 防护（协议/端口/私网/逐跳重定向/字节上限）；每 IP 限流与输入长度上限；测试 127 个
  - 部署默认值：compose 只发布 `127.0.0.1`，公网必须设置 `APP_AUTH_TOKEN` 并放在反向代理/TLS 后面
- [x] **C1 数据可靠性**：checkpoint/memory 统一 `db.connect`（WAL + busy_timeout）；memory schema 用 `PRAGMA user_version` 记版本；checkpoint 按最后时间清理；备份/恢复 CLI（sqlite backup API + integrity_check + 恢复前安全备份）；Docker `memory.db` 挂载到数据卷；测试 133 个
  - 备份包含 WAL 中的数据；恢复前自动给目标库做安全备份，避免误覆盖无可回退
- [x] **R3 预算硬中断**：单次 LLM timeout / 重试受剩余墙钟约束（`_request_timeout`），预算耗尽前不再发起调用；OpenAI 兼容与 Ollama 原生路径都逐次计算；测试 138 个
  - 目的：避免一次卡住的模型调用绕过 `MAX_WALL_CLOCK_S`，让"预算硬闸门"在途调用上也成立
- [x] **E1 可复现构建**：`requirements.lock`（77 个锁定依赖）+ GitHub Actions（pytest + docker build）+ Dockerfile 改用锁文件并加 HEALTHCHECK
  - 实测：镜像 413MB；容器内 `/healthz` 返回 200；`nh3/langgraph/fastapi` 导入正常；Actions run #1/#2 成功
- [x] **R10 引用后校验**：writer 出口剔除越界 `[n]`、把未知正文链接降级为纯文本，审计写入状态/CLI/前端/评测；报告附「引用校验说明」
  - 测试 144 个；与评测侧共用 `citations.py` 的引用正则，避免两套口径漂移
- [x] **E2 可运维性**：JSON 结构化日志（request_id/thread_id）、请求头 `X-Request-ID`、异常堆栈落日志；新增 [docs/runbook.md](docs/runbook.md)（升级/回滚/备份/密钥轮换/排障）
  - 测试 149 个；真实 uvicorn 验证 JSON 行与 request_id 透传
- [x] **F1 可测量评测**：`--set KEY=VALUE` 单变量覆盖；汇总输出 p50/p95；`JUDGE_MODEL` 支持独立 judge；历史 baseline/tuned 批次已重算分位数
  - 测试 154 个；历史批次 p50/p95 见 [comparison.md](eval/results/comparison.md)；真实单变量批次需模型在线时补跑
- [x] **D3 指标与非 root 容器**：`GET /metrics`（请求计数 + research 状态）；镜像改为 UID 10001 非 root 运行；Docker 无 token 启动时写 warning
  - 测试 156 个；容器内 `id -u` = 10001，`/healthz` 200，带 token 的 `/metrics` 正常
- [x] **P0 生产化切片**：多 token + 按 token 限流；Prometheus 文本指标；CI 增加容器 runtime job（健康检查 + 非 root）；Caddy TLS 反代示例与 [docs/production.md](docs/production.md)
  - 测试 158 个；单实例公网服务目标；多实例的 Redis/RBAC 明确列为 P1
- [x] **P1.1 Postgres 后端**：checkpoint/memory 支持 `DATABASE_URL`（SQLite fallback）；自动建表；`migrate-memory` 迁移研究档案；CI 增加 Postgres service job；compose 可选 Postgres profile
  - Postgres 集成测试 3 个通过；旧 checkpoint 不迁移，切换前需让在途任务完成；限流/指标仍为进程级

## 评测体系（W3）

```bash
python eval/run_eval.py --tag baseline --limit 8 --max-sub 2 --rounds 1   # 跑评测集
python eval/run_eval.py --tag tuned     --limit 8 --max-sub 2 --rounds 1   # 调优后对照批次
python eval/run_eval.py --tag strict --limit 20 --set WRITER_STRICT_GROUNDING=1  # 单变量实验
python eval/run_eval.py --compare baseline tuned                           # 生成 results/comparison.md
```

- 评测集：`eval/golden_questions.jsonl`（20 题，覆盖概念解释/技术对比/工具选型/技术趋势，每题带 `must_cover` 必覆盖要点）
- 评分维度：忠实度（论断能否在笔记中找到依据）、覆盖度（子问题 + 必覆盖要点）、引用质量（LLM judge 1~5）+ 必覆盖要点覆盖率 / 引用有效性 / 来源利用率（0~1）+ token 用量与成本估算
- 忠实度的对照物与 writer 实际看到的证据一致（压缩生效时用压缩后的笔记视图，避免"报告 vs 未压缩原文"系统性偏松）
- 逐题记录预算是否超限、是否压缩、重规划轮数、降级次数：**降级过的题不应与正常题混在一起看均值**
- A/B 开关全在 `.env`（`RESEARCH_PAGE_CHARS` / `NOTE_MIN~MAX_CHARS` / `WRITER_STRICT_GROUNDING`，以及 W5 的预算/压缩/重规划参数）；`--set KEY=VALUE` 可按次覆盖配置，**一次实验建议只改一项**，配置差异由 `comparison.md` 的「配置差异」表逐项列出
- 汇总输出均值 + p50/p95（耗时、成本、输入/输出 tokens），避免只看均值掩盖长尾
- `JUDGE_MODEL` 可指定独立 judge 模型；留空复用研究模型，便于用更强模型复评
- `--recompute TAG`：确定性校验规则修正后重算历史批次，无需重跑研究
- ⚠️ 评测工具本身也要被验证：早期版本的引用正则只匹配单个 `[n]`，把 `[2, 6, 12]` 复合引用判为"无引用"，导致 tuned 被低估（0.90 → 修正后 1.00）。**指标口径错误会直接误导调优方向**，确定性指标尤其要用边界用例自测
- 注意：judge 与研究共用同一模型时存在系统性偏差，分数用于相对对比；对外数字建议用 `JUDGE_MODEL` 指定更强模型复评全量 20 题
- 历史 baseline/tuned 批次已按当前口径重算 p50/p95：baseline 耗时 p50 63.7s / p95 88.755s；tuned p50 72.25s / p95 82.13s（同一批历史数据，非新的单变量实验）

