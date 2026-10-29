# 部署到 Render

仓库根目录的 `render.yaml` 会创建：

- 一个 Docker Web Service（FastAPI + LangGraph）；
- 一个 Render Postgres（checkpoint、memory、auth、审计）；
- 所有模型/搜索/登录 secret 通过 Render 环境变量注入。

不适合直接照搬的部分：Render 免费 Web 服务会休眠，免费 Postgres 有期限；长期使用请升到 paid plan。

## 1. 部署前

1. 轮换已经暴露的 Command Code key，只把新 key 放进 Render 环境变量，不提交仓库。
2. 准备 Tavily key（或把 `SEARCH_PROVIDER` 改为 `duckduckgo`，不需要 key）。
3. 生成登录 token：

```powershell
-join ((48..57)+(65..90)+(97..122) | Get-Random -Count 48 | ForEach-Object {[char]$_})
```

4. 确认 `render.yaml`、`Dockerfile` 已在 `main`：

```powershell
git status
git push origin main
```

## 2. 创建 Blueprint

Render Dashboard → **New +** → **Blueprint** → 连接 GitHub 仓库 → 选择 `deep-research-agent`。

Render 会读取 `render.yaml`，并提示填写：

| Key | 填什么 |
|---|---|
| `LLM_API_KEY` | 轮换后的 Command Code key |
| `SEARCH_API_KEY` | Tavily key；如果不用 Tavily，先部署后改成 `duckduckgo` |
| `APP_AUTH_TOKEN` | 上一步生成的随机字符串，登录网页时用 |

`DATABASE_URL` 自动从 `deep-research-db` 注入，不需要手填。

点击 **Apply**。首次构建会安装 `requirements.lock` 并启动 Uvicorn。

## 3. 验证

在 Render 服务页看：

- **Logs**：出现 `Uvicorn running on http://0.0.0.0:10000`；
- **Health**：`/readyz` 返回 200；
- 打开 `https://<service>.onrender.com`；
- 在页面顶部 token 输入框填 `APP_AUTH_TOKEN`；
- 先跑一个短问题，确认能收到 SSE 节点事件和最终报告。

如果 `/readyz` 一直 503：

- 看 Postgres 是否已 available；
- 看 `DATABASE_URL` 是否来自 `deep-research-db`；
- 看日志里的 `database` / `auth` 检查项；
- Render free Postgres 初始化可能需要一点时间。

## 4. Render 运行时行为

### 端口

Render 注入 `PORT`（通常是 `10000`）。Dockerfile 使用：

```sh
--port ${PORT:-8000}
```

本地仍默认 8000，Render 不会因为端口不匹配失败。

### SSE

研究请求可能持续数分钟。`_stream` 每 15 秒发送一个 SSE 注释帧：

```text
: heartbeat
```

它不会出现在前端事件里，只用于防止 Render/代理因长时间无数据断开连接。

### 持久化

配置了 `DATABASE_URL` 后：

- checkpoint；
- 账号/API token/配额/审计

都在 Postgres。容器本地文件系统是临时的，重建后不会丢研究状态。

`render.yaml` 首次把 `MEMORY_ENABLED` 设为 `false`，避免 Postgres 冷启动/建表拖住 writer 之后的归档；确认稳定后可在 Render 环境变量改为 `true` 再重新部署。开启后研究记忆也存 Postgres。

如果页面在 writer 后断流，前端会自动调用 `/api/report/<thread_id>` 从 checkpoint 取回报告；timeline 会显示当前 thread id。

### 休眠与数据库期限

Render 免费层不支持 `maxShutdownDelaySeconds`，所以 Blueprint 不设置该字段；付费实例需要更长收尾时间时可在 Dashboard 或 Blueprint 中加回。

Render 免费层：

- Web Service 15 分钟无请求会休眠，下一次请求冷启动；
- 免费 Postgres 有 30 天期限（以 Render 当前政策为准）；
- 免费 Web 实例 512 MB / 0.1 CPU。

演示可以用 free；长期运行建议：

- Web Service 选 paid always-on；
- Postgres 选 paid；
- 需要多实例时再加 Render Key Value，并把 `REDIS_URL` 注入服务。

## 5. 账号与权限

`APP_AUTH_TOKEN` 是静态 **admin** token，可直接登录网页和访问 `/metrics`、`/api/audit`。

需要普通用户时，在 Render Shell 或同一 Postgres 环境执行：

```bash
python admin.py create-user --username alice --role user --daily-quota 10
python admin.py issue-token --username alice --name browser
```

明文 token 只显示一次；服务端只存 SHA-256。

## 6. 更新配置

- 改模型/搜索参数：Render Dashboard → Environment；
- 改代码：push `main`，`autoDeployTrigger: commit` 自动部署；
- 不要把 `.env` 提交到 GitHub；
- 换 key 后只更新 Render 环境变量，并重新 deploy。

## 7. 当前限制

- `render.yaml` 只起单实例；多实例需要共享 Redis（限流/指标）和 Postgres（已具备）；
- Render 免费 Web 会休眠，不适合要求秒级首响的场景；
- 免费 Postgres 不是长期生产数据库；
- 自定义 WAF/DDoS 仍建议放在 Cloudflare 等边缘层；
- `AGENT_MODE=on` 会带来多次模型调用，先观察单题耗时和成本。

## 参考

- [Render Blueprint YAML Reference](https://render.com/docs/blueprint-spec)
- [Render Docker services](https://render.com/docs/docker)
- [Render free tier limits](https://render.com/docs/free)
- [Render long-running web requests](https://render.com/articles/how-to-trigger-a-long-running-task-from-a-web-service-on-render)
