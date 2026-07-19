# Docker + ngrok 本地调试

本文档使用 Docker 运行 GitHub AI Review 服务，使用 ngrok 接收 GitHub Webhook。宿主机端口固定为 `8787`。

## 1. 环境状态

本机已确认：

- Docker `29.6.1`
- Docker Compose `5.3.0`
- ngrok `3.39.9`
- ngrok 配置有效
- 宿主机端口 `8787` 当前未占用

## 2. 准备 GitHub App

GitHub App 基本信息建议填写：

```text
GitHub App name: AI Code Review

Description:
Automatically reviews pull request changes with AI, identifies high-confidence bugs, security risks, regressions, performance issues, and missing tests, then publishes structured review summaries and inline comments directly to GitHub.

Homepage URL:
https://github.com/你的账号/你的项目仓库
```

Description 中文参考：

```text
基于 AI 自动审查 Pull Request 代码变更，识别高置信度缺陷、安全风险、回归、性能问题和缺失测试，并将结构化总结与行内评论直接发布到 GitHub。
```

GitHub App 的 Description 建议使用英文版本，方便 GitHub 安装页面和不同团队成员阅读。Homepage URL 使用该项目仓库地址；本地调试地址和 ngrok 地址不要填写在 Homepage URL 中。

GitHub App 的 Repository permissions：

| 权限 | 级别 |
| --- | --- |
| Metadata | Read-only |
| Contents | Read-only |
| Pull requests | Read and write |
| Issues | Read and write |

订阅以下 Webhook events：

- Pull request
- Issue comment
- Installation
- Installation repositories

从 GitHub App 设置页生成并下载 PEM 私钥，记录它在宿主机上的绝对路径。安装 GitHub App 时只选择用于调试的仓库。

## 3. 创建 Docker 环境配置

进入项目：

```bash
cd /Users/ivyzhang/Desktop/workspace/ai_code_review
cp .env.docker.example .env.docker
```

编辑 `.env.docker`：

```dotenv
GITHUB_APP_ID=你的_GitHub_App_ID
GITHUB_APP_PRIVATE_KEY_HOST_PATH=/宿主机绝对路径/github-app.pem
GITHUB_WEBHOOK_SECRET=一个随机且固定的字符串

LLM_BASE_URL=https://你的模型服务/v1
LLM_API_KEY=你的模型密钥
LLM_MODEL=模型名称

INSTALLATION_DAILY_TASK_LIMIT=200
MAX_PATCH_CHARS=6000
MAX_INPUT_CHARS=60000
MAX_COMMENTS=20
MODEL_TIMEOUT_SECONDS=120
```

注意：

- `GITHUB_APP_PRIVATE_KEY_HOST_PATH` 必须是宿主机绝对路径。
- Compose 会把私钥只读挂载为容器内的 `/run/secrets/github-app.pem`。
- `LLM_BASE_URL` 不包含 `/chat/completions`。
- 不要提交 `.env.docker` 或 PEM 私钥。

## 4. 构建并启动服务

```bash
docker compose --env-file .env.docker up --build
```

Compose 会：

- 构建 Python 3.12 镜像。
- 将当前源码挂载到 `/app` 并启用 Uvicorn reload。
- 将宿主机 `127.0.0.1:8787` 映射到容器 `8787`。
- 使用 Docker volume `review-data` 持久保存 SQLite。
- 将 GitHub App 私钥以只读方式挂载到容器。

另开终端验证：

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/ready
curl http://127.0.0.1:8787/metrics
```

预期前两个接口分别返回：

```json
{"status":"ok"}
{"status":"ready"}
```

OpenAPI 页面：

```text
http://127.0.0.1:8787/docs
```

## 5. 启动 ngrok

再开一个终端：

```bash
ngrok http 8787
```

ngrok 会显示类似：

```text
Forwarding  https://example.ngrok-free.app -> http://localhost:8787
```

GitHub App 的 Webhook URL 设置为：

```text
https://example.ngrok-free.app/hook
```

Webhook Secret 必须与 `.env.docker` 中的 `GITHUB_WEBHOOK_SECRET` 完全一致。保存 GitHub App 配置后，可以在以下页面检查请求和响应：

```text
http://127.0.0.1:4040
```

ngrok Inspector 可以查看请求头、Payload、响应码并重放请求。重放相同 GitHub Delivery 时，服务应返回 `duplicate`。

## 6. 端到端调试

在已安装 GitHub App 的测试仓库中创建非 Draft PR。预期流程：

1. GitHub 发送 `pull_request.opened`。
2. ngrok 将请求转发到 `127.0.0.1:8787/hook`。
3. Webhook 返回 `{"status":"accepted"}`。
4. SQLite 持久保存 Delivery 和 Review 任务。
5. Worker 获取 PR patch 并调用模型。
6. PR 中出现一次包含总结和行内发现的 Review。

手动触发：

```text
/review 重点检查权限控制和输入校验
```

命令被接受后出现 `eyes` reaction，成功完成后出现 `+1`。

修改 PR 并推送新 commit，可以验证 `synchronize` 和发布前 head SHA 校验。旧任务一旦过期，不会发布 Review 或普通评论。

## 7. 查看日志和数据库

实时日志：

```bash
docker compose --env-file .env.docker logs -f review
```

查看容器状态：

```bash
docker compose --env-file .env.docker ps
```

进入容器查询 SQLite：

```bash
docker compose --env-file .env.docker exec review python - <<'PY'
import sqlite3

db = sqlite3.connect('/data/review.sqlite3')
db.row_factory = sqlite3.Row
for table in ('tasks', 'deliveries', 'published_reviews', 'installations', 'repositories'):
    print(f'\n[{table}]')
    for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid DESC LIMIT 20'):
        print(dict(row))
PY
```

## 8. 常见问题

### Webhook 返回 `401`

确认 GitHub App 和 `.env.docker` 使用完全相同的 Webhook Secret，然后重新启动 Compose：

```bash
docker compose --env-file .env.docker up --build
```

### `/ready` 返回 `503`

检查 Worker 日志和 SQLite volume：

```bash
docker compose --env-file .env.docker logs review
docker volume inspect ai_code_review_review-data
```

### Webhook 已接受但没有 Review

依次检查：

1. Compose 日志中的 GitHub 或模型错误。
2. `tasks` 表中的任务状态。
3. GitHub App 是否安装到目标仓库。
4. Pull requests 和 Issues 是否具有读写权限。
5. 模型地址、密钥和模型名称是否有效。
6. PR head SHA 是否在模型调用期间发生变化。

### ngrok 地址变化

免费随机地址在 ngrok 重启后可能变化。重新执行 `ngrok http 8787` 后，将新的 HTTPS 地址更新到 GitHub App Webhook URL。

## 9. 停止和清理

停止服务并保留 SQLite 数据：

```bash
docker compose --env-file .env.docker down
```

停止服务并删除 SQLite volume：

```bash
docker compose --env-file .env.docker down -v
```

第二条命令会永久删除本地任务和发布记录，只在确认不再需要调试数据时使用。
