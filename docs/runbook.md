# 运行手册（E2）

适用对象：负责部署、升级、排障的维护者。命令默认在仓库根目录执行。

## 0. 关键约定

- 进程：每个容器 1 个 uvicorn；水平扩展用多副本（`--scale app=N`），不要在一个容器里开多 worker（SQLite 模式尤其如此）。
- 入口：`POST /api/research/stream`、`POST /api/resume/stream`、`GET /api/report/{thread_id}`。
- 鉴权：`Authorization: Bearer <token>`；静态 `APP_AUTH_TOKEN(S)` 为 admin，数据库账号 token 分 user/admin；user 只能访问自己的 thread 并受每日配额约束。
- 日志：默认 JSON 行到 stdout，关键字段 `request_id` / `thread_id` / `level` / `message`。
- 数据：默认 `checkpoints.db`（断点）、`memory.db`（研究档案）、`auth.db`（账号/token/审计）；设置 `DATABASE_URL` 后都在 Postgres。
- 共享状态：设置 `REDIS_URL` 后限流与指标计数在 Redis，多实例共享。
- 健康检查：`GET /healthz`（200/503，只读本地配置，不访问外部依赖）；`GET /readyz` 检查数据库，Redis 故障标记 degraded。
- 多副本：`docker-compose.prod.yml` 中 app 不发布端口；Caddy 负责 TLS/安全头，nginx 通过 Docker DNS 发现 app 副本并分发；研究流绑单副本，续跑从共享 checkpoint 恢复。
- 运行指标：`GET /metrics`（请求计数 + research 状态；仅 admin，静态 admin token 或 admin 账号 token）。
- 容器用户：`appuser`（UID 10001），非 root。

## 1. 发布前检查

```bash
python -m pytest tests -q            # 必须全绿
git status --short                   # 必须干净
docker build -t deep-research-agent:$(git rev-parse --short HEAD) .
```

- CI（`.github/workflows/ci.yml`）必须通过 `test`、`docker`、`runtime`、`postgres`、`redis`、`security`、`compose` 七个 job。
- 生产必须设置：`APP_AUTH_TOKEN`（admin）、LLM key、搜索 key；优先用 `*_FILE` 挂 secret。
- 升级前先加密备份：

```bash
export BACKUP_ENCRYPTION_KEY='<Fernet key>'
python maintenance.py backup --encrypt --out backups
# Postgres 模式：python maintenance.py pg-backup --encrypt --out backups
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
# 生成 key（只显示一次，存 secret manager）
python maintenance.py gen-key

# 加密备份（checkpoint + memory + auth），成功后删除明文
export BACKUP_ENCRYPTION_KEY='<Fernet key>'
python maintenance.py backup --encrypt --out backups

# 加密后同步到异地（需要 rclone）
python maintenance.py backup --encrypt --rclone-remote remote:deep-research-backups

# 清理过期 checkpoint / 备份
python maintenance.py prune-checkpoints --days 7
python maintenance.py prune-backups --days 30

# 恢复（必须显式 --yes；.fernet 会自动要求 key）
python maintenance.py restore backups/checkpoints-YYYYmmdd-HHMMSS-ffffff.db.fernet \
  --target checkpoints.db --key-file backup.key --yes
```

- 备份使用 sqlite backup API，包含 WAL；不要直接 `cp` 正在写入的 `.db`。
- `backups/` 可能含研究内容，限制目录权限；加密成功后才删明文。
- **备份 key 丢失 = 无法恢复**：至少两地保存 key，定期做恢复演练。
- 建议 cron / Task Scheduler 每天备份、每周清理。

### Postgres 模式

```bash
# 备份 + 加密 + 可选 rclone 异地
python maintenance.py pg-backup --encrypt --out backups
python maintenance.py pg-backup --encrypt --rclone-remote remote:deep-research-backups

# 恢复（先停 app；会 --clean --if-exists 覆盖目标库）
python maintenance.py pg-restore backups/postgres-....dump.fernet \
  --database-url "$DATABASE_URL" --key-file backup.key --yes

# SQLite 研究档案迁移到 Postgres（旧 checkpoint 不迁移，先让在途任务完成）
python maintenance.py migrate-memory --from memory.db --database-url "$DATABASE_URL"
```

- `maintenance.py backup/prune/restore` 只维护 SQLite；Postgres 用 `pg-backup`/`pg-restore` 或 pg_dump/pg_restore。
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
2. 更新 secret 文件（`*_FILE`）或 `.env`；
3. 重启服务；
4. 用 `/healthz` + 一次小规模研究验证；
5. 撤销旧 key。

### 备份加密 key

- 新 key 只用于新备份；旧备份仍需要旧 key 才能解密。
- 轮换时保留旧 key 至少一个备份保留周期（默认 30 天），并标注对应日期范围。
- 不要把新旧 key 一起放在同一台生产主机；至少两地保存。
- key 丢失无法恢复备份，必须做恢复演练。

