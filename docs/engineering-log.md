# 工程化落地记录

- 仓库：`deep-research-agent`
- 分支：`main`
- 最后更新：2026-10-29
- 验证基线：`python -m pytest tests -q` → **188 passed + 6 集成用例（4 Postgres / 2 Redis，需对应 URL）**
- 说明：本文件按阶段追加，记录目标、改动、验证、遗留与决策；后续任务完成后继续往下写。

## 1. 当前状态

本地提交（按时间顺序）：

| 提交 | 内容 |
|---|---|
| `461fd81` | 固化 W5–W8 工作区基线，打 tag `v0.1.0-audit-baseline` |
| `5550bc8` | A1：启动配置自检 + `/healthz` + 清理死文件 |
| `8b59429` | B1：运行期状态按 thread 隔离 |
| `2afe08d` | D1：Bearer 认证、POST + token、XSS/SSRF、限流 |
| `1851adc` | C1：SQLite WAL、schema 版本、checkpoint 清理、备份/恢复 |
| `5505601` | R3：LLM 调用受剩余墙钟预算约束 |
| `5bd630f` | 工程记录文档 |
| `dd5ca26` | E1：requirements.lock + CI + Docker healthcheck |
| `c1cc3a9` | R10：writer 出口引用/链接校验 |
| `4daa6f6` | E2：结构化日志 + 运行手册 |
| `2f732ba` | F1：可测量评测 + 单变量覆盖 |
| `e73d3af` | D3：/metrics + 非 root 容器 |
| `d5d5861` | P0：多 token、Prometheus 指标、CI runtime、Caddy/生产文档 |
| `319693f` | P1.1：Postgres checkpoint/memory + 迁移 + CI postgres job |
| `2999f8e` | P1.2：Redis 共享限流/指标 + CI redis job |
| `f97a57e` | P1.3：账号/API token/RBAC/配额/审计 |
| `c497805` | P1.4：多副本 + Caddy/nginx + `/readyz` + 日志聚合 |
| `a49fe66` | P1.5：加密备份 + secret + CSP + Trivy/Dependabot |
| `1c2fe02` | CI：Trivy 改为直接跑固定版本镜像 |
| `58b2191` | P1.6：证据支撑审计 + 网页不可信包裹 |
| `8b4c2d2` | P1.7：真实 smoke 修复 + 可选内容安全审计 |
| `451c818` | P1.8：20 题 baseline + kill/restart 修复 |
| `48c2e51` | 评测集扩到 110 题 + suite 分层 |

推送状态：

- 已通过 SSH 推送成功：`origin = ssh://git@ssh.github.com:443/FanXiaofu/deep-research-agent.git`；
- 远端 `main` 已包含到 `5aa8bf9`，tag `v0.1.0-audit-baseline` 已推送；P1.4 / P1.5 / P1.6 / P1.7 已推送；
- 本机用 `core.sshCommand` 指定专用 key 与 `known_hosts`，不再依赖已失效的 GCM token。

## 2. 阶段记录

### A1 交付基线

- 目标：把 W5–W8 从“只在工作区”固化为可回退版本；配置错误在启动时暴露。
- 改动：
  - `config.validate_runtime_config()`：校验 LLM base/model、远程 key、Tavily key、预算/规模/抓取参数；
  - CLI 与 FastAPI lifespan 使用同一套校验；
  - `GET /healthz`：200/503，只读本地配置，不访问外部服务；
  - 删除 `test.py`、`$null`；README 测试数与口径同步。
- 验证：95 tests；缺 Tavily key 时 CLI exit 1、Web lifespan 拒绝启动；`/healthz` 实测 200。
- 遗留：`/healthz` 不探测外部依赖连通性（只做配置就绪判断）。
- 提交：`5550bc8`。

### B1 请求隔离

- 目标：并发请求、评测重跑、HITL 续跑不互相污染。
- 改动：
  - `run_context.py`：`usage / 预算时钟 / 降级事件 / 熔断器` 按 `run_id` 隔离；
  - 图节点入口 `_bind_run`，`Send` payload 携带 `run_id`；
  - Web thread_id 加 UUID 后缀；`_stream` 改为 worker 线程 + 队列，避免 sync 生成器跨 context 重置 ContextVar token；
  - resume 保留 usage、只重算墙钟并清 breaker；
  - 评测每题新建 thread，同 tag 可重跑。
- 验证：106 tests；三路并发 SSE 流统计互不污染；同 thread 重跑不再追加旧 notes。
- 遗留：`SqliteSaver` 仍是进程级单连接；多 worker 需要换存储。
- 提交：`8b59429`。

