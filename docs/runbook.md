# 运行手册（E2）

适用对象：负责部署、升级、排障的维护者。命令默认在仓库根目录执行。

## 0. 关键约定

- 进程：单进程 uvicorn（SQLite 单写者；不要直接开多 worker）。
- 入口：`POST /api/research/stream`、`POST /api/resume/stream`、`GET /api/report/{thread_id}`。
- 鉴权：`Authorization: Bearer <token>`；静态 `APP_AUTH_TOKEN(S)` 为 admin，数据库账号 token 分 user/admin；user 只能访问自己的 thread 并受每日配额约束。
- 日志：默认 JSON 行到 stdout，关键字段 `request_id` / `thread_id` / `level` / `message`。
- 数据：默认 `checkpoints.db`（断点）、`memory.db`（研究档案）、`auth.db`（账号/token/审计）；设置 `DATABASE_URL` 后都在 Postgres。
- 共享状态：设置 `REDIS_URL` 后限流与指标计数在 Redis，多实例共享。
- 健康检查：`GET /healthz`（200/503，只读本地配置，不访问外部依赖）。
- 运行指标：`GET /metrics`（请求计数 + research 状态；仅 admin，静态 admin token 或 admin 账号 token）。
- 容器用户：`appuser`（UID 10001），非 root。

## 1. 发布前检查

```bash
python -m pytest tests -q            # 必须全绿
git status --short                   # 必须干净
docker build -t deep-research-agent:$(git rev-parse --short HEAD) .
```

- CI（`.github/workflows/ci.yml`）必须通过 `test` 与 `docker` 两个 job。
- 生产 `.env` 必须设置：`APP_AUTH_TOKEN`、LLM key、搜索 key。
- 升级前先备份：

```bash
python maintenance.py backup --out backups
```

## 2. 启动与健康检查

```bash
# 本机开发
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# Docker（本地也必须设置 APP_AUTH_TOKEN：端口映射后客户端不是回环）
docker compose up -d
curl -s http://127.0.0.1:8000/healthz
```

期望：`{"status":"ok",...}`。503 表示配置自检失败，`errors` 会列出具体变量。

Linux 主机挂载 `./data` 时，先确保 UID 10001 可写：

```bash
mkdir -p data
sudo chown -R 10001:10001 data
```

## 3. 升级

```bash
# 1) 停写/备份
python maintenance.py backup --out backups

# 2) 取新版本
git fetch origin
git checkout <new-tag-or-commit>

# 3) 构建并切换
docker compose build app
docker compose up -d app

# 4) 验证
curl -s http://127.0.0.1:8000/healthz
docker compose logs --tail=50 app
```

- 先看 release 是否包含数据库变更；当前只有 SQLite `PRAGMA user_version`，升级前备份即可。
- 升级后若 `/healthz` 503，先看 `errors`，不要反复重启。

## 4. 回滚

```bash
# 代码回滚
git checkout <previous-tag-or-commit>
docker compose build app
docker compose up -d app
curl -s http://127.0.0.1:8000/healthz
```

- 如果新版本改过数据库结构，按备份恢复：

```bash
python maintenance.py restore backups/<snapshot>.db --target memory.db --yes
python maintenance.py restore backups/<snapshot>.db --target checkpoints.db --yes
```

- `restore` 会先给当前库做安全备份；仍需先停服务。

## 5. 备份与恢复

```bash
# 备份（checkpoint + memory + auth）
python maintenance.py backup --out backups

# 清理过期 checkpoint（0=不清理）
python maintenance.py prune-checkpoints --days 7

# 恢复（必须显式 --yes）
python maintenance.py restore backups/checkpoints-YYYYmmdd-HHMMSS-ffffff.db \
  --target checkpoints.db --yes
```

- 备份使用 sqlite backup API，包含 WAL；不要直接 `cp` 正在写入的 `.db`。
- `backups/` 可能含研究内容，限制目录权限；生产建议加密存储。
- 建议 cron / Task Scheduler 每天备份、每周清理。

### Postgres 模式

```bash
# 备份/恢复
pg_dump -Fc "$DATABASE_URL" > backups/deep_research-$(date +%F).dump
pg_restore --clean --if-exists -d "$DATABASE_URL" backups/deep_research-2026-10-29.dump

# SQLite 研究档案迁移到 Postgres（旧 checkpoint 不迁移，先让在途任务完成）
python maintenance.py migrate-memory --from memory.db --database-url "$DATABASE_URL"
```

- `maintenance.py backup/prune/restore` 只维护 SQLite；`DATABASE_URL` 启用时会显式拒绝。
- Postgres checkpoint 保留策略尚未内置，需按 `checkpoints` 表时间制定清理。
- 切换存储前先停服务、备份 SQLite 与 Postgres 两侧。

### Redis 模式

```bash
# 与 Postgres 一起启动
docker compose -f docker-compose.yml -f docker-compose.postgres.yml -f docker-compose.redis.yml up -d
```

- 限流 key：`dr:rate:<分钟窗口>:<token/IP>`，自然过期；不需要人工清理。
- 指标 key：`dr:metrics:*`，只存计数，可从零重建。
- Redis 故障时自动回退进程内限流/指标并写 `redis_*_failed` warning；
  恢复后 30 秒内自动重试。
