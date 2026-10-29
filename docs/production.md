# 生产化落地说明（P0 + P1：单实例/多副本公网服务 / Postgres + Redis + 账号/RBAC）

本文定义当前项目“向真实生产过渡”的目标形态、已具备能力和仍未覆盖的部分。
**当前目标是小规模多副本、低并发的公网服务，不是多租户 SaaS。**

## 1. 目标拓扑

```text
Internet
  │ 80/443
  ▼
Caddy（TLS 自动证书、HSTS、CSP、SSE 不缓冲）
  │ 反向代理
  ▼
app 容器（uvicorn，非 root UID 10001）
  ├─ DATABASE_URL 存在 → Postgres（checkpoint + memory + auth）
  ├─ 否则 → ./data/checkpoints.db + memory.db + auth.db（SQLite WAL）
  ├─ REDIS_URL 存在 → 共享限流 + 指标计数
  └─ Langfuse（可选，自托管或 Cloud）
```

- app 只发布到 `127.0.0.1:8000`，不直接对公网暴露。
- Caddy 负责 80/443、TLS、HSTS、基础安全响应头。
- 当前不引入消息队列；限流与指标可切 Redis，多实例共享。

## 2. 部署前提

- 一台有公网 IP 的 Linux 主机（2C4G 起步），Docker + Compose；
- 域名 A 记录指向主机；
- 模型与搜索凭据：`LLM_*`、`SEARCH_*`；
- 一个足够长的随机 `APP_AUTH_TOKEN`（admin，用于 bootstrap/监控），或用 `admin.py` 创建账号并签发 token；
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
- 多实例时 checkpoint/memory 已共享；限流与 metrics 可再开 Redis profile，见 2.2 节。

## 2.2 Redis 共享状态（P1.2）

```bash
docker compose -f docker-compose.yml \
  -f docker-compose.postgres.yml \
  -f docker-compose.redis.yml up -d
```

- `REDIS_URL` 启用 Redis 固定窗口限流（按 token/IP 分桶）与跨实例指标；
- `/metrics` 从 Redis 汇总，Prometheus 抓任一实例都是全局计数；
- Redis 故障时自动回退进程内限流/指标并写 warning；
- Redis 只存限流窗口与指标计数，不持久化研究数据；
- 多副本拓扑见 2.4 节。

## 2.3 账号 / RBAC / 配额 / 审计（P1.3）

```bash
# 容器内或宿主机（AUTH_DB 指向 /app/data/auth.db）
python admin.py create-user --username alice --role user --daily-quota 20
python admin.py issue-token --username alice --name laptop   # 明文只显示一次
python admin.py list-users
python admin.py audit --limit 50
```

- 服务端只存 token SHA-256；用户用 `Authorization: Bearer <token>`。
- `user`：只能访问自己发起的 thread，每日研究配额；`admin`：跨 thread、`/metrics`、`/api/audit`。
- 研究开始消耗一次配额，续跑不重复消耗；审计记录 `research_start` / `research_finished` / `research_resume` 与 CLI 管理动作。
- `AUTH_DB` 默认 `auth.db`；设置 `DATABASE_URL` 后 auth 表也在 Postgres。
- 静态 `APP_AUTH_TOKEN(S)` 仍兼容，视为 admin；监控抓取 `/metrics` 请使用 admin token。

## 2.4 多副本 / 负载均衡（P1.4）

```bash
export APP_DOMAIN=research.example.com
export POSTGRES_PASSWORD='<random>'
export REDIS_PASSWORD='<random>'
docker compose -f docker-compose.prod.yml up -d --build --scale app=2
```

拓扑：

```
Caddy :80/:443（TLS、安全头、SSE flush）
  │  reverse_proxy nginx:80
  ▼
nginx :80（Docker DNS resolver + upstream resolve）
  ├─ app replica 1 :8000
  └─ app replica 2 :8000
        ├─ Postgres（checkpoint/memory/auth）
        └─ Redis（限流/指标）
```

- nginx `resolver 127.0.0.11 valid=5s` + `upstream server app:8000 resolve`，扩缩容后自动发现副本；
- Caddy `flush_interval -1` 保证 SSE 实时；`health_uri /readyz` 检查 nginx/后端链路；
- uvicorn 120s graceful shutdown，compose `stop_grace_period: 150s`；
- 研究流绑定单个副本，续跑从共享 Postgres checkpoint 恢复，不需要 sticky session；
- app 不发布宿主机端口；Postgres/Redis 也不对外；
- 扩缩容后若 nginx 未发现新副本，检查 `resolver`/重启 nginx；
- 可选日志：`docker-compose.observability.yml`（Loki/Promtail/Grafana）；Promtail 需 Docker socket，仅受信主机。

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

- **宿主机运行**：执行
  `APP_DOMAIN=research.example.com UPSTREAM=127.0.0.1:8000 caddy run --config deploy/Caddyfile`；