### D1 安全边界

- 目标：具备公网部署的最低安全边界。
- 改动：
  - `app/security.py`：Bearer token；未配置 token 时只允许回环地址；每 IP 每分钟限流；
  - 研究/续跑改 POST JSON，token 走 Authorization header，无 cookie CSRF 面；
  - 报告 HTML 经 `nh3` 白名单清洗；
  - `fetch_page` SSRF 防护：http/https、80/443、拒绝非 global IP、逐跳校验重定向、响应字节上限；
  - 请求体/主题/审核内容长度上限；
  - compose 默认只发布 `127.0.0.1`，Langfuse 管理面同样只绑本机。
- 验证：127 tests（认证、限流、XSS、SSRF、POST SSE）；真实 uvicorn 冒烟 200/405/400/401；未触发真实研究。
- 遗留：限流是单进程内存实现；chunked body 需反向代理限制；DNS rebinding 残余窗口已在代码注释说明。
- 提交：`2afe08d`。

### C1 数据可靠性

- 目标：SQLite 持久化在并发/重启/误操作下可恢复。
- 改动：
  - `db.connect`：WAL、`busy_timeout=30s`、`synchronous=NORMAL`；
  - memory schema 用 `PRAGMA user_version` 记版本（`SCHEMA_VERSION=1`）；
  - `maintenance.py`：过期 checkpoint 清理、在线备份、恢复（integrity_check + 恢复前安全备份）；
  - compose 把 `MEMORY_DB` 放到 `/app/data`，与 checkpoint 共用数据卷；
  - `backups/` 加入 git/docker ignore。
- 验证：133 tests；checkpoint/memory `journal_mode=wal`；备份→修改→恢复往返；坏备份被拒绝；`prune --days 0` 不删除数据。
- 遗留：清理未内置调度器；恢复要求先停服务；备份未加密；SQLite 仍是单写者模型。
- 提交：`1851adc`。

### R3 预算硬中断

- 目标：一次卡住的 LLM 调用不能绕过 `MAX_WALL_CLOCK_S`。
- 改动：
  - `llm/client._request_timeout()`：剩余墙钟 / 最大尝试次数，再与 `LLM_CALL_TIMEOUT_S` 取小；
  - 预算耗尽时在创建客户端前抛 `BudgetExceeded`；
  - OpenAI 兼容与 Ollama 原生路径都使用同一约束；重试次数由 `LLM_MAX_RETRIES` 控制；
  - `chat_safe` 仍负责把失败转成降级事件，不毁掉整轮研究。
- 验证：138 tests；剩余 10s、4 次尝试时 timeout ≈ 2.5s；预算耗尽不调用客户端；native 路径同样受限。
- 遗留：已经发出的阻塞调用只能靠 timeout 收敛，不能从外部强行取消；多 worker 下墙钟仍是各自计算。
- 提交：`5505601`。

### E1 可复现构建

- 目标：让 CI/Docker/本地安装使用同一套锁定依赖，并能实际构建、启动、通过健康检查。
- 改动：
  - `requirements.lock`：从测试通过的 venv 锁定 77 个包；`requirements.txt` 保留直接依赖；
  - `.github/workflows/ci.yml`：`test` job（锁文件安装 + pytest）与 `docker` job（镜像构建）；
  - `Dockerfile`：改用锁文件安装，`PIP_INDEX_URL` 改为构建参数，内置 `/healthz` HEALTHCHECK；
  - README 增加 CI 章节、锁文件安装步骤与镜像实测尺寸。
- 验证：
  - `docker build` 成功，镜像 413MB；
  - 容器内 `/healthz` 返回 200；`nh3 / langgraph / fastapi` 导入正常；
  - GitHub Actions run #1（`5148414`）`test` + `docker` 两个 job 均成功：
    https://github.com/FanXiaofu/deep-research-agent/actions/runs/40815035190
  - 本地 `pytest` 仍为 138 passed。
- 遗留：升级/回滚 runbook、非 root 容器、结构化日志/指标仍未做。
- 提交：`dd5ca26`。

### R10 引用运行时后校验

- 目标：不再只在评测阶段发现越界引用/未知链接，而是在报告出口直接拦截。
- 改动：
  - 新增 `citations.py`：引用正则、`sanitize_citations`、`sanitize_links`、`validate_report`；
  - `eval/judge.py` 改为共用同一套引用正则，避免 writer 与评测口径漂移；
  - `writer` 在模型/兜底报告之后校验：越界 `[n]` 删除，未知正文链接降级为纯文本；
  - 校验结果写入 `citation_audit`（state），报告附「引用校验说明」，CLI/前端/评测均可见；
  - `_limitation_section` 过滤 citation 事件，避免与引用校验说明重复。
