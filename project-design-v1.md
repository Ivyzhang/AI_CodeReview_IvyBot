# GitHub AI Code Review 服务设计文档（V1）

## 1. 文档目的

本文档定义 GitHub AI Code Review 服务 V1 的产品范围、系统架构、核心流程、模块职责、异常处理、安全边界和业务验收标准。

V1 是可独立部署和使用的正式版本。它通过 GitHub App 接入用户仓库，在 Pull Request 发生变化或收到 `/review` 命令时调用大模型分析代码变更，并将结构化 Review 结果写回 GitHub。

V1 采用单实例、SQLite 持久任务表和单 Worker，以较低的基础设施复杂度完成可靠的业务闭环。Webhook 返回 `202` 前，Delivery 和任务必须在同一事务中持久化；服务重启后能够恢复未完成任务。模块边界允许后续替换数据库、调度方式和上下文来源，但 V1 不提前实现多实例等 V2 能力。

## 2. 目标用户与使用场景

V1 面向希望在 GitHub 仓库中获得自动代码审查的个人开发者和中小型研发团队。仓库管理员安装 GitHub App 并选择授权仓库后，开发者继续在原有 Pull Request 工作流中使用服务，无需进入独立管理后台。

核心场景包括：

- PR 首次进入可审查状态时自动检查本次代码变更。
- PR 推送新 commit 后，取消旧版本结果并审查最新 head SHA。
- 仓库成员通过 `/review` 指定安全、性能或某项业务逻辑作为额外关注点。
- Review 结果以低噪声方式集中发布，并在位置失效时安全降级。

## 3. 项目目标

V1 需要完成以下目标：

- 用户能够通过安装 GitHub App 将仓库接入服务。
- PR 创建、重新打开、转为 Ready 或推送新提交时能够自动触发 Review。
- 有权限的仓库成员能够通过 `/review` 手动触发 Review，并附加关注点。
- 服务能够获取 PR 元数据和文件变更，构造受约束的 Review 上下文。
- 大模型输出必须经过结构和 diff 位置校验后才能写回 GitHub。
- 多个发现通过一次 GitHub PR Review 批量提交，减少通知噪声。
- 行内位置失效或提交失败时，发现仍能通过 Review 正文或普通 PR 评论保留。
- 任务必须绑定触发时的 head SHA，避免旧结果写入已更新的 PR。

## 4. 非目标

V1 不包含以下能力：

- 分布式部署和多 Worker 调度。
- Redis、PostgreSQL 或外部消息队列。
- 完整仓库检出、语义索引和跨文件调用链检索。
- 多轮 Agent 工具调用或沙箱执行。
- 多策略并行 Review 和跨策略结果去重。
- 管理后台、完整计费系统和复杂租户套餐。
- 由 AI 直接批准、拒绝或阻断 PR 合并。

这些能力属于 V2 的演进范围，不影响 V1 的独立交付。

## 5. 技术选型

| 领域 | V1 方案 |
| --- | --- |
| 服务语言 | Python |
| Web 框架 | FastAPI |
| GitHub 接入 | GitHub App |
| GitHub 凭证 | 短期 Installation Token |
| 持久化 | SQLite |
| 后台任务 | SQLite 任务表和单 Worker |
| 幂等去重 | SQLite 唯一约束和事务 |
| PR 上下文 | GitHub Pull Request API 与 PR Files API |
| 模型接口 | OpenAI-compatible Chat Completions API |
| 数据校验 | Pydantic 结构化模型 |
| HTTP 客户端 | httpx |

Fine-grained PAT 仅用于本地开发或受控内部调试，不作为正式用户接入方式，也不能与 GitHub App 正式模式同时启用。

## 6. 系统架构

```text
GitHub
  │
  │ Webhook
  ▼
Webhook Router
  ├─ 验证签名
  ├─ 解析事件
  ├─ 过滤触发条件
  └─ Delivery 与业务幂等
  │
  ▼
SQLite Task Store ──> Review Worker
                 │
                 ├─ 获取 Installation Token
                 ├─ 执行前校验 head SHA
                 ├─ 获取 PR 与 Files
                 ├─ 构造 Review 上下文
                 ├─ 调用 Review Engine
                 ├─ 校验模型发现
                 ├─ 发布前再次校验 head SHA
                 └─ 幂等发布 Review 或安全降级
                         │
                         ▼
                       GitHub
```

