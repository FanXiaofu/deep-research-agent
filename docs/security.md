# 安全基线（P1.5）

本文记录当前实现的安全边界、运维要求与仍未覆盖的风险，避免把“有 TLS”误当成“生产安全”。

## 1. 身份与权限

- 数据库 API token（`auth.py`）：只存 SHA-256，角色 `user` / `admin`。
- 静态 `APP_AUTH_TOKEN(S)`：兼容旧部署，视为 admin；生产建议只用于监控/应急。
- `user` 只能访问自己发起的 thread，按自然日消耗研究配额；`admin` 可跨 thread、看 `/metrics` 与 `/api/audit`。
- 所有研究/续跑/报告接口走 Bearer；`/healthz`、`/readyz` 公开。
- 错误 token 不会因为请求来自 loopback 而回退成本机匿名 admin。
- 令牌轮换：`admin.py issue-token` / `disable-token`；禁用用户会使其所有 token 失效。

## 2. Secret 管理

配置支持 `<NAME>_FILE`，优先于环境变量，适配 Docker/Kubernetes secret：

```bash
# 例如 Docker secret
export LLM_API_KEY_FILE=/run/secrets/llm_api_key
export APP_AUTH_TOKEN_FILE=/run/secrets/app_auth_token
export DATABASE_URL_FILE=/run/secrets/database_url
```

支持：`LLM_API_KEY`、`SEARCH_API_KEY`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`DATABASE_URL`、`REDIS_URL`、`APP_AUTH_TOKEN`、`APP_AUTH_TOKENS`、`BACKUP_ENCRYPTION_KEY`。

- 不要把 secret 写入镜像、Git、`.env.example`；
- 生产用 Docker secret、K8s Secret、Vault/云 secret manager 挂文件；
- secret 文件权限至少 `0400`，只给运行用户（容器 UID 10001）读。

## 3. 备份加密与异地

```bash
# 生成 key（只显示一次，保存到 secret manager）
python maintenance.py gen-key

# SQLite：加密并删除明文
export BACKUP_ENCRYPTION_KEY='...'
python maintenance.py backup --encrypt --out backups

# Postgres：pg_dump 自定义格式 + 加密
python maintenance.py pg-backup --encrypt --out backups

# 加密后同步到异地（需要 rclone）
python maintenance.py backup --encrypt --rclone-remote remote:deep-research-backups

# 清理 30 天前备份
python maintenance.py prune-backups --days 30
```

- 加密算法 Fernet（AES-128-CBC + HMAC），带完整性校验；错误 key 会拒绝解密。
- `--rclone-remote` 只能与 `--encrypt` 一起用，避免同步明文。
- 恢复：`python maintenance.py restore backups/xxx.db.fernet --target checkpoints.db --yes`；Postgres 用 `pg-restore`（要求 `--yes`）。
- **key 丢失 = 备份不可恢复**：至少两地保存 key，并定期演练恢复。
- 备份目录仍按敏感数据管理；加密只解决存储介质泄露，不解决主机被攻陷后的内存/key 泄露。

## 4. 供应链

- CI `security` job 构建镜像后用 Trivy 扫描 `HIGH,CRITICAL`，只忽略无修复项。
- `.github/dependabot.yml` 每周更新 pip / Docker / GitHub Actions。
- `requirements.lock` 锁定测试通过的版本；升级后必须重跑 CI。
- 镜像以非 root `appuser`（UID 10001）运行。
- 基础镜像/依赖仍可能有未修复 CVE；Trivy 失败时先评估可利用性，不要直接关闭扫描。

## 5. 边缘防护

当前仓库内实现：

- Caddy：TLS、HSTS、CSP、`X-Frame-Options: DENY`、`nosniff`、`Referrer-Policy`、请求体 64KB；
- nginx：`client_max_body_size 64k`、`proxy_buffering off`、SSE 长连接、被动故障转移；
- 应用：Bearer、按 token/IP 限流、主题/审核长度、SSRF 防护、报告 HTML 白名单清洗；
- CSP 已去掉 `unsafe-inline`，前端 CSS/JS 外置到 `/static`。

**WAF 不在本仓库内**。公网部署建议在 DNS/入口层加：

- Cloudflare WAF / AWS WAF / GCP Cloud Armor / 阿里云 WAF；
- 托管规则（OWASP CRS）、Bot 管理、地理/ASN 规则；
- 对 `/api/research/stream` 做速率与并发限制；
- 隐藏源站，只允许 WAF 回源（IP allowlist / mTLS / Cloudflare Tunnel）。

没有 WAF 时，应用限流和 token 仍是最后一道门槛；不要因为“有 token”就省略边缘防护。

## 6. 数据与日志

- 研究主题、报告、审计事件可能含业务敏感信息；日志/备份/数据库按敏感数据分级。
- 日志默认 JSON 到 stdout，不记录 token 明文；auth 日志只记录 token 哈希前缀。
- 报告 HTML 经 `nh3` 白名单清洗；引用链接限制在来源清单内。
- Postgres 磁盘/卷加密、对象存储服务端加密由基础设施负责，当前仓库不配置。
- 备份保留 30 天是可配置默认值；按合规要求调整。

## 7. 仍未覆盖

- 没有浏览器登录/密码/MFA；账号是 token 型。
- 没有 token 过期/自动轮换；需人工 `disable-token`。
- 没有 WAF、Bot 管理、DDoS 清洗的内置实现。
- 没有 secret manager 集成代码，只支持挂文件。
- 没有镜像签名/证明（cosign/SLSA）；只有漏洞扫描与依赖更新。
- 没有主机/运行时入侵检测（Falco/auditd）。
- 没有真实模型在线时的提示词注入/内容安全专项测试。
