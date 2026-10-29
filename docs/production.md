# 生产化落地说明（P0 + P1.1：单实例公网服务 / Postgres 存储）

本文定义当前项目“向真实生产过渡”的目标形态、已具备能力和仍未覆盖的部分。
**当前目标是单实例或少量实例、低并发的公网服务，不是多租户 SaaS。**

## 1. 目标拓扑

```text
Internet
  │ 80/443
  ▼
Caddy（TLS 自动证书、HSTS、CSP、SSE 不缓冲）
  │ 反向代理
  ▼
app 容器（uvicorn，非 root UID 10001）
  ├─ DATABASE_URL 存在 → Postgres（checkpoint + memory）
  ├─ 否则 → ./data/checkpoints.db + ./data/memory.db（SQLite WAL）
  └─ Langfuse（可选，自托管或 Cloud）
```

- app 只发布到 `127.0.0.1:8000`，不直接对公网暴露。
- Caddy 负责 80/443、TLS、HSTS、基础安全响应头。
- 当前不引入 Redis/消息队列；限流与进程指标仍按实例独立。

## 2. 部署前提

- 一台有公网 IP 的 Linux 主机（2C4G 起步），Docker + Compose；
- 域名 A 记录指向主机；
- 模型与搜索凭据：`LLM_*`、`SEARCH_*`；
- 一个足够长的随机 `APP_AUTH_TOKEN`（或用 `APP_AUTH_TOKENS` 给多个客户端分发）；
- 80/443 对外放行，8000 不对公网放行。

## 2.1 Postgres 后端（P1.1）

设置 `DATABASE_URL` 后，checkpoint 与 memory 都走 Postgres：

```bash
# 本地 compose 起一个 Postgres（生产建议用外部托管库）
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d
```

- 首次连接自动建 `research_memory`、`memory_schema` 和 LangGraph checkpoint 表；
- 旧 SQLite 研究档案可迁移：
  `python maintenance.py migrate-memory --from memory.db --database-url <url>`；
- **旧 checkpoint 不迁移**：切换前让在途研究完成，或接受旧 thread 不能续跑；
- `maintenance.py backup/prune/restore` 仅维护 SQLite；Postgres 使用 `pg_dump` / `pg_restore`；
- 多实例时 checkpoint/memory 已共享；限流、metrics、日志仍需 Redis/共享后端，见第 7 节。

## 3. 首次部署

```bash
git clone <repo> && cd deep-research-agent
cp .env.example .env
# 必填：LLM_*、SEARCH_*、APP_AUTH_TOKEN
python -c "import secrets; print(secrets.token_urlsafe(32))"

# 数据目录给容器内 UID 10001 写权限
mkdir -p data && sudo chown -R 10001:10001 data

# 只启动 app（Langfuse 可选）
docker compose up -d --build app

# 本机健康检查
curl -s http://127.0.0.1:8000/healthz
```

Caddy 二选一：

- **宿主机运行**：修改 `deploy/Caddyfile` 的 `app:8000` 为 `127.0.0.1:8000`，执行
  `APP_DOMAIN=research.example.com caddy run --config deploy/Caddyfile`；
- **compose 内运行**：把 Caddy 作为同网络服务，保留 `app:8000`，映射 80/443 与证书卷。

验证：

```bash
curl -s https://research.example.com/healthz
curl -s -H "Authorization: Bearer $APP_AUTH_TOKEN" \
  https://research.example.com/metrics | head
```

## 4. 安全基线

- `APP_AUTH_TOKEN` 必填；更细粒度用 `APP_AUTH_TOKENS`（逗号分隔），限流按 token 分桶。
- 所有研究/续跑/报告/metrics 接口都走 Bearer；`/healthz` 公开。
- 报告 HTML 已做白名单清洗；抓取侧已做 SSRF 防护。
- 请求体 64KB、主题 500 字、审核内容 5000 字上限；每 IP/ token 每分钟限流。
- Caddy 加 HSTS、CSP、`X-Frame-Options: DENY`、`nosniff`、`Referrer-Policy`。
- 密钥轮换见 `docs/runbook.md`；轮换后旧 token 立即失效。
- 不要把 `.env`、`data/`、`backups/` 提交到 Git；备份目录按敏感数据管理。

