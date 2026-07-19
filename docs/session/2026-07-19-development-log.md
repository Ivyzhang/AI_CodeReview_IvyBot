# GitHub AI Review 项目会话记录

日期：2026-07-19
工作目录：`/Users/ivyzhang/Desktop/workspace/ai_code_review`

## 1. 初始状态

目录最初只有 `design.md`。该文件是一份面向初学者的 GitHub AI Review Bot 教学稿，包含单文件示例代码、GitHub PAT、进程内队列、PR Files API、OpenAI-compatible 模型调用和 GitHub Review 回写流程。

用户要求：

> 深度调研目录里的 design.md，深入理解这个项目，然后根据内容重新生成设计文档。不要发散，以 design.md 为基础，但不要有学习 Demo 的痕迹，要作为一个正式项目。

## 2. 设计文档决策

讨论了两种版本定位：

- 保留轻量技术方案，作为可运行的单实例项目。
- 纳入 GitHub App、持久化、多 Worker 和完整仓库上下文，作为生产架构。

用户决定同时生成两个版本：

- V1：当前可交付版本，同时保留后续扩展接口。
- V2：作为 V1 的下一版本规划，不另起产品方向。

文档组织采用两份独立文件，并保留原始 `design.md`：

- `project-design-v1.md`
- `project-design-v2.md`

原始 `design.md` 不覆盖、不修改。

## 3. GitHub App 决策

最初讨论了 Fine-grained PAT 与 GitHub App 的区别。

结论：

- V1 正式接入方式使用 GitHub App。
- PAT 只作为本地开发或受控内部调试的兼容方式。
- 用户通过安装 GitHub App 并选择仓库完成接入。
- 服务根据 Webhook 中的 `installation.id` 获取短期 Installation Token。

V1 的 GitHub App 最小权限：

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

## 4. 设计审查意见

用户对第一版 V1 设计提出了 10 项审查意见：

1. 模型调用期间 PR 更新时，旧 Review 仍可能通过普通评论写入新版本。
2. 进程内队列无法支撑正式版本的任务可靠性。
3. GitHub App 权限、事件、安装和撤权配置不完整。
4. 缺少同一 head SHA 的 Review 业务幂等。
5. 缺少仓库级产品配置。
6. 文件与 patch 截断策略不明确。
7. 缺少模型成本和滥用边界。
8. 私有代码发送外部模型时的数据治理不足。
9. 缺少可衡量的产品效果指标。
10. 手动任务状态反馈和运行指标不完整。

根据审查，V1 定位保持为正式版本，并作出以下调整：

- 引入 SQLite 持久任务、Delivery、Installation、仓库授权和发布状态。
- Webhook 在事务提交后才返回 `202`。
- 模型调用前和发布前分别检查 head SHA。
- SHA 变化时停止任务，不发布 Review，也不降级普通评论。
- 只有 SHA 未变化且 GitHub 明确拒绝行内位置时才允许降级。
- 增加 Delivery、业务任务和发布三个层级的幂等。
- 增加 `.ai-review.yml` 仓库策略。
- 增加确定性的文件优先级和截断规则。
- 增加冷却、并发合并、输入限制、超时和 Installation 每日软限制。
- 增加数据外发、保留、训练政策和删除边界。
- 增加用户 reaction、健康检查、指标和灰度指标。

测试要求由用户明确为：

> 只测试产品的业务逻辑，不对配置、测试文件等添加测试点。

## 5. V1 代码实现

用户要求：

> 按照 project-design-v1.md 一步到位实现代码，要求代码简洁、结构清晰。

目标运行时确认使用 Python `3.12.13`。

主要源码：

```text
app/
├── config.py
├── context.py
├── events.py
├── github.py
├── main.py
├── models.py
├── policy.py
├── review.py
├── service.py
├── storage.py
└── worker.py
```

实现内容：

- FastAPI Webhook 服务。
- GitHub HMAC SHA-256 验签。
- GitHub App JWT 和 Installation Token。
- SQLite 持久任务、恢复和状态流转。
- Delivery ID 幂等。
- 自动/手动 Review 业务幂等。
- GitHub 发布标记与发布幂等。
- Installation 暂停、卸载和仓库移除处理。
- PR 自动触发和 `/review` 手动命令。
- 仓库策略读取和业务过滤。
- patch RIGHT-side 行号解析。
- 确定性文件选择、排除和截断。
- OpenAI-compatible 模型调用。
- Pydantic 结构化结果解析和一次格式修复。
- 文件路径和 diff 行号校验。
- 批量 GitHub Review。
- 发布前二次 SHA 校验。
- 受限普通评论降级。
- 单 Worker、任务恢复和每日软限制。
- `/health`、`/ready` 和 `/metrics`。