- 验证：144 tests；`[99]` 被移除、`[2, 99]` 保留 `[2]`、未知链接降级、合法链接/锚点保留；模型不可用时的兜底报告同样被校验。
- 遗留：只做确定性剔除，不判断引用与论断的语义相关性；无引用/少引用的报告仍可能质量不足。
- 提交：`c1cc3a9`。

### E2 可运维性

- 目标：公网服务出问题时，能按 request/thread 检索日志并完成升级、回滚、恢复。
- 改动：
  - `logging_config.py`：JSON 行日志、`request_id` / `thread_id` 上下文、文本格式开关；
  - Web 中间件分配/透传 `X-Request-ID`，记录 method/path/status/duration/client；
  - `_stream` worker 绑定 request/thread 日志上下文，异常记录完整堆栈；SSE 事件带两个 id；
  - auth 失败/限流写结构化 warning；Ollama 上下文告警改为 logger；
  - `docs/runbook.md`：发布前检查、启动/健康检查、升级、回滚、备份恢复、密钥轮换、常见故障、监控建议、安全默认值。
- 验证：149 tests；真实 uvicorn 冒烟验证 JSON 行、`X-Request-ID` 透传、400 请求日志。
- 遗留：指标（metrics）与非 root 容器未做；限流仍为单进程实现。
- 提交：`4daa6f6`。

### F1 可测量评测

- 目标：让 A/B 可按单变量执行，并能看到长尾（p50/p95）而不是只看均值。
- 改动：
  - `eval/run_eval.py --set KEY=VALUE`：按 config 现有类型解析 int/float/bool/str，覆盖后写入 config snapshot；
  - summary / comparison 增加耗时、成本、输入/输出 tokens 的 p50/p95；
  - `JUDGE_MODEL` 支持独立 judge 模型，留空复用研究模型；
  - 历史 baseline/tuned 批次用 `--recompute` 重算分位数，不重跑研究。
- 验证：
  - 154 tests；
  - 历史批次（同一批数据，非新单变量实验）：baseline 耗时 p50 63.7s / p95 88.755s，tuned p50 72.25s / p95 82.13s；
  - 成本 p50 0.004/0.005 USD；输入 tokens p50 9917/11796.5；
  - 本地 Ollama 当时不可用，未跑新的真实单变量批次。
- 遗留：历史 A/B 仍同时改了 4 个参数，不能替代单变量结论；真实单变量实验与独立强 judge 复评需要模型在线时补跑。
- 提交：`2f732ba`。

### D3 运行指标与非 root 容器

- 目标：补齐可直接采集的运行指标，并降低容器权限。
- 改动：
  - 新增 `metrics.py`：请求计数、research started/finished/failed/active、outcome 分布；
  - `GET /metrics` 返回 JSON（需认证，本机回环可免）；
  - `_stream` worker 记录 research outcome；异常路径保持 failed；
  - Dockerfile 改为 `appuser`（UID 10001）运行，`COPY --chown` + 数据目录 chown；
  - Docker 无 token 启动写 warning（端口映射后客户端不是回环，接口会 403）；
  - runbook/README 补充 Docker token、bind mount 权限与 metrics 采集命令。
- 验证：156 tests；容器内 `id -u`=10001，`/healthz` 200，带 token 的 `/metrics` 返回请求计数与 research 状态；研究接口带 token 返回 400（参数校验），未带 token 按预期拒绝。
- 遗留：metrics 为进程级 JSON，未接 Prometheus；多 worker 下计数各自独立。
- 提交：`e73d3af`。

### P0 单实例公网生产化

- 目标：把“可部署”推进到“单实例公网服务可运维”，明确不追求多租户/多实例。
- 改动：
  - `APP_AUTH_TOKENS` 支持多客户端 token，限流按 token 分桶；
  - `/metrics` 输出 Prometheus 文本，`/metrics.json` 保留 JSON 快照；
  - GitHub Actions 增加 `runtime` job：启动容器、检查 `/healthz`、验证 UID 10001；
  - 新增 `deploy/Caddyfile`：TLS 自动证书、HSTS/CSP、SSE `flush_interval -1`；
  - 新增 `docs/production.md`：目标拓扑、部署步骤、安全/备份/监控/容量/发布回滚、P1 扩展触发条件。
