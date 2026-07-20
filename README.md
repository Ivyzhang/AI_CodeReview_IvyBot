# GitHub AI Code Review Ivybot

基于 GitHub App 的单实例 AI Code Review 服务。服务持久接收 GitHub Webhook，审查 Pull Request patch，校验模型返回的文件与 diff 行号，并通过一次 GitHub Review 写回结果。

## 核心行为

- `opened`、`reopened`、`ready_for_review` 和按策略启用的 `synchronize` 自动触发 Review。
- 仓库成员可以使用 `/review [关注点]` 手动触发。
- Delivery、同一 head SHA 的业务任务和 GitHub 发布均具备幂等保护。
- SQLite 在 Webhook 返回 `202` 前持久保存任务，服务重启后恢复超时任务。
- 模型调用前和发布前分别校验 head SHA；过期结果不写回。
- 只有 SHA 未变化且 GitHub 明确拒绝行内位置时，结果才降级为普通 PR 评论。

## GitHub App

Repository permissions：

| 权限 | 级别 |
| --- | --- |
| Metadata | Read-only |
| Contents | Read-only |
| Pull requests | Read and write |
| Issues | Read and write |

订阅事件：

- Pull request
- Issue comment
- Installation
- Installation repositories

Webhook URL 设置为公开 HTTPS 地址的 `/hook`，Webhook Secret 必须与服务配置一致。应用不需要用户 OAuth callback。安装时由仓库管理员选择允许访问的仓库。

## 本地运行

要求 Python 3.12：

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
```

准备 GitHub App PEM 私钥并修改 `.env`，然后启动：

```bash
.venv/bin/uvicorn 'app.main:create_app_from_env' --factory --host 0.0.0.0 --port 8000
```

正式部署时，`DATABASE_PATH` 必须指向持久卷。V1 只允许一个服务实例消费该 SQLite 文件；滚动部署前应停止旧 Worker。

## 仓库策略

仓库可以将 [.ai-review.yml.example](.ai-review.yml.example) 复制为默认分支根目录的 `.ai-review.yml`。服务读取 PR base 分支上的策略，待审 PR 不能通过修改自身策略绕过限制。

仓库策略可以关闭自动 Review、控制 Draft/Bot PR、限定目标分支、排除路径、限制文件和变更行数，以及设置输出语言和最低严重级别。系统级资源上限始终优先。

## 接口

| 接口 | 用途 |
| --- | --- |
| `POST /hook` | GitHub Webhook |
| `GET /health` | 进程存活状态 |
| `GET /ready` | SQLite 与 Worker 就绪状态 |
| `GET /metrics` | 队列和 Webhook 计数指标 |

Webhook 业务状态包括 `accepted`、`duplicate`、`existing`、`ignored` 和 `limited`。

## 数据边界

模型服务只接收筛选后的 PR 标题、描述、关注点和代码 patch。GitHub Token、App 私钥、模型密钥及未选中的仓库文件不会发送给模型。

服务默认只在 SQLite 中保存任务元数据、状态、用量和 GitHub 发布 ID，不保存完整 Prompt、模型原始响应或完整代码。私有仓库代码会发送到配置的模型供应商；部署方必须确认供应商不会将 API 输入用于训练，并向安装管理员披露供应商、数据区域、保留和删除政策。

## 验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q app
```

## CI

GitHub Actions 在每个 Pull Request 的创建、更新、重新打开和转为 Ready for review 时运行 Python 3.12 测试，并在旧任务仍运行时自动取消旧检查。CI 只安装项目依赖、执行业务测试和 Python 编译检查，不读取 `.env`、GitHub App 私钥或模型密钥。