Webhook 请求只负责可信接收和任务持久化，不在请求生命周期内执行模型调用。Delivery 和任务写入 SQLite 成功后才能返回 `202`。所有耗时操作由后台 Worker 完成。

## 7. 项目模块

建议采用以下模块结构：

```text
app/
├── main.py
├── config.py
├── models.py
├── webhook/
│   ├── signature.py
│   ├── events.py
│   └── router.py
├── storage/
│   ├── database.py
│   ├── migrations.py
│   ├── task_store.py
│   └── installation_store.py
├── github/
│   ├── auth.py
│   └── client.py
├── review/
│   ├── context.py
│   ├── prompt.py
│   ├── engine.py
│   ├── validation.py
│   └── publisher.py
└── worker.py
```

### 7.1 Webhook Router

职责：

- 接收 GitHub Webhook。
- 使用原始请求体和 Webhook Secret 验证 `X-Hub-Signature-256`。
- 读取 `X-GitHub-Delivery` 和 `X-GitHub-Event`。
- 将受支持的事件转换为统一 `ReviewTask`。
- 在单个事务中完成 Delivery 去重、业务幂等判断和任务持久化。
- 事务提交后立即返回。

该模块不能调用模型或执行完整 Review。

### 7.2 Task Store

`TaskStore` 定义任务创建、获取、状态流转、恢复和发布记录协议。V1 使用 SQLite 实现，由单个 Worker 顺序领取 `queued` 任务。

Webhook 事务写入失败时返回 `503`，不记录 Delivery，使 GitHub 后续重试能够重新提交。服务启动时，超过执行超时仍处于 `running` 的任务恢复为 `queued`；因此 V1 提供至少一次任务执行语义，并通过 Review 级幂等避免重复发布。

SQLite 数据库文件必须位于持久卷，不能放在容器临时文件系统。数据库启用外键、事务和适合单实例并发读写的日志模式；部署过程必须先停止旧 Worker 或确认任务执行超时后再启动新实例，V1 不允许两个实例同时消费同一 SQLite 文件。

### 7.3 Delivery 与 Review 幂等

GitHub Delivery ID 在 SQLite 中具有唯一约束，用于阻止同一次 Webhook 重投重复创建任务。

自动 Review 的业务幂等键为：

```text
installation_id + repository_id + pull_number + head_sha + automatic
```

`opened`、`reopened`、`ready_for_review` 和 `synchronize` 针对同一 head SHA 最终只保留一个自动任务。

手动 Review 的业务幂等键为：

```text
installation_id + repository_id + pull_number + head_sha
+ manual + sha256(normalized_focus)
```

关注点通过去除首尾空白、合并连续空白后计算摘要。相同 head SHA 和相同关注点在冷却期内复用已有任务；不同关注点允许创建独立任务，但仍受单 PR 冷却和 Installation 费用限制。Review 正文包含业务幂等标记，发布重试前同时检查本地发布记录和 GitHub 已有 Review，处理“GitHub 已成功但本地请求超时”的不确定状态。

### 7.4 GitHub Credential Provider

职责：

- 使用 GitHub App ID 和私钥签发 App JWT。
- 根据任务中的 `installation_id` 获取短期 Installation Token。
- 确保 Token 不写入日志和错误响应。

每个任务使用其所属 Installation 的凭证访问仓库，不能跨 Installation 复用权限。

### 7.5 GitHub Client

封装以下 GitHub API：

- 获取 PR 元数据。
- 分页获取 PR 变更文件。
- 创建包含总结和行内评论的 PR Review。
- 创建普通 PR 评论，用于失败降级和用户触发失败反馈。
- 查询当前 head SHA 和已有 Review 幂等标记。
- 为手动命令添加状态 reaction。

业务模块不直接拼接 GitHub API 请求。

### 7.6 Review Context Provider

职责：

- 汇总 PR 标题、描述、base/head 分支和 head SHA。
- 获取变更文件状态、增删行数和 patch。
- 为 patch 标注新文件侧行号。
- 为每个文件生成可评论的 RIGHT side 行号集合。
- 应用仓库排除规则、文件优先级、文件数和单文件字符数限制。
- 合并 `/review` 命令附带的关注点。