## 7. 质量审计

- 报告末尾会附“证据支撑审计”：词面覆盖低/数字未在来源中找到的句子会被列出，但不会自动删改。
- 默认只做零成本词面审计；设置 `GROUNDING_JUDGE_MODEL` 后追加语义 judge，每轮最多 `GROUNDING_MAX_CLAIMS` 条，计入 LLM 预算/成本。
- 语义 judge 使用独立模型时，注意它和研究模型的相关性；不要把分数当绝对真值。
- 网页内容统一视为不可信数据；若发现注入迹象，先停用相关来源并人工复核报告。
- 可选 `SAFETY_JUDGE_MODEL` 会追加“内容安全审计”（low/medium/high），默认关闭；高风险结果必须人工处理，不能视为合规结论。
- 独立 judge：`JUDGE_PROVIDER=commandcode` + `JUDGE_MODEL=deepseek/deepseek-v4-pro`（Go 套餐可用），并设置 `COMMAND_CODE_API_KEY`；`cmdc` 必须已安装。该模式每题会通过 headless CLI 调用，速度比本地 judge 慢。

## 8. 故障排查

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

## 9. 多副本与日志聚合（P1.4）

```bash
# 启动 3 个 app 副本 + Caddy + Postgres + Redis
export APP_DOMAIN=research.example.com
export POSTGRES_PASSWORD='<random>'
export REDIS_PASSWORD='<random>'
docker compose -f docker-compose.prod.yml up -d --build --scale app=3

# 本地无域名时：APP_DOMAIN=http://localhost CADDY_HTTP_PORT=8080 \
#   docker compose -f docker-compose.prod.yml up -d --build --scale app=3
#   curl http://localhost:8080/healthz

# 通过 Caddy（80/443）验证
curl -s https://research.example.com/healthz
curl -s https://research.example.com/readyz
```

- app 不发布宿主机端口；`docker compose ps` 应看到多个 app 副本。
- Caddy 每 10s 检查入口 `/readyz`；nginx 对失败后端被动重试，Docker DNS 每 5s 刷新副本列表。
- 扩容/缩容后若 nginx 未发现新副本，检查 `resolver`/重启 nginx。
- 停止副本时会等待最多 120s（uvicorn）+ 30s 缓冲（stop_grace_period 150s），在途 SSE 尽量收尾。
- 研究流本身不跨副本；续跑请求可以落到任意副本，因为 checkpoint 在 Postgres。

可选日志聚合：

```bash
docker compose -f docker-compose.prod.yml -f docker-compose.observability.yml up -d
# Grafana: http://127.0.0.1:3001
```

- Loki 只绑 `127.0.0.1:3100`；Promtail 需要 Docker socket，等同 root 权限，只用于受信主机。
- Promtail 从容器 stdout 收集 JSON 日志，`service`/`container` label 可用于过滤。
- Prometheus 抓取示例在 `deploy/prometheus.example.yml`；把 `REPLACE_WITH_ADMIN_TOKEN` 换成 admin token，使用 DNS SD 发现 `app` 的多个副本。
- 若日志量/主机数上来，改用 Docker Loki logging driver、Alloy 或云日志，不要长期把 Docker socket 暴露给容器。

## 10. 监控建议

- 容器 healthcheck + `/healthz` 探活；
- `GET /metrics` 输出 Prometheus 文本（请求计数 + research started/finished/failed/active）；
  配置 `REDIS_URL` 时从 Redis 汇总，多实例一致；抓取需 admin token：
  `curl -H "Authorization: Bearer $APP_AUTH_TOKEN" http://127.0.0.1:8000/metrics`
- `GET /metrics.json` 输出 JSON 快照，便于人工排查。
- 日志按 `level=ERROR` 与 `request_id/thread_id` 聚合；
- Langfuse（可选）看每轮 trace、token、延迟；
- 磁盘：`checkpoints.db`、`memory.db`、`backups/` 容量与保留策略；
- 成本：CLI/前端显示单轮调用数、token、成本估算，结合 `MAX_*` 预算。

## 11. 安全默认值

- compose 只发布 `127.0.0.1`；公网必须放在反向代理/TLS 后面；
- Docker 端口映射后客户端不是回环，本地 compose 也必须设置 `APP_AUTH_TOKEN`；
- 多用户用 `admin.py` 创建账号并签发 token；不要共用 admin token，审计和配额会混在一起；
- 容器以 `appuser`（UID 10001）运行；Linux bind mount 需提前 chown；
- 未配置 `APP_AUTH_TOKEN` 只允许回环；
- 报告 HTML 经白名单清洗；抓取只允许公网 http/https；
- 请求体、主题、审核内容、抓取字节均有上限；
- 备份目录按敏感数据处理。