- 验证：158 tests；Prometheus 文本包含请求/研究指标；本机容器 `/healthz`、`/metrics`（带 token）、非 root UID 已验证；CI runtime job 待本轮推送后确认。
- 遗留（明确不覆盖）：多租户/RBAC、Redis、多实例水平扩展、语义级引用核验、加密备份/异地容灾、镜像扫描/WAF、值班/SLO。
- 提交：`d5d5861`。

### P1.1 Postgres 存储

- 目标：把 checkpoint 与 memory 从 SQLite 单写者迁到 Postgres，保留 SQLite 本地 fallback。
- 改动：
  - `DATABASE_URL` 配置后 `make_checkpointer()` 使用 `PostgresSaver` + `psycopg_pool`（autocommit，兼容 `CREATE INDEX CONCURRENTLY`）；
  - `memory.py` 增加 Postgres 后端：自动建表、`%s` 占位符、`memory_schema` 版本表；显式 `db_path` 时仍走 SQLite（测试/本地）；
  - `maintenance.py migrate-memory`：SQLite 研究档案 upsert 到 Postgres；备份/恢复明确只维护 SQLite；
  - `docker-compose.postgres.yml` 可选 profile；CI 增加 postgres service job；
  - `docs/production.md` / `runbook.md` 补 Postgres 部署、迁移、`pg_dump`/`pg_restore`。
- 验证：158 SQLite tests + 3 Postgres 集成测试（memory 往返、checkpointer setup、SQLite→Postgres 迁移）通过；compose override 校验通过。
- 遗留：旧 checkpoint 不迁移（切换前需完成在途任务）；Postgres checkpoint 保留策略未内置。
- 提交：`319693f`。

### P1.2 Redis 共享状态

- 目标：多实例下限流与指标不再各算各的。
- 改动：
  - `REDIS_URL` 启用 Redis 固定窗口限流，按 token/IP 分桶；Redis 故障回退进程内限流并写 warning；
  - `/metrics` 从 Redis 汇总 HTTP 请求与 research started/finished/active；`/metrics.json` 同步；
  - Redis 故障 30 秒内暂停重试，避免每次指标写入都超时；
  - 新增 `docker-compose.redis.yml` 可选 profile；CI 增加 redis service job；
  - README/production/runbook 补 Redis 部署与故障行为。
- 验证：158 SQLite tests；2 Redis 集成用例用 fakeredis 验证限流 429、指标聚合与文本输出（本地 Docker engine 不稳定，真实 Redis 由 CI service 验证）。
- 遗留：日志仍按实例采集；账号/RBAC 随后由 P1.3 完成，多实例负载均衡/SSE 超时仍未做。
- 提交：`2999f8e`。

### P1.3 账号 / RBAC / 配额 / 审计

- 目标：从“共享 token”升级到可区分用户、可限制额度、可追溯操作。
- 改动：
  - 新增 `auth.py`：users / api_tokens / thread_owners / usage_daily / audit_events 五张表；SQLite 与 Postgres 双后端；token 只存 SHA-256；
  - 新增 `admin.py`：create-user / issue-token / list-users / list-tokens / disable-user / disable-token / audit / stats；
  - `app/security.py`：静态 env token 保留为 admin；数据库 token 走账号；新增 `require_admin`；
  - `app/main.py`：研究请求消耗每日配额并登记 thread 归属；resume/report 校验 owner；`/metrics`、`/api/audit` 仅 admin；研究开始/结束/续跑写审计；
  - `maintenance.py backup` 纳入 `auth.db`；compose 数据卷增加 `AUTH_DB`；
  - 配额使用 SQLite/Postgres 原子 upsert（`ON CONFLICT ... WHERE ... RETURNING`），多实例并发下不会超发；
  - auth schema 初始化按数据库 URL 缓存，避免每个请求重复建表。
- 验证：164 SQLite tests + 6 集成（4 Postgres / 2 Redis）；RBAC 403/200、thread 归属、配额耗尽、token 禁用、审计、CLI 冒烟均通过；Postgres job 新增 auth 往返与配额测试。
- 遗留：账号是 token 型，没有浏览器登录/密码/找回；配额按自然日、失败 run 也计数；`require_auth` 每请求查一次 auth 库，用户量大时需缓存；SQLite auth 表不会自动迁移到 Postgres。
- 提交：`f97a57e`。

### P1.4 多副本 / 负载均衡 / 日志聚合