二进制文件或缺少 patch 的文件保留文件元数据，但不生成可评论行。

### 7.7 Review Engine

职责：

- 构造系统指令、输出 Schema、PR 元数据和变更内容。
- 明确 PR 文本和代码是不可信数据，不能作为系统指令执行。
- 调用 OpenAI-compatible 模型接口。
- 从响应中提取 JSON 并校验为 `ReviewResult`。
- 首次输出无效时追加一次格式修复请求。

Review 只关注本次展示的变更，优先输出高置信的缺陷、安全风险、回归、性能问题和缺失的业务测试，不输出表扬或纯风格意见。

### 7.8 Finding Validator

每条模型发现必须满足：

- `path` 属于本次展示的 PR 变更文件。
- `line` 是该文件 patch 中可评论的 RIGHT side 行。
- `severity` 为 `high`、`medium` 或 `low`。
- `body` 包含具体问题及其影响。

文件或行号无效的发现不会丢弃，而是转入 Review 正文中的非行内发现区域。

### 7.9 Review Publisher

职责：

- 将总结和有效行内评论组装为一次 GitHub PR Review。
- 使用触发任务绑定的 commit SHA 提交 Review。
- 将无效位置的发现附加到 Review 正文。
- 发布前重新获取 PR head SHA；与任务 SHA 不一致时将任务标记为 `superseded`，不发布 Review，也不降级为普通评论。
- 仅当 head SHA 仍一致、Installation 仍有效，但 GitHub 拒绝行内位置时，将全部发现降级为普通 PR 评论。
- 写入发布记录并使用幂等标记避免重复 Review。

V1 始终使用 `COMMENT` 事件，不使用 `APPROVE` 或 `REQUEST_CHANGES`。

### 7.10 Review Worker

Worker 编排任务的完整执行过程，但不实现各模块内部逻辑：

1. 获取任务所属 Installation 的短期 Token。
2. 查询 PR 当前状态和 head SHA。
3. 若当前 head SHA 与任务中的值不同，结束过期任务。
4. 获取 PR 文件并构造 Review 上下文。
5. 调用 Review Engine。
6. 校验模型发现。
7. 发布前重新查询 PR head SHA；不一致则标记过期并停止。
8. 检查 Review 幂等状态并发布结果。
9. 记录完成、过期或失败状态。

## 8. 核心数据模型

### 8.1 ReviewTask

```text
id: string
idempotency_key: string
installation_id: integer
repository_id: integer
owner: string
repo: string
pull_number: integer
head_sha: string
trigger_mode: automatic | manual
focus: string
user_initiated: boolean
status: queued | running | completed | superseded | failed
attempt_count: integer
created_at: timestamp
started_at: timestamp | null
finished_at: timestamp | null
```

`head_sha` 在 Webhook 事件转换任务时确定。`focus` 必须归一化并限制长度。`idempotency_key` 具有唯一约束。

### 8.2 Finding

```text
path: string
line: positive integer
severity: high | medium | low
body: non-empty string
```

### 8.3 ReviewResult

```text
summary: non-empty string
comments: Finding[]
```

### 8.4 PublishedReview

```text
task_id: string
idempotency_marker: string
github_review_id: integer | null
github_comment_id: integer | null
publish_mode: inline_review | fallback_comment
published_at: timestamp
```

单次 Review 的发现数量必须受上限约束。发布记录与任务一一对应。

## 9. GitHub App 配置清单

### 9.1 安装和回调

- GitHub App 可以安装到个人或组织账号，并允许管理员选择全部仓库或指定仓库。
- Webhook URL 指向服务公开的 HTTPS `/hook`。
- Webhook Secret 必须使用高强度随机值，并与服务配置一致。
- V1 不使用 GitHub 用户 OAuth 登录，因此不要求 User authorization callback URL。
- 如配置安装完成后的 Setup URL，应指向产品的安装结果页；没有该页面时可以不配置，不能将其误作 Webhook URL。
- 私有仓库只有在安装时明确授权后才能访问，服务不得尝试读取未授权仓库。

