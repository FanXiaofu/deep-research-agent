# 工程化落地记录

- 仓库：`deep-research-agent`
- 分支：`main`
- 最后更新：2026-10-29
- 验证基线：`python -m pytest tests -q` → **138 passed**
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

推送状态：

- 已通过 SSH 推送成功：`origin = ssh://git@ssh.github.com:443/FanXiaofu/deep-research-agent.git`；
- 远端 `main` 已包含到 `5bd630f`，tag `v0.1.0-audit-baseline` 已推送；E1 与本文档更新随后补推；
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
  - CI YAML 解析出 `test`、`docker` 两个 job；`pytest` 仍为 138 passed。
- 遗留：CI 尚未在 GitHub Actions 上实际跑过（配置推送后触发）；升级/回滚 runbook、非 root 容器、结构化日志/指标仍未做。
- 提交：`dd5ca26`。

## 3. 路线状态

| 阶段 | 状态 | 说明 |
|---|---|---|
| A 基线/稳定 | 完成 | A1 |
| B 正确性/隔离 | 完成 | B1 |
| C 数据可靠性 | 完成 | C1 |
| D 安全与运维 | 部分完成 | D1 已完成应用层边界；结构化日志/指标仍未做 |
| E 部署与交付 | 部分完成 | E1 完成依赖锁、CI、Docker 重建；升级/回滚 runbook 未做 |
| F 性能与质量 | 未开始 | 需先有测量；评测归因也需拆分 |

建议下一项：**R10 引用运行时后校验**。当前引用编号虽然在生成时由代码注册，但模型输出的越界 `[n]`、编造链接只在评测阶段才会被发现；上线前应在 writer 出口做一次确定性校验/剔除并留痕。之后再做 E2 结构化日志与 runbook、F 阶段评测拆分。

## 4. 部署检查清单（公网）

- [x] 依赖锁定 + CI + Docker 镜像构建（E1）
- [ ] `.env` 设置足够长的随机 `APP_AUTH_TOKEN`
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
- 报告引用在运行时没有后校验；越界 `[n]` 目前只能靠评测发现（R10 待做）。
- LLM judge 与研究共用同一模型时存在系统性偏差；对外数字需更强模型复评。
- 评测 A/B 曾同时调整 4 个参数，0.82→1.00 不能单独归因于“代码生成引用编号”（baseline 已有注册表）；README 已修正口径。
- 结构化日志/指标、非 root 容器、升级/回滚 runbook 仍未做；CI 已配置但尚未在 GitHub Actions 上实跑确认。

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