- 目标：从“单实例”推进到“多副本滚动发布”，同时保持 SSE 实时。
- 改动：
  - 新增 `docker-compose.prod.yml`：app 多副本（`--scale app=N`）、Caddy、nginx、Postgres、Redis；app 不发布宿主机端口；
  - Caddy：`health_uri /readyz`、`flush_interval -1`、JSON 访问日志；nginx：Docker DNS `resolver` + `upstream resolve`，多副本轮询与被动故障转移；
  - 新增 `/readyz`：检查 checkpoint/auth 数据库，Redis 故障标 degraded 但返回 200；
  - Dockerfile：uvicorn `--timeout-graceful-shutdown 120`；compose `stop_grace_period: 150s`；
  - 新增可选 `docker-compose.observability.yml`（Loki + Promtail + Grafana）与 `deploy/prometheus.example.yml`；
  - CI 增加 `compose` job，校验生产 compose、Caddyfile 与 nginx。
- 验证：166 SQLite tests（新增 2 个 `/readyz` 用例）；`caddy validate`、`nginx -t`、生产 compose config 通过；本地实际起 2 副本，nginx 将请求分发到两个 app；停掉 app-1 后 6 次请求仍全 200；扩到 3 副本后请求落到 3 个 app；Caddy 入口 `/readyz` 200。
- 决策记录：实测 Caddy 直接 `reverse_proxy app:8000` 只命中一个副本（8 次请求全到 app-2）；Traefik v3.5 在 Docker 29 上 Docker provider 报 400；最终用 nginx `resolver 127.0.0.11` + `upstream server app:8000 resolve`，实测 2/3 副本分发与停副本故障转移。
- 遗留：没有自动化压测/滚动发布、蓝绿/金丝雀；nginx 只有被动健康检查（无主动探测）；Loki 方案需要 Docker socket；没有 Prometheus/Grafana 自动拉起。
- 提交：`c497805`。

### P1.5 数据安全 / 供应链 / CSP

- 目标：把“能备份、能扫描、能挂 secret、CSP 无 inline”补齐，而不是只靠 TLS。
- 改动：
  - `maintenance.py`：Fernet 加密/解密、`gen-key`、`prune-backups`、加密恢复、Postgres `pg-backup`/`pg-restore`、`--rclone-remote` 仅允许加密备份；
  - `config.py`：敏感配置支持 `<NAME>_FILE`（Docker/K8s secret），覆盖 LLM/搜索/Langfuse/DB/Redis/静态 token/备份 key；
  - 前端：`index.html` 内联 CSS/JS 拆到 `app/static/app.css` / `app.js`，事件监听器外置；CSP 去掉 `unsafe-inline`，增加 `object-src`/`base-uri`/`form-action`/`frame-ancestors`；
  - Caddy `request_body max_size 64KB`、nginx `client_max_body_size 64k`；
  - CI 新增 `security` job（Trivy HIGH/CRITICAL，忽略无修复项）；新增 Dependabot（pip/Docker/Actions）；
  - 新增 `docs/security.md`，明确 WAF 应部署在 Cloudflare/云 WAF 层，仓库不内置 WAF。
- 验证：173 SQLite tests；加密备份/错误 key/恢复/密钥文件/过期清理；静态资源与无 inline 断言；`node --check`；本地 CLI 加密备份→恢复往返。
- 供应链验证：Trivy 首次扫描发现 5 个可修复 HIGH/CRITICAL（certifi、urllib3、jaraco.context、wheel），升级后重建镜像再扫退出码 0；setuptools 也升级以替换其 vendored 旧 wheel/jaraco；CI run #24 七个 job（含 security）全绿。
- 遗留：无 WAF/Bot 管理、无 token 过期、无镜像签名、无主机运行时检测。
- 提交：`a49fe66`。

### P1.6 证据支撑 / prompt injection

- 目标：引用编号正确之外，再给出“论断是否有来源支撑”的审计信号；同时降低网页内容劫持提示词的风险。
- 改动：
  - 新增 `untrusted.py`：外部网页用 `<untrusted_web_content source="...">` 包裹，中和同名闭合标签；researcher/writer/grounding judge 提示词声明“这是数据不是指令”；
  - 抓取来源新增 `excerpt`（搜索摘要/正文节选），随 note/source 持久化，供审计引用；
  - 新增 `grounding.py`：按带引用句子做词面覆盖率 + 数字一致性检查；`GROUNDING_JUDGE_MODEL` 非空时追加语义 judge（supported/partial/unsupported）；
  - writer 在引用校验后追加“证据支撑审计”章节，并写回 `grounding_audit` state；
  - eval 逐题记录 `grounding_claims / low_support / numeric_mismatch / semantic_unsupported`；
  - 新增配置 `GROUNDING_AUDIT_ENABLED / GROUNDING_JUDGE_MODEL / GROUNDING_MAX_CLAIMS / GROUNDING_LEXICAL_THRESHOLD`。