### 9.2 Repository permissions

采用最小权限：

| 权限 | 级别 | 用途 |
| --- | --- | --- |
| Metadata | Read-only | 识别仓库基础信息；GitHub App 默认需要 |
| Contents | Read-only | 读取仓库内 `.ai-review.yml`；为后续相关文件读取保留一致权限边界 |
| Pull requests | Read and write | 读取 PR、变更文件并创建 PR Review |
| Issues | Read and write | 读取 `/review` 评论、发布降级评论和添加状态 reaction |

V1 不申请 Administration、Checks、Workflows 或代码写权限。部署前应在 GitHub App 设置页再次核对各 REST API 显示的细粒度权限要求；若 GitHub 调整权限映射，以官方接口说明为准，并继续保持最小权限。

### 9.3 Webhook events

订阅：

- Pull request
- Issue comment
- Installation
- Installation repositories

`installation` 和 `installation_repositories` 用于同步安装、暂停、卸载及仓库授权变化。Installation 被暂停、删除，或仓库被移除后，相关未开始任务标记失败，运行中任务在发布前停止，不能写回 GitHub。

## 10. GitHub 事件和任务状态

### 10.1 自动 Review

支持 `pull_request` 的 `opened`、`reopened`、`ready_for_review`，以及由仓库策略控制的 `synchronize`。Draft PR 默认不触发。机器人或 Dependabot PR 是否触发由仓库策略决定。

### 10.2 手动 Review

`issue_comment.created` 只有满足以下条件时创建任务：

- 评论所属 Issue 是 PR。
- 正文以 `/review` 开头。
- `author_association` 为 `OWNER`、`MEMBER` 或 `COLLABORATOR`。
- 发送者不是 Bot。

接受手动任务后在命令评论上添加 `eyes` reaction。成功发布后添加 `+1`；任务过期或被合并时添加 `confused` 并回复简短原因；失败时回复可操作的错误信息。相同幂等键已在排队、执行或完成时，不创建新任务，并反馈已有状态。

### 10.3 Webhook 响应

| 场景 | 响应 |
| --- | --- |
| 验签失败 | `401` |
| 缺少 Delivery ID 或 JSON 无效 | `400` |
| 不支持或不满足条件的事件 | `202 ignored` |
| 重复 Delivery | `202 duplicate` |
| 重复业务任务 | `202 existing` |
| 事务提交成功 | `202 accepted` |
| SQLite 不可写或事务失败 | `503` |

## 11. 仓库配置协议

仓库可以在默认分支根目录提供 `.ai-review.yml`。服务始终读取 PR base 分支上的配置，不能让待审 PR 通过修改自身配置绕过限制。缺少文件时使用默认策略；语法或字段非法时，自动任务停止并记录可观测错误，手动任务向用户反馈配置错误。

支持的业务策略包括：

```yaml
enabled: true
auto_review: true
review_drafts: false
review_bot_prs: false
include_branches: ["*"]
exclude_paths:
  - "vendor/**"
  - "dist/**"
  - "**/*.min.js"
max_files: 20
max_changed_lines: 2000
review_language: "zh-CN"
minimum_severity: "low"
```

系统级安全上限始终优先于仓库配置。仓库配置只能缩小资源范围，不能提升系统最大 Token、超时、评论数或费用限制。

## 12. Review 上下文和截断

### 12.1 文件选择

文件选择必须确定且可解释：

1. 应用仓库 `exclude_paths`，并排除已识别的生成文件、vendor 内容和二进制文件。
2. 将认证、授权、密钥、网络入口、数据查询、依赖清单和 CI 配置归入安全敏感优先级。
3. 其余源代码和业务测试优先于文档、资源和纯格式文件。
4. 同一优先级按变更行数降序，再按路径字典序稳定排序。
5. 按最大文件数、最大变更行数和输入 Token 预算依次截断。

Review 正文必须显示“已检查 X/Y 个文件”，并说明其余文件因排除规则、无 patch、文件上限、行数上限或 Token 预算未检查。总结不能将部分检查描述成完整 PR 覆盖。

### 12.2 上下文内容

模型输入包含仓库、PR、base/head、关注点、覆盖情况，以及每个选中文件的状态、增删行数和标注行号后的 patch。

