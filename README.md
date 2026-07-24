# AI Code Review IvyBot

一个认真看 Pull Request、但不会在凌晨两点催你改代码的 AI Review Bot。

IvyBot 以 GitHub App 的方式接入仓库，在 PR 创建、重新打开或更新时读取代码差异，调用兼容 OpenAI API 的模型完成审查，再把总结和行内问题写回 GitHub。你也可以留下一条 `/review` 评论，让它重点看看权限、性能或任何你放心不下的地方。

它不替你决定代码能不能合并，也不假装自己永远正确。它负责先筛一遍高价值问题，让人工 Review 把时间花在真正需要判断的地方。

## 它能做什么

- 自动审查新建、重新打开和转为 Ready for review 的 PR。
- 按仓库策略审查后续推送的代码变更。
- 支持 `/review [关注点]` 手动触发定向审查。
- 将结论发布为一次 GitHub Review，问题尽量落到对应的 diff 行。
- 识别缺陷、安全风险、行为回归、性能问题和缺失测试。
- 通过仓库配置控制分支、Draft、Bot PR、排除路径、规模上限、语言和严重等级。

一句话概括：开发者负责写代码，IvyBot 负责先把代码里的“等等，这里好像不太对”找出来。

## 工作流程

```text
GitHub PR / /review
        |
        v
签名校验与事件解析
        |
        v
SQLite 持久化任务  --->  Worker 重启恢复
        |
        v
读取仓库策略与 PR diff
        |
        v
构造上下文并调用模型
        |
        v
校验文件、行号与严重等级
        |
        v
GitHub Review + 行内评论
```

这条链路看起来不长，真正容易出问题的地方却不少：Webhook 会重投，服务会重启，PR 在模型思考时还可能继续更新，模型也可能一本正经地返回一个根本不存在的行号。IvyBot 对这些情况分别做了处理：

- Webhook 返回 `202` 前先把任务写入 SQLite，进程退出不等于任务失忆。
- Delivery、业务任务和发布结果分层幂等，避免同一份结果重复出现。
- 模型调用前和发布前两次检查 head SHA，旧代码的结论不会写到新版本 PR 上。
- 发布前校验模型返回的文件和 diff 行号，模型说得再自信也得先对坐标。
- 同一 PR 的新任务会让旧任务失效；临时网络错误由 Worker 统一重试。
- 行内位置失效时，只有确认 PR 版本未变化才会降级为普通评论。

## 快速开始

最省事的本地调试组合是 Docker + ngrok，服务使用宿主机端口 `8787`。

### 1. 创建 GitHub App

在 GitHub 的 **Settings > Developer settings > GitHub Apps** 中创建 App。

Repository permissions：

| 权限 | 级别 | 用途 |
| --- | --- | --- |
| Metadata | Read-only | 读取仓库基础信息 |
| Contents | Read-only | 读取 PR diff 和仓库配置 |
| Pull requests | Read and write | 读取 PR 并发布 Review |
| Issues | Read and write | 读取 `/review` 并添加 reaction |

订阅以下 Webhook events：

- Pull request
- Issue comment
- Installation
- Installation repositories

生成并下载 PEM 私钥，然后把 App 安装到用于测试的仓库。Homepage URL 可以填写本项目仓库地址；本地隧道地址只属于 Webhook URL，不必让它兼职当官网。

### 2. 准备配置

```bash
cp .env.docker.example .env.docker
```

编辑 `.env.docker`：

```dotenv
GITHUB_APP_ID=你的_App_ID
GITHUB_APP_PRIVATE_KEY_HOST_PATH=/宿主机绝对路径/github-app.pem
GITHUB_WEBHOOK_SECRET=一个随机且固定的字符串

LLM_BASE_URL=https://你的模型服务/v1
LLM_API_KEY=你的模型密钥
LLM_MODEL=模型名称
```

`LLM_BASE_URL` 使用兼容 OpenAI Chat Completions 的 API 根地址，不要再拼接 `/chat/completions`。`.env.docker` 和 PEM 私钥都不应提交到 Git。

### 3. 启动服务

```bash
docker compose --env-file .env.docker up --build
```

确认服务正常：

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/ready
```

返回 `{"status":"ok"}` 和 `{"status":"ready"}` 后，说明服务和 Worker 都已就位。

### 4. 暴露 Webhook

另开终端运行：

```bash
ngrok http 8787
```

将 GitHub App 的 Webhook URL 设置为：

```text
https://你的-ngrok-域名/hook
```

Webhook Secret 必须与 `.env.docker` 完全一致。免费 ngrok 地址重启后可能变化，地址变了记得去 GitHub App 更新，否则 GitHub 很努力，Webhook 只是找不到门。

更完整的配置、数据库检查和故障排查见 [LOCAL_DEBUG.md](LOCAL_DEBUG.md)。

## 怎么使用

### 自动 Review

安装 GitHub App 后，以下 PR 事件会自动创建任务：

- `opened`
- `reopened`
- `ready_for_review`
- `synchronize`，需要仓库策略启用 `auto_review`

Draft PR 和 Bot 创建的 PR 默认不审查。第一次开门欢迎，后续每次推送要不要继续敲门，则由仓库配置决定。

### 手动 Review

仓库成员可以在 PR 评论中输入：

```text
/review
```

也可以附带关注点：

```text
/review 重点检查权限控制、异常处理和并发安全
```

命令被接受后会收到 `eyes` reaction；成功完成后变为 `+1`。普通讨论评论不会触发 Review，也不会为了确认它不是命令而多跑一趟 GitHub API。

## 仓库级配置

将 [.ai-review.yml.example](.ai-review.yml.example) 复制到目标仓库默认分支根目录，并命名为 `.ai-review.yml`：

```yaml
enabled: true
auto_review: true
review_drafts: false
review_bot_prs: false
include_branches:
  - "*"