- 验证：180 SQLite tests；包裹/闭合标签中和、researcher 提示词边界、词面低覆盖/数字不一致、语义 judge stub、writer 集成均通过；CI run #26 七个 job 全绿。
- 遗留：没有真实模型在线的语义 judge 复评；词面覆盖不是事实裁决；没有内容安全分类器/审核策略；真实 kill/restart 与 A/B 仍未执行。
- 提交：`58b2191`。

### P1.7 真实模型 smoke / 内容安全钩子

- 目标：不再只用 mock；用真实 Ollama 跑一轮，暴露报告出口问题；补可选内容安全审计。
- 真实评测（`docs/eval-p17-smoke.md`）：1 题、2 子问题、1 轮、memory off，`qwen2.5:7b-instruct` 同时做研究/judge；faithfulness 4/5、citation_validity 1.0、source_utilization 0.75、37 引用/8 来源、6 次 LLM、6474/1493 tokens、195.1s、估价 $0.0034（本机实际 0）、grounding 7 句/1 低覆盖。
- 真实评测发现并修复：
  - 引用标记 `[1][4]` 被按句切开当成假论断 → 按行/段落提取，并剔除引用编号再比数字；
  - 模型自造 `## 参考来源` / `--- [n1]` 来源清单 → 统一剥离，只保留代码生成的一份；
  - 正文无 H1 时 `_strip_preamble` 从 `## 参考来源` 截断 → 只认 `# ` 一级标题。
- 内容安全：新增可选 `SAFETY_JUDGE_MODEL`，对报告输出 low/medium/high 风险审计；默认关闭，不自动拦截，不是合规分类器。
- 验证：182 SQLite tests；真实 smoke 结果与修复记录；CI 待本轮推送确认。
- 遗留：judge 与研究模型相同，不是独立强 judge；只跑 1 题；没有 kill/restart 真实演示；没有内容安全审核队列/自动拦截；没有全量 20 题和真实单变量 A/B。
- 提交：`8b4c2d2`。

### P1.8 全量 baseline / kill-restart

- 全量 20 题（`p17_full20_samejudge`，1 子问题 × 1 轮，memory off）：20/20 完成、0 报错；faithfulness 4.75、coverage 4.05、must_cover 0.5、citation_validity 1.0、source_utilization 0.752；均值 79.7s/题，p95 132.5s；8 次 fetch 降级。
- 真实 kill/restart：在 `review_brief` 暂停处强杀 CLI，再 `cli.py --resume` 完成；thread `r-20261029-175219-e0c2f2`，4 次调用、4982+863 tokens、54s，报告落盘。
- 修复：CLI 进度打印对 `None`/非 dict 节点输出崩溃；observability context manager 在异常时二次 `yield` 掩盖原始异常。
- 限制：judge 与研究模型同为 `qwen2.5:7b-instruct`，不是独立强 judge；1×1 规模低于默认；没有独立 judge 的 20 题复评。
- 验证：183 SQLite tests；真实 20 题与 kill/restart 记录见 `docs/eval-p17-smoke.md`。
- 提交：`451c818`。

### 评测集 v2（分层扩充）

- 数据集从 20 题扩到 **110 题**：80 core（10 smoke / 50 regression / 10 holdout）+ 30 adversarial。
- `run_eval.py` 增加 `--suite all|smoke|core|regression|adversarial|holdout`；`--limit/--offset` 在子集过滤后生效；summary 记录 suite。
- 新增题型覆盖：后端/数据库/消息队列/容量规划/数据工程/可观测性/成本估算/信息不足/争议分析/非技术沟通/时效性；对抗集覆盖提示词注入、隐私、伪造引用、错误前提、SSRF/XSS、越界引用等。
- 验证：185 SQLite tests；数据集唯一性/分层计数/筛选测试；文档 `docs/eval-dataset.md`。
- 遗留：110 题仍不是生产真理；同 judge 只能做回归；adversarial 的期望行为需要独立安全 judge/人工复核；holdout 不应参与调参。
- 提交：`48c2e51`。

### Command Code 独立 judge（P1.8 延伸）