patch 中新增行和上下文行可作为 RIGHT side 评论位置；删除行不可评论。多个 hunk 分别按各自的新文件起始行计算。

### 12.3 模型输出

模型只返回符合 `ReviewResult` 的 JSON。首次校验失败允许修复一次，第二次仍失败则任务失败。不存在的文件或无效行号不能生成行内评论。

## 13. 成本和滥用控制

V1 不实现计费系统，但必须具备最小资源治理：

- 同一 PR 同一 head SHA 同时最多运行一个任务，后续任务排队并执行幂等合并。
- 相同手动关注点在冷却期内不重复执行。
- 每个 Installation 设置每日模型调用软预算；达到后停止新自动任务，手动命令收到明确反馈。
- 限制单任务输入 Token、输出 Token、文件数、变更行数和评论数。
- 模型请求设置连接超时、响应超时和任务总时限。
- Webhook 请求体、命令关注点和模型修复次数均受限。
- 新 head SHA 到达后，旧排队任务立即标记 `superseded`；运行中任务在阶段边界和发布前检查状态。

默认限制集中由服务配置管理，仓库策略只能在允许范围内收紧。限制值应基于灰度数据调整，不在设计文档中固化为未经验证的容量承诺。

## 14. 发布和降级策略

发布顺序必须固定：

1. 查询 PR 当前 head SHA 和 Installation 状态。
2. SHA 不一致时将任务标记 `superseded`，丢弃生成结果，不发布任何 Review 或普通评论。
3. 检查本地发布记录和 GitHub Review 幂等标记；已发布时直接完成任务。
4. SHA 一致且未发布时，批量提交总结、有效行内评论和非行内发现。
5. 只有 SHA 仍一致、权限仍有效，但 GitHub 明确拒绝行内位置时，才降级为普通 PR 评论。
6. 网络超时等结果不确定错误先查询幂等标记，再决定重试，不能直接降级。

普通评论降级必须保留全部发现，并说明行内位置不可用。权限撤销、PR 关闭或任务过期均不允许降级写回。

## 15. 安全、隐私和数据治理

### 15.1 服务安全

- 所有 Webhook 验签后再解析业务事件，并使用恒定时间比较签名。
- GitHub App 私钥通过 Secret 文件或 Secret Manager 提供。
- Installation Token、App JWT、模型密钥和 Webhook Secret 不写入日志。
- PR 标题、描述、评论和代码均是不可信输入，不能覆盖系统指令。
- AI Review 只使用 `COMMENT`，不改变合并规则。

### 15.2 发送给模型的数据

外部模型服务只接收第 12 节选中的 PR 元数据、关注点和代码 patch，不接收 GitHub Token、用户邮箱、Installation 私钥或未选中的仓库文件。私有仓库代码同样会发送给配置的模型供应商，安装管理员必须在安装前看到这一事实和数据范围。

### 15.3 保留和删除

- SQLite 保存任务元数据、状态、用量和 GitHub 发布 ID，不默认保存完整 Prompt、模型原始响应或完整代码。
- 应用日志不记录完整代码和 Prompt，并设置明确的滚动和保留期限。
- 卸载 GitHub App 后停止新任务，并在规定期限内删除该 Installation 的任务元数据。
- 运营方必须在隐私说明中披露模型供应商、数据处理区域、保留策略、删除渠道，以及供应商是否会使用输入训练模型。
- 只有声明不将 API 输入用于模型训练且满足项目数据区域要求的供应商才能用于正式环境。

具体保留天数和数据区域属于部署合规参数，必须在上线前确定并对安装管理员公开。

## 16. 健康检查、状态和指标

- `/health` 只表示进程存活，不访问外部依赖。
- `/ready` 检查 SQLite 可读写、Worker 心跳和必要外部配置是否可用；不满足时返回非成功状态。
- `/metrics` 提供任务总数、排队深度、失败数、过期数、处理耗时、模型用量和发布结果，不暴露仓库代码或密钥。
- 自动任务失败默认不在 PR 中制造评论噪声，但必须进入失败指标和结构化日志。
- 手动任务通过 reaction 和简短回复展示接受、重复、完成、过期和失败状态。