- **compose 内运行**：用 `docker-compose.prod.yml`，Caddy 设置 `UPSTREAM=nginx:80`，由 nginx 发现 app 副本；映射 80/443 与证书卷。

验证：

```bash
curl -s https://research.example.com/healthz
curl -s -H "Authorization: Bearer $APP_AUTH_TOKEN" \
  https://research.example.com/metrics | head
```

## 4. 安全基线

- `APP_AUTH_TOKEN` 作为 admin bootstrap/监控 token；面向用户请用 `admin.py` 创建账号并签发 token。
- 多用户按 `user_id` 做 thread 归属与每日配额；`/metrics`、`/api/audit` 仅 admin。
- 所有研究/续跑/报告/metrics 接口都走 Bearer；`/healthz` 公开。
- 报告 HTML 已做白名单清洗；抓取侧已做 SSRF 防护；网页正文用不可信标签隔离并在提示词声明边界。
- 报告出口做引用句词面覆盖/数字一致性审计；`GROUNDING_JUDGE_MODEL` 可选语义 judge（每轮最多 `GROUNDING_MAX_CLAIMS` 条，额外成本）。
- 请求体 64KB（Caddy `request_body` + nginx `client_max_body_size` + app）、主题 500 字、审核内容 5000 字上限；每 token/IP 每分钟限流。
- Caddy 加 HSTS、CSP、`X-Frame-Options: DENY`、`nosniff`、`Referrer-Policy`；CSS/JS 已外置，CSP 不含 `unsafe-inline`。
- 敏感配置优先读 `<NAME>_FILE`（Docker/K8s secret）；不要把 secret 写入 `.env`/镜像。
- 镜像由 CI Trivy 扫描 HIGH/CRITICAL；Dependabot 每周更新依赖。
- **WAF 不在仓库内**：公网必须在 Cloudflare/云 WAF 层加托管规则、Bot 管理和源站隐藏，详见 `docs/security.md`。
- 密钥轮换见 `docs/runbook.md`；轮换后旧 token 立即失效。
- 不要把 `.env`、`data/`、`backups/` 提交到 Git；备份目录按敏感数据管理。

## 5. 数据与备份

### SQLite 模式

- 数据文件：`data/checkpoints.db`、`data/memory.db`、`data/auth.db`（WAL）。
- 每日备份（SQLite 模式包含 `checkpoints.db`、`memory.db`、`auth.db`）：

```bash
export BACKUP_ENCRYPTION_KEY='<Fernet key>'
python maintenance.py backup --encrypt --out backups
python maintenance.py backup --encrypt --rclone-remote remote:deep-research-backups
```

- 备份包含 WAL 内容；**不要直接 cp 正在写入的 .db**。
- 加密成功后会删除明文；`--rclone-remote` 只能与 `--encrypt` 一起使用。
- 每周执行 `prune-checkpoints --days 7` 与 `prune-backups --days 30`；恢复需停服，`restore` 会先做安全备份。
- 恢复加密备份：`restore xxx.db.fernet --target ... --key-file ... --yes`。

### Postgres 模式

```bash
# 备份 + 加密 + 可选 rclone 异地
python maintenance.py pg-backup --encrypt --out backups
python maintenance.py pg-backup --encrypt --rclone-remote remote:deep-research-backups

# 恢复（先停 app；会覆盖目标库）
python maintenance.py pg-restore backups/postgres-....dump.fernet \
  --database-url "$DATABASE_URL" --key-file backup.key --yes
```

- 备份使用 `pg_dump --format=custom`；`pg-restore` 会 `--clean --if-exists`，必须显式 `--yes`。
- Postgres checkpoint 的保留策略尚未内置；上线后需按 `checkpoints` 表时间字段制定清理任务。
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

当前已完成：

1. LangGraph checkpointer 换 Postgres；
2. memory 表迁移到 Postgres（含 `migrate-memory`）；
3. 限流换 Redis；
4. 指标计数接 Redis（日志仍按实例采集）；
5. 账号/API token/RBAC/每日配额/审计（auth 表跟随 Postgres）；
6. 多副本负载均衡、`/readyz` 健康检查、SSE 不缓冲、120s 优雅退出；
7. 可选 Loki/Promtail/Grafana 日志聚合。

仍需：

8. 加密备份/异地容灾、镜像扫描、WAF、值班/SLO；
9. 语义级引用核验与提示词注入防护；
10. 真实模型在线时的单变量 A/B、强 judge 复评与 kill/restart 演示。

## 8. 发布与回滚

发布门禁：

```bash
python -m pytest tests -q
git status --short          # 必须干净
docker build -t deep-research-agent:<tag> .
```

GitHub Actions 必须通过 `test`、`docker`、`runtime`、`postgres`、`redis`、`security`、`compose` 七个 job（runtime 实际启动容器、检查 `/healthz` 与 UID 10001；postgres/redis 跑集成测试；security 用 Trivy 扫 HIGH/CRITICAL；compose 校验生产 compose、Caddyfile 与 nginx）。

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