- 新增 `eval/commandcode_judge.py`：通过 `cmdc` headless + stdin 调独立模型，解析 NDJSON finalText 的 JSON。
- 配置：`JUDGE_PROVIDER=commandcode`、`JUDGE_MODEL`、`JUDGE_EFFORT`、可选 `COMMAND_CODE_CLI`；key 只从 `COMMAND_CODE_API_KEY` 环境变量继承。
- 单题抽样：baseline q01 同模型 qwen 给 5/5/5（但正文 0 引用），`deepseek/deepseek-v4-pro` 给 4/4/2，证明同 judge 偏差。
- 验证：188 SQLite tests；适配器 fake subprocess 测试；真实 key 最小调用解析通过；key 未写入仓库。
- 限制：`cmdc` 每题启动一次，较慢；需要 Go+ 套餐/API key；仍不是 110 题独立复评。

## 3. 路线状态

| 阶段 | 状态 | 说明 |
|---|---|---|
| A 基线/稳定 | 完成 | A1 |
| B 正确性/隔离 | 完成 | B1 |
| C 数据可靠性 | 完成 | C1 |
| D 安全与运维 | 完成 | D1 应用层安全 + E2 日志/健康检查 + D3 指标/非 root |
| E 部署与交付 | 完成 | E1 依赖锁/CI/Docker；E2 日志与 runbook |
| F 性能与质量 | 部分完成 | R10 引用校验 + F1 分位数/单变量工具；真实单变量批次待模型在线 |
| P0 单实例公网生产化 | 完成 | 多 token、Prometheus、CI runtime、Caddy/生产文档 |
| P1.1 Postgres 存储 | 完成 | checkpoint/memory 可切 Postgres；SQLite fallback；迁移命令 |
| P1.2 Redis 共享状态 | 完成 | 共享限流/指标；Redis 故障回退 |
| P1.3 账号/RBAC/配额/审计 | 完成 | 数据库 token、user/admin、thread 归属、每日配额、审计 |
| P1.4 多副本/负载均衡/日志 | 完成 | Caddy + nginx DNS 发现、/readyz、SSE 不缓冲、120s 优雅退出、可选 Loki |
| P1.5 数据安全/供应链/CSP | 完成 | 加密备份、secret 文件、Trivy/Dependabot、CSP 去 inline、边缘请求体限制 |
| P1.6 证据支撑/prompt injection | 完成 | 不可信内容包裹、词面/数字审计、可选语义 judge |
| P1.7 真实 smoke/内容安全钩子 | 部分完成 | 1 题真实 smoke、报告 bug 修复、可选安全审计 |
| P1.8 全量 baseline/kill-restart | 部分完成 | 20 题同 judge、断点续跑完成；Command Code judge 已接入，110 题独立复评未跑 |
| 评测集 v2 | 完成 | 110 题分层：80 core + 30 adversarial；suite 过滤 |
| Command Code 独立 judge | 完成（单题抽样） | `JUDGE_PROVIDER=commandcode`；Go 模型 headless |

工程化 A–F + P0 基线已闭环。剩余未完成项分为两类：

**需要真实模型/外部环境验证，不伪造数字：**
- F1 真实单变量 A/B（Ollama 在线时跑）；
- 独立强 judge 复评全量 20 题；
- 真实 kill/restart 的 `--resume` 演示。

**P1.6 AI 质量/内容安全（当前 P1.5 明确不覆盖）：**
- 语义级引用核验（论断是否被来源支持）；
- prompt injection / 内容安全 / 输出政策；
- 真实模型在线时的单变量 A/B、强 judge 复评、kill/restart 演示；
- WAF/Bot 管理、镜像签名、值班/SLO。

## 4. 部署检查清单（公网）