- 不要把 Redis 当业务数据库；研究数据仍在 SQLite/Postgres。

## 6. 密钥轮换

### 访问令牌

```bash
# 账号 token：签发新 token，确认客户端切换后禁用旧 token
python admin.py issue-token --username alice --name laptop-2026q4
python admin.py list-tokens --username alice
python admin.py disable-token --id 3

# 静态 admin token：生成新值后更新 .env 的 APP_AUTH_TOKEN 并重启
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

- 数据库 token 不需要重启，`disable-token` 立即生效；浏览器端需要重新输入新 token。
- 静态 token 轮换后旧值立即失效，需重启并重新输入。
- 用户离职：`disable-user` 会同时让该用户所有 token 失效（认证 join 检查 users.active）。
- 反向代理若也做鉴权，先更新代理配置再更新应用，避免中间窗口全断。

### LLM / 搜索 / Langfuse key

1. 在对应控制台创建新 key；
2. 更新 `.env`；
3. 重启服务；
4. 用 `/healthz` + 一次小规模研究验证；
5. 撤销旧 key。

## 7. 故障排查

### `/healthz` 503

- 看 `errors`：通常是 `LLM_BASE_URL/LLM_MODEL` 为空、远程 LLM 无 key、Tavily key 为空、数值非法。
- 本地 Ollama：确认 `LLM_BASE_URL=http://localhost:11434/v1`、`LLM_NATIVE_OLLAMA=1`，`ollama serve` 已启动。

### 401 / 403

- 401：token 缺失/错误，或账号 token 已禁用。检查 `Authorization: Bearer <token>` 与 `admin.py list-tokens`。
- 403：无权限（user 访问别人 thread、非 admin 访问 metrics/audit），或未配置 token 且请求不是本机回环。
- 429：限流或每日研究配额用尽；配额可在 `admin.py list-users` 查看，次日重置。
- 日志字段：`event=auth_failed` / `auth_denied_no_token`，含 `client` 与 `path`。

### 429

- 每 IP 每分钟研究/续跑次数超限；看响应 `Retry-After`。
- 限流是单进程内存实现；多 worker 需换 Redis/网关限流。

### SSE 流报错/中断

- 响应事件含 `thread_id` 与 `request_id`；用它们在同一行日志里检索：

```bash
docker compose logs app | grep '"request_id": "..."'
docker compose logs app | grep '"thread_id": "..."'
```

- `research_failed` 日志带完整堆栈；检查 LLM/搜索 key、网络、预算。
- 中断后可用同一 `thread_id` 调 `/api/resume/stream` 续跑。

### `database is locked`

- 已启用 WAL + `busy_timeout=30s`；仍出现时通常是长事务/外部进程占用。
- 停服务 → 备份 → 用 `python maintenance.py prune-checkpoints` 清理旧 thread（可选）→ 重启。
- 不要直接删除 `-wal` / `-shm` 文件。

### checkpoint 损坏

```bash
python maintenance.py restore backups/<last-good>.db --target checkpoints.db --yes
```

- 恢复后未完成的 thread 可能无法续跑，重新发起即可；memory 不受影响。

### LLM 超时 / 预算耗尽

- 单次调用 timeout 会被剩余墙钟/尝试次数压低；报告会附「研究受限说明」。
- 调大 `MAX_WALL_CLOCK_S` 或 `LLM_CALL_TIMEOUT_S`；检查模型服务是否卡住。
- 本地 7B 模型长提示词可能慢；可缩小 `RESEARCH_PAGE_CHARS` / `NOTE_MAX_CHARS`。

### 搜索结果全空

- 看日志 `search` 降级事件；确认 Tavily 额度/网络。
- 熔断后会在冷却期内短路；`BREAKER_COOLDOWN_S` 后可恢复。
- 报告仍会生成，但会标注研究受限。

## 8. 监控建议

- 容器 healthcheck + `/healthz` 探活；
- `GET /metrics` 输出 Prometheus 文本（请求计数 + research started/finished/failed/active）；
  配置 `REDIS_URL` 时从 Redis 汇总，多实例一致；抓取需 admin token：
  `curl -H "Authorization: Bearer $APP_AUTH_TOKEN" http://127.0.0.1:8000/metrics`
- `GET /metrics.json` 输出 JSON 快照，便于人工排查。
- 日志按 `level=ERROR` 与 `request_id/thread_id` 聚合；
- Langfuse（可选）看每轮 trace、token、延迟；
- 磁盘：`checkpoints.db`、`memory.db`、`backups/` 容量与保留策略；
- 成本：CLI/前端显示单轮调用数、token、成本估算，结合 `MAX_*` 预算。

## 9. 安全默认值

- compose 只发布 `127.0.0.1`；公网必须放在反向代理/TLS 后面；
- Docker 端口映射后客户端不是回环，本地 compose 也必须设置 `APP_AUTH_TOKEN`；
- 多用户用 `admin.py` 创建账号并签发 token；不要共用 admin token，审计和配额会混在一起；
- 容器以 `appuser`（UID 10001）运行；Linux bind mount 需提前 chown；
- 未配置 `APP_AUTH_TOKEN` 只允许回环；
- 报告 HTML 经白名单清洗；抓取只允许公网 http/https；
- 请求体、主题、审核内容、抓取字节均有上限；
- 备份目录按敏感数据处理。