## 5. 数据与备份

### SQLite 模式

- 数据文件：`data/checkpoints.db`、`data/memory.db`（WAL）。
- 每日备份：

```bash
python maintenance.py backup --out backups
```

- 备份包含 WAL 内容；**不要直接 cp 正在写入的 .db**。
- 每周执行 `prune-checkpoints --days 7`；恢复需停服，`restore` 会先做安全备份。

### Postgres 模式

```bash
# 备份
pg_dump -Fc "$DATABASE_URL" > backups/deep_research-$(date +%F).dump

# 恢复（先停 app）
pg_restore --clean --if-exists -d "$DATABASE_URL" backups/deep_research-2026-10-29.dump
```

- 备份加密后传到对象存储/另一台机器；当前脚本只做本地备份。
- Postgres checkpoint 的保留策略尚未内置（P1.2）；上线后需按 `checkpoints` 表时间字段制定清理任务。
- 每季度做恢复演练：停服 → 恢复 → `/healthz` → 小规模研究。

## 6. 观测与告警

- `/healthz`：容器 healthcheck + 外部探活；
- `/metrics`：Prometheus 文本（请求计数、research started/finished/failed/active）；
- `/metrics.json`：人工排查用 JSON；
- 日志：JSON 行，按 `request_id` / `thread_id` / `level` 聚合；
- Langfuse（可选）：每轮 trace、LLM token/延迟；
- 建议告警：
  - healthz 连续失败；
  - `dr_research_finished_total{outcome="failed"}` 增速；
  - 429/401 突增；
  - 磁盘使用率 > 80%、备份超过 24h 未更新。

## 7. 容量与扩展触发条件

当前单实例假设：

- 同时进行的研究请求建议 ≤ 2~3 个（每个请求会占 worker 线程并调用外部 LLM/搜索）；
- 日研究量按搜索 API 额度与模型成本控制；
- SSE 长连接由 Caddy 反代，需保持 `flush_interval -1`。

出现以下情况时应进入多实例阶段：

- 需要 2 个以上 app 副本或滚动发布；
- 限流/指标需要跨实例一致；
- 需要按用户配额、审计与 RBAC。

当前 P1.1 已完成：

1. LangGraph checkpointer 换 Postgres（`langgraph-checkpoint-postgres`）；
2. memory 表迁移到 Postgres（含 `migrate-memory`）。

仍需（P1.2）：

3. 限流换 Redis；
4. 指标/日志接共享后端；
5. 负载均衡 + SSE 超时配置 + 共享会话/数据；
6. 多租户账号/RBAC/配额。

## 8. 发布与回滚

发布门禁：

```bash
python -m pytest tests -q
git status --short          # 必须干净
docker build -t deep-research-agent:<tag> .
```

GitHub Actions 必须通过 `test`、`docker`、`runtime`、`postgres` 四个 job（runtime 实际启动容器、检查 `/healthz` 与 UID 10001；postgres 跑存储集成测试）。

发布步骤：

1. `python maintenance.py backup --out backups`
2. `git checkout <tag>`
3. `docker compose build app && docker compose up -d app`
4. `curl /healthz` + 一条小规模研究
5. 观察 15 分钟日志/指标

回滚：

1. `git checkout <previous-tag>`
2. `docker compose build app && docker compose up -d app`
3. 如涉及数据变更，按备份 restore
4. 验证 `/healthz` 与一次最小研究

## 9. 当前明确不覆盖

- 多租户账号、RBAC、按用户配额/计费；
- 多实例/水平扩展、Postgres/Redis；
- 语义级“论断是否被来源支持”的自动核验；
- 提示词注入/内容安全的专门防护；
- 加密备份、异地容灾、镜像漏洞扫描、WAF；
- 值班/SLO/容量规划等组织流程。

以上未完成项不应在对外材料中描述为“已具备”。