exclude_paths:
  - "vendor/**"
  - "dist/**"
  - "**/*.min.js"
max_files: 20
max_changed_lines: 2000
review_language: "zh-CN"
minimum_severity: "low"
```

配置读取自 PR 的 base 分支。也就是说，待审 PR 不能顺手改掉自己的规则，再宣布自己免检。

`auto_review: false` 只关闭 `synchronize` 触发的持续自动审查；PR 首次打开、重新打开和转为 Ready for review 时仍会执行一次 Review。系统级资源限制始终优先于仓库配置。

## Review 结果

一次成功的 Review 包含：

- 对本次改动的简短总结。
- 经过结构化解析和规则校验的问题列表。
- 文件路径、diff 行号、严重等级、问题说明和修改建议。
- 实际检查文件数，以及因为规模限制未检查的内容说明。

IvyBot 更偏向报告可验证、可行动的问题。纯粹的代码风格偏好不值得堵住 PR，大段泛泛而谈也只会让评论区看起来很忙。

## API 与可观测性

| 接口 | 用途 |
| --- | --- |
| `POST /hook` | 接收 GitHub Webhook |
| `GET /health` | 检查进程存活 |
| `GET /ready` | 检查 SQLite 和 Worker 是否就绪 |
| `GET /metrics` | 查看队列、Webhook 和处理指标 |
| `GET /docs` | FastAPI OpenAPI 页面 |

Webhook 接收结果包括 `accepted`、`duplicate`、`existing`、`ignored` 和 `limited`。运行日志会记录任务 ID 和失败堆栈，但不会把完整代码 Prompt 当作纪念品长期收藏。

## 数据与安全边界

- Webhook 使用 HMAC SHA-256 校验签名。
- GitHub App 使用短期 installation token 访问被授权仓库。
- 模型只接收筛选后的 PR 标题、描述、关注点和代码 patch。
- GitHub Token、App 私钥、模型密钥以及未选中的仓库文件不会发送给模型。
- SQLite 默认保存任务元数据、状态、用量和发布 ID，不保存完整 Prompt、模型原始响应或完整代码。
- 私有仓库代码仍会发送给所配置的模型供应商，部署方需要确认其训练、保留、区域和删除政策。

Prompt 注入也是代码审查场景的一部分：PR 描述、评论和源码都被视为待分析数据，而不是可以指挥系统泄露密钥或绕过规则的可信指令。

## 本地开发

项目要求 Python 3.12：

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

运行测试和编译检查：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q app
```

直接启动服务：

```bash
.venv/bin/uvicorn 'app.main:create_app_from_env' \
  --factory --host 0.0.0.0 --port 8787
```

GitHub Actions 会在 PR 创建、更新、重新打开和转为 Ready for review 时运行 Python 3.12 测试。CI 不读取 GitHub App 私钥、模型密钥或本地 `.env`。

## 项目结构

```text
app/
├── main.py       # Webhook、健康检查与依赖装配
├── events.py     # GitHub 事件到 Review 任务的转换
├── storage.py    # SQLite 持久化、幂等与任务状态
├── worker.py     # 消费、重试和失败边界
├── service.py    # Review 主业务流程
├── github.py     # GitHub App 鉴权与 API 封装
├── context.py    # diff 筛选、排序、截断与 Prompt 上下文
├── review.py     # 模型调用、结构化解析与修复重试
├── policy.py     # 仓库配置读取和校验
└── models.py     # 领域模型
```

业务逻辑通过模块边界拆开：Webhook 负责快速接收，Worker 负责可靠执行，Service 负责流程编排，GitHub 和模型只是外部适配器。这样测试时不需要真的给 GitHub 留二十条评论，也不用为了验证一个重试逻辑花掉模型额度。

## 当前边界

这是单实例、SQLite 持久化的 V1，适合个人项目、小团队和受控部署。它已经处理任务恢复、幂等、限流、过期结果和行内发布，但不打算假装自己是无限扩展的平台。

下一阶段设计见 [project-design-v2.md](project-design-v2.md)，包括多实例任务系统、更完整的管理能力和规模化运营支持。先把一件事做稳，再把它做大，通常比反过来省睡眠。

## License

当前仓库尚未声明开源许可证。使用或分发前，请先由项目维护者补充明确的 License。