- [x] 依赖锁定 + CI + Docker 镜像构建（E1）
- [x] writer 出口引用/链接校验（R10）
- [x] 结构化日志 + 请求 ID + 升级/回滚 runbook（E2）
- [x] 分位数/单变量评测工具 + 历史批次重算（F1）
- [x] `/metrics` 运行指标 + 非 root 容器（D3）
- [x] 多 token + Prometheus + CI runtime + Caddy/生产文档（P0）
- [x] Postgres checkpoint/memory + 迁移 + CI postgres job（P1.1）
- [x] Redis 共享限流/指标 + CI redis job（P1.2）
- [x] 账号/API token/RBAC/每日配额/审计（P1.3）
- [x] 多副本 compose + Caddy/nginx 负载均衡 + `/readyz` + 日志聚合蓝图（P1.4）
- [x] 加密备份 + secret 文件 + Trivy/Dependabot + CSP 去 inline（P1.5）
- [x] 证据支撑审计 + 网页不可信包裹（P1.6）
- [x] 真实模型 1 题 smoke + 可选内容安全审计（P1.7）
- [x] 20 题同 judge baseline + kill/restart 续跑（P1.8）
- [x] 评测集扩到 110 题并支持 suite 分层
- [x] Command Code 独立 judge 适配器（单题抽样验证）
- [ ] `.env` 设置足够长的随机 `APP_AUTH_TOKEN`（admin bootstrap）或创建管理员账号
- [ ] 8000 端口不直接暴露公网；放在反向代理/TLS 后面
- [ ] 反向代理与 app 同机 loopback 时也必须配 token
- [ ] `docker compose` 只发布 `127.0.0.1`；确认 Langfuse 管理面不对外
- [ ] `./data` 卷持久化 `checkpoints.db` / `memory.db`
- [ ] 定期执行 `python maintenance.py prune-checkpoints --days 7`
- [ ] 定期执行 `python maintenance.py backup --out backups`，并验证可恢复
- [ ] 备份目录权限限制（可能含研究内容）
- [ ] 监控 `/healthz`；容器 healthcheck 已配置
- [ ] 多 worker 部署时把限流换成 Redis 等共享实现
- [ ] 反向代理限制请求体大小（chunked 请求）
- [ ] 生产 judge/研究模型与 key 分离；评测用更强模型复评

## 5. 已知限制

- SQLite 单文件、单写者；高并发/多 worker 需 PostgreSQL 或外部存储。
- checkpoint 清理依赖 msgpack 里的 `ts`；格式变化时保守跳过，不会误删。
- 备份未加密；`restore` 需要停服务。
- 限流为单进程固定窗口，重启清零。
- SSRF 校验与实际连接之间仍有 DNS rebinding 残余窗口。
- 引用后校验只做确定性剔除，不判断论断与来源的语义相关性；无引用/少引用的报告仍可能质量不足。
- LLM judge 与研究共用同一模型时存在系统性偏差；对外数字需更强模型复评。
- 评测 A/B 曾同时调整 4 个参数，0.82→1.00 不能单独归因于“代码生成引用编号”（baseline 已有注册表）；README 已修正口径。
- 指标为进程级 JSON，未接 Prometheus；多 worker 下计数/限流各自独立；真实单变量 A/B 与独立强 judge 待模型在线时补。

## 6. 复现与验证

```bash
# 测试
python -m pytest tests -q

# 配置自检 / 服务
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/healthz

# 数据维护
python maintenance.py prune-checkpoints --days 7
python maintenance.py backup --out backups
python maintenance.py restore backups/checkpoints-YYYYmmdd-HHMMSS-ffffff.db --target checkpoints.db --yes
```

## 7. 关键决策记录

1. **D1 放在 C1 之前**：公网服务一旦暴露，未认证/CSRF/XSS/SSRF 的风险高于数据维护；先把入口守住。
2. **POST + Bearer 而不是 cookie session**：浏览器不会跨站自动带 Authorization header，直接消除 CSRF 面；前端改用 fetch + ReadableStream 消费 SSE。
3. **run_context 按 run_id 隔离**：比把 usage 塞进 LangGraph state 改动小，又能覆盖并行 researcher 线程；节点入口绑定，避免依赖线程继承 context。
4. **SSE 生成器用 worker 线程 + 队列**：sync 生成器可能被 Starlette 分多次 `next()` 推进，ContextVar token 不能跨 context reset；worker 内 context 稳定，也保住 Langfuse trace。
5. **WAL + sqlite backup API**：直接拷贝正在写入的 `.db` 会漏 WAL；backup API 能拿到一致快照。
6. **R3 用“剩余墙钟均摊”而不是强杀线程**：Python 线程无法安全取消阻塞 IO；把 timeout 压到剩余预算内是最小可靠方案。
7. **不引入向量库/消息队列/微服务**：当前瓶颈是隔离、安全、运维，不是吞吐；先不增加运维面。

## 8. 简历/面试口径

- 可主张：多 Agent 并行、上下文隔离、20 题评测、引用确定性校验、checkpoint 续跑、预算/熔断/压缩/记忆分层、并发隔离、认证/XSS/SSRF、WAL/备份恢复。
- 需限定：
  - 0.82→1.00 来自同批调整的 4 个参数（strict grounding、笔记长度、正文长度），不是单一“代码生成编号”；
  - LLM judge 的绝对分数不可靠（baseline q03 零引用仍被 judge 给 5 分）；
  - “断点续跑”已有代码与单测，但应补一次真实 kill/restart 演示；
  - 公网部署仍需反向代理/TLS、token、备份与监控，当前是“可上线的最小边界”，不是完整 SaaS。