## 17. 产品成功指标与灰度

V1 上线后至少衡量：

- Review 成功发布率。
- 从事件接收到发布完成的 P50 和 P95 时间。
- 任务过期率和失败率。
- 有效行内评论占全部发现的比例。
- 被开发者解决或采纳的发现比例。
- 被标记为重复、误报或无帮助的比例。
- 单次 Review 平均输入/输出 Token 和模型成本。

灰度顺序为内部仓库、少量自愿试用仓库、受限公开安装。每一阶段先确认任务可靠性、成本和误报指标，再扩大 Installation 数量。指标阈值应在内部基线形成后确定，不在缺少真实数据时伪造目标值。

## 18. 业务测试与验收

测试只验证产品业务逻辑，不为配置加载、配置文件格式、目录结构、数据库迁移文件、测试文件、框架初始化或无业务价值的实现细节设置测试点。

### 18.1 事件、权限和状态

- 支持的 PR 事件生成绑定 Installation、PR 和 head SHA 的任务。
- Draft、无关事件、非协作者命令和 Bot 命令按策略忽略。
- 手动任务的接受、重复、完成、过期和失败产生正确用户反馈。
- Installation 暂停、卸载或仓库移除后不再创建或发布 Review。

### 18.2 持久化和幂等

- Webhook 返回 `202` 后重启服务，任务仍能继续执行。
- 相同 Delivery 只创建一个任务。
- 针对同一 head SHA 的不同自动事件合并为一个自动任务。
- 相同手动关注点在冷却期内复用已有任务，不同关注点可独立执行。
- 发布请求超时后通过幂等标记确认结果，不产生重复 Review。

### 18.3 任务时效

- 执行前 SHA 已变化时不调用模型、不发布结果。
- 模型调用期间 SHA 变化时，发布前检查将任务标记过期且不发布任何评论。
- SHA 未变化但行内位置被 GitHub 拒绝时才执行普通评论降级。

### 18.4 上下文和模型结果

- 文件按排除、优先级、变更量和稳定路径顺序确定性选择。
- Review 正文准确报告已检查和未检查文件。
- patch 的新增、上下文和删除行生成正确的可评论集合。
- 合法模型结果进入发布流程；无效结构只修复一次。
- 错误文件和行号不能生成行内评论，但有效问题内容得到保留。

### 18.5 限制和发布

- 冷却、并发合并、Token 上限和 Installation 软预算能阻止重复或超额执行，并给手动用户明确反馈。
- 多个有效发现通过一次 Review 批量提交。
- 无效位置的发现进入 Review 正文。
- 权限撤销、PR 关闭和过期任务不执行普通评论降级。

## 19. V2 扩展点

| V1 接口 | V1 实现 | V2 演进 |
| --- | --- | --- |
| `TaskStore` | SQLite 单 Worker | PostgreSQL、分布式队列和多 Worker |
| Delivery/Review 幂等 | SQLite 唯一约束与 GitHub 标记 | 跨实例事务、锁和发布协调 |
| Installation Store | SQLite 基础状态 | 完整租户、仓库和策略管理 |
| `ReviewContextProvider` | PR patch 和确定性选择 | 仓库工作区与相关文件检索 |
| `ReviewEngine` | 单轮模型调用 | 多策略 Review 和结果合并 |
| 可观测性 | 单实例指标和日志 | 集中指标、追踪、告警和运营查询 |

业务主流程在 V2 中保持不变：可信触发、持久任务、上下文构建、模型判断、结果校验和受控写回。

## 20. V1 完成标准

- GitHub App 权限和事件按第 9 节配置，并可安装到授权仓库。
- Webhook 接受的任务在进程重启后不丢失。
- Delivery、业务任务和发布三个层级的幂等规则有效。
- 自动和手动 Review 可以端到端运行并提供约定状态反馈。
- 执行前和发布前两次 SHA 校验阻止旧结果写回。
- 仓库策略、文件选择、资源限制和数据披露行为明确。
- 模型输出和 GitHub 行内位置经过强制校验。
- 只有满足降级条件时才发布普通评论，且不丢失有效发现。
- 业务测试覆盖第 18 节关键行为。
- 灰度阶段能够采集第 17 节产品指标。