依赖安装在项目 `.venv`，核心框架版本固定为：

```text
Python 3.12
FastAPI 0.115.14
Pydantic 2.10.6
pydantic-settings 2.7.1
```

实现计划保存在：

`docs/superpowers/plans/2026-07-19-github-ai-review-v1.md`

## 6. 测试和验证

实现采用业务测试优先方式。最终测试覆盖：

- 关注点归一化和幂等键。
- Delivery、任务和发布幂等。
- SQLite 崩溃恢复。
- Installation 与仓库撤权。
- PR 与 `/review` 事件过滤。
- diff 行号和文件选择。
- 模型 JSON 修复和 Finding 校验。
- GitHub 分页、批量 Review 和错误分类。
- 模型调用前和发布前双 SHA 门禁。
- Webhook 持久化后返回和状态响应。
- 每日 Installation 软限制。
- 健康、就绪和指标接口。

最终验证结果：

```text
32 passed
Python compileall passed
FastAPI route import passed
```

已确认以下路由存在：

```text
/hook
/health
/ready
/metrics
/docs
/redoc
```

原始 `design.md` SHA-256 保持不变：

```text
40ebddafaabc813831f216c81616353cf13bc2c1febbba8e750dcc8f461e73e5
```

当前目录不是 Git 仓库，因此没有 commit、分支或 Pull Request。

## 7. 本地调试演进

最初使用临时凭证在 `127.0.0.1:8000` 验证服务，健康、就绪和指标接口均成功。该临时服务随后已停止。

用户确认本机具有：

```text
Docker 29.6.1
Docker Compose 5.3.0
ngrok 3.39.9
```

最终本地调试方案改为：

- Docker 运行应用。
- 宿主机和容器统一使用端口 `8787`。
- ngrok 使用 `ngrok http 8787`。
- SQLite 保存在 Docker volume `review-data`。
- GitHub App PEM 私钥只读挂载到 `/run/secrets/github-app.pem`。
- 当前源码挂载到 `/app` 并启用 Uvicorn reload。

新增文件：

- `Dockerfile`
- `compose.yaml`
- `.dockerignore`
- `.env.docker.example`
- `LOCAL_DEBUG.md`

启动方式：

```bash
cp .env.docker.example .env.docker
# 填写真实 GitHub App、私钥和模型配置

docker compose --env-file .env.docker up --build
```

另一个终端：

```bash
ngrok http 8787
```

GitHub App Webhook URL：

```text
https://你的-ngrok-地址/hook
```

ngrok Inspector：

```text
http://127.0.0.1:4040
```

完整步骤见 `LOCAL_DEBUG.md`。

## 8. GitHub App 展示信息

建议的 GitHub App 名称：

```text
AI Code Review
```

建议的英文 Description：

```text
Automatically reviews pull request changes with AI, identifies high-confidence bugs, security risks, regressions, performance issues, and missing tests, then publishes structured review summaries and inline comments directly to GitHub.
```

中文参考：

```text
基于 AI 自动审查 Pull Request 代码变更，识别高置信度缺陷、安全风险、回归、性能问题和缺失测试，并将结构化总结与行内评论直接发布到 GitHub。
```

当前项目没有用户 Web 页面或管理后台：

- Homepage URL 使用项目 GitHub 仓库地址。
- Setup URL 留空。
- Callback URL 留空。
- `/docs` 只是开发 API 文档，不作为 Homepage。

## 9. 当前状态和后续入口

当前 V1 已实现并通过本地测试，但尚未使用用户的真实 GitHub App 和模型凭证完成外部端到端验证。

后续调试入口：

1. 按 `LOCAL_DEBUG.md` 创建 `.env.docker`。
2. 启动 Docker Compose。
3. 启动 ngrok。
4. 更新 GitHub App Webhook URL。
5. 安装 App 到测试仓库。
6. 创建 PR 或使用 `/review`。
7. 同时观察 Compose 日志、ngrok Inspector 和 SQLite 状态。

重要文件索引：

| 文件 | 用途 |
| --- | --- |
| `design.md` | 原始设计来源，未修改 |
| `project-design-v1.md` | V1 正式设计 |
| `project-design-v2.md` | V2 演进规划 |
| `README.md` | 项目说明 |
| `LOCAL_DEBUG.md` | Docker + ngrok 本地调试 |
| `.env.docker.example` | Docker 环境变量模板 |
| `.ai-review.yml.example` | 仓库级 Review 策略示例 |
