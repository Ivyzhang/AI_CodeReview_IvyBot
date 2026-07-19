# GitHub AI Code Review 服务设计文档（V2 规划）

## 1. 文档目的

本文档定义 GitHub AI Code Review 服务从 V1 演进到 V2 的生产化设计。V2 继承 V1 的产品目标和业务主流程，通过替换基础设施、增强 Review 上下文和完善任务治理，使系统能够支持多用户、多仓库、多实例和持续运营。

V2 不是独立重写。V1 中的 Webhook、任务、GitHub 凭证、上下文、Review Engine 和 Publisher 接口继续作为系统边界，V2 在这些边界内替换或组合具体实现。

## 2. 演进目标

V2 需要在保留 V1 使用方式的前提下实现：

- 将 V1 的 SQLite Installation、仓库和任务状态迁移到支持多实例的 PostgreSQL。
- 支持多服务实例和多 Worker 并发执行。
- 保证 Webhook 幂等、任务可恢复和结果只针对最新 head SHA。
- 支持任务取消、分类重试、退避和失败追踪。
- 从 PR patch 扩展到受控仓库工作区和相关上下文检索。
- 将单轮 Review 扩展为多个明确职责的 Review 策略。
- 对跨策略重复发现进行合并，并继续验证文件与 diff 位置。
- 提供任务耗时、模型 Token、费用、失败原因和队列状态的可观测性。
- 扩展 V1 的仓库级 Review 规则、资源限制和费用预算。

## 3. 继承范围

V2 保持以下 V1 行为不变：

- 通过 GitHub App 安装接入仓库。
- 支持 PR 自动 Review 和 `/review` 手动 Review。
- Webhook 必须验签并快速响应。
- Review 任务绑定 head SHA。
- 执行前和发布前均校验 head SHA，过期结果不以任何形式写回。
- 模型输出必须结构化并经过服务端校验。
- 多个发现通过一次 Review 批量提交。
- 只有 head SHA 仍一致时，行内位置不可用才允许保留为非行内结果。
- AI Review 默认使用非阻断的 `COMMENT`。

V2 不改变用户命令和 GitHub PR 中的主要交互方式。

## 4. V2 非目标

V2 不包含：

- 自动修改代码并向用户分支推送提交。
- 自动批准、拒绝或合并 PR。
- 通用软件开发 Agent 或任意命令执行平台。
- 与 GitHub PR Review 无关的代码托管平台适配。
- 无限制的完整仓库内容进入模型上下文。

## 5. 总体架构

```text
                         ┌────────────────────┐
GitHub Webhook ─────────>│ Webhook API        │
                         │ 验签/解析/幂等     │
                         └─────────┬──────────┘
                                   │
                                   ▼
                         ┌────────────────────┐
                         │ Persistent Queue   │
                         └─────────┬──────────┘
                                   │
                     ┌─────────────┴─────────────┐
                     ▼                           ▼
              ┌─────────────┐             ┌─────────────┐
              │ Review      │             │ Review      │
              │ Worker      │             │ Worker      │
              └──────┬──────┘             └──────┬──────┘
                     └─────────────┬─────────────┘
                                   ▼
                         ┌────────────────────┐
                         │ Review Pipeline    │
                         │ Context/Strategies │
                         │ Merge/Validation   │
                         └─────────┬──────────┘
                                   ▼
                         ┌────────────────────┐
                         │ GitHub Publisher   │
                         └────────────────────┘

Shared Services:
- PostgreSQL：安装、仓库、任务、执行和发布状态
- Redis/Queue：任务分发、幂等、锁和短期缓存
- Workspace Storage：受控仓库工作区
- Observability：日志、指标、追踪和告警
```

## 6. 核心组件

### 6.1 Webhook API

沿用 V1 的验签、事件解析、Delivery 幂等和业务幂等职责，并将 SQLite 事务实现替换为跨实例持久化实现：

- 使用持久化幂等记录原子接受 Delivery。
- 记录原始事件的必要元数据和接收状态。
- 更新 Installation 和仓库的可用状态。
- 创建任务后向持久化队列发布任务标识。

Webhook API 不执行仓库检出和模型调用。

### 6.2 Installation Registry

从 V1 的基础安装状态扩展为完整 GitHub App 安装关系：

- Installation ID、Account 和状态。
- 已授权仓库及其启用状态。
- 安装创建、权限变更、仓库增删和卸载事件。
- 仓库级 Review 策略与资源限制的关联。

卸载或权限撤销后，未开始任务应取消，运行中任务不得继续写回 GitHub。

### 6.3 Persistent Task Queue

V2 的任务系统需要支持：

- 任务持久化和服务重启恢复。
- 多 Worker 竞争消费。
- 可见性超时或租约，避免 Worker 崩溃后任务永久丢失。
- 有界重试和指数退避。
- 延迟重试和死信状态。
- 同一 PR 最新任务优先和旧任务取消。

队列消息只携带任务 ID。任务完整数据和状态存储在数据库中，避免消息成为唯一事实来源。

### 6.4 Task Orchestrator

负责 Review 任务的状态流转：

```text
queued
  -> running
  -> collecting_context
  -> reviewing
  -> publishing
  -> completed

异常分支：retry_scheduled | superseded | cancelled | failed
```

状态更新必须具备并发保护。只有持有当前任务执行租约的 Worker 可以推进任务。

### 6.5 Workspace Manager

为 Review 任务创建受控仓库工作区：

- 使用 Installation Token 获取目标 commit。
- 仅检出任务绑定的 base/head 或必要历史。
- 限制仓库大小、文件大小、检出时间和磁盘占用。
- 任务结束后按策略清理或复用只读缓存。
- 禁止执行仓库中的脚本、Hook 和任意命令。

工作区用于读取上下文，不自动运行不可信代码。

### 6.6 Context Retrieval

V2 在 V1 PR patch 基础上补充高价值上下文：

- 变更文件的完整内容。
- 直接导入、调用或被调用的相关文件。
- 与变更对应的业务测试。
- 仓库级 Review 规则。
- PR 当前未解决的 Review 线程，避免重复反馈。

检索必须受到文件类型、路径、字符数和 Token 预算约束。无法证明相关性的文件不进入模型上下文。

### 6.7 Review Strategy Pipeline

V2 将 Review 拆分为职责明确的策略，例如：

- Correctness：业务错误、边界条件和回归。
- Security：输入信任边界、权限和敏感数据风险。
- Performance：明显的复杂度、资源和阻塞问题。
- Test Coverage：变更涉及的业务行为是否缺少必要测试。

策略根据变更内容和仓库规则选择，不要求每个 PR 固定执行全部策略。每个策略继续输出统一的 `ReviewResult`，不能直接调用 GitHub Publisher。

### 6.8 Finding Merger

多个策略完成后，Finding Merger 负责：

- 合并指向相同文件、相近位置和相同根因的发现。
- 保留更具体、证据更完整的描述。
- 统一严重程度。
- 应用单次 Review 评论数量上限。
- 将无法可靠定位但仍有价值的发现转入总结。

结果合并后仍必须通过 V1 定义的路径和 diff 行号校验。

### 6.9 Review Publisher

沿用 V1 的批量 Review、发布前二次 SHA 校验和受限普通评论降级协议，并增加：

- 发布前再次确认 Installation 和仓库处于启用状态。
- 发布前再次确认 PR head SHA 与任务一致。
- 记录 GitHub Review ID、Comment ID 和发布结果。
- 通过幂等键避免重试造成重复 Review。
- 区分可重试 GitHub 错误和永久权限错误。

head SHA 不一致时必须标记 `superseded` 并停止，不能发布普通评论。只有 SHA 一致且 GitHub 明确拒绝行内位置时才能降级。

## 7. 数据模型

### 7.1 Installation

```text
id
github_installation_id
account_login
account_type
status
permissions_snapshot
created_at
updated_at
```

### 7.2 Repository

```text
id
installation_id
github_repository_id
owner
name
default_branch
status
review_policy
created_at
updated_at
```

### 7.3 Delivery

```text
delivery_id
event_type
repository_id
received_at
accepted
task_id
```

`delivery_id` 具有唯一约束，用于跨实例幂等。

### 7.4 ReviewTask

```text
id
idempotency_key
repository_id
installation_id
pull_number
head_sha
trigger_mode
focus
user_initiated
status
attempt_count
superseded_by
lease_owner
lease_expires_at
created_at
started_at
finished_at
```

### 7.5 ReviewRun

```text
id
task_id
strategy
model
input_tokens
output_tokens
duration_ms
status
error_category
created_at
```

模型原始 Prompt 和完整响应不默认持久化；确需诊断时应采用脱敏、访问控制和保留期限。

### 7.6 PublishedReview

```text
id
task_id
github_review_id
github_comment_id
publish_mode
finding_count
created_at
```

## 8. 任务幂等与并发

V2 同时处理三类幂等：

### 8.1 Delivery 幂等

数据库对 GitHub Delivery ID 建立唯一约束。重复 Webhook 返回已接受状态，不重复创建任务。

### 8.2 PR 版本幂等

同一仓库、PR、head SHA 和触发类型只能存在一个有效自动任务。手动 `/review` 可以创建独立任务，但相同 Delivery 仍只能接受一次。

### 8.3 发布幂等

发布记录与任务绑定。Worker 重试前先确认是否已经成功创建 Review，避免重复通知。

当同一 PR 到达新 head SHA：

- 尚未开始的旧任务标记为 `superseded`。
- 正在执行的旧任务收到取消信号，并在阶段边界检查。
- 即使取消未及时生效，发布前的 head SHA 校验仍阻止旧结果写回。

## 9. 重试和错误分类

### 9.1 可重试错误

- GitHub 或模型服务的限流响应。
- 网络超时和临时连接错误。
- GitHub 5xx 或模型服务 5xx。
- Worker 异常退出导致租约过期。

可重试错误使用带抖动的指数退避，并限制最大次数和任务总时长。

### 9.2 不可重试错误

- Installation 已卸载或仓库授权已撤销。
- PR 不存在或已关闭。
- 任务 head SHA 已过期。
- 仓库或文件超过明确的安全限制。
- 模型在格式修复后仍无法返回合法结构。

### 9.3 发布降级

行内 Review 位置失效时仍沿用 V1 的普通评论降级前置条件。head SHA 已变化、权限已撤销或 PR 已关闭时不能尝试降级写回，只记录明确失败原因。

## 10. 仓库级策略

V2 继承 V1 的 `.ai-review.yml` 协议，并扩展仓库维度的 Review 策略：

- 是否在 `synchronize` 时自动复审。
- 启用的 Review 策略。
- 忽略的文件路径或生成文件模式。
- 最大文件数、上下文 Token 和评论数量。
- 自定义 Review 规则文件路径。
- 每日或每月模型费用预算。

仓库规则只能约束 Review 行为，不能覆盖系统安全限制。

## 11. 安全设计

V2 继承 V1 的 Webhook 验签、短期 Token、输入不可信和资源上限要求，并增加：

- Installation 与仓库访问必须按租户隔离。
- 私钥和模型密钥由 Secret Manager 管理并支持轮换。
- 数据库中的敏感字段加密或避免持久化。
- 工作区按任务或租户隔离，禁止执行仓库代码。
- 外部请求限制目标域名，避免上下文或模型触发任意网络访问。
- Prompt、模型响应和代码内容采用最短必要保留策略。
- 管理操作和策略变更记录审计日志。
- 限流同时覆盖 Installation、仓库、PR 和全局资源。

## 12. 可观测性

### 12.1 指标

- Webhook 接收、拒绝、重复和入队数量。
- 队列深度、等待时间、运行时间和重试数量。
- Review 完成、过期、取消和失败数量。
- GitHub API 与模型 API 的延迟和错误率。
- 每个任务和策略的输入、输出 Token 与估算费用。
- 行内评论有效率和普通评论降级率。

### 12.2 日志

日志使用 Delivery ID、Task ID、Installation ID、仓库和 PR 编号串联请求。日志不得包含 Token、密钥、完整代码或未脱敏 Prompt。

### 12.3 告警

以下情况需要告警：

- Webhook 验签失败率异常升高。
- 队列持续积压或任务等待超过阈值。
- GitHub 或模型服务错误率持续升高。
- 任务失败率、重试耗尽或发布降级率异常。
- 模型费用接近全局或租户预算。

## 13. 业务测试与验收

测试只覆盖产品业务逻辑和跨组件业务契约，不为配置文件、迁移文件、目录结构、测试文件或框架初始化设置测试点。

### 13.1 安装和仓库状态

- GitHub App 安装、仓库授权变更和卸载事件正确更新可用状态。
- 已卸载 Installation 或已移除仓库不能创建或发布 Review。
- 不同 Installation 的任务不能使用彼此的仓库权限。

### 13.2 持久任务与幂等

- 多实例接收相同 Delivery 时只创建一个任务。
- Worker 中断后，租约到期的任务能够被其他 Worker 恢复。
- 相同任务的发布重试不会产生重复 Review。
- 新 head SHA 到达后，旧任务被标记过期或取消且不能发布。

### 13.3 上下文检索

- Review 上下文包含与变更直接相关的文件和业务测试。
- 无关文件、忽略路径和超出资源限制的内容不会进入上下文。
- 仓库规则能够影响 Review 关注点，但不能突破系统限制。
- 未解决线程中的已有发现不会被无意义重复发布。

### 13.4 多策略 Review

- 系统根据变更和仓库策略选择正确的 Review 策略。
- 不同策略输出通过统一 Schema 校验。
- 相同根因的重复发现被合并。
- 合并结果仍遵守文件、diff 行号和评论数量限制。

### 13.5 重试、取消和降级

- 临时 GitHub 或模型错误按策略重试并最终成功或进入明确失败状态。
- 永久权限错误不进行无效重试。
- 任务取消能在阶段边界停止后续模型调用或发布。
- head SHA 一致但行内位置失效时，所有发现仍能降级保留；SHA 变化时不写回。

### 13.6 资源和费用约束

- Installation、仓库和全局限流能够阻止超额任务继续消耗资源。
- 达到费用预算后自动任务停止，系统保留可解释状态。
- 单任务上下文、模型调用次数和总执行时间受业务限制。

## 14. 从 V1 到 V2 的迁移顺序

V2 建议按以下顺序实施，每一步保持现有 Review 主流程可用：

1. 将 SQLite Schema 迁移到 PostgreSQL，保持 Installation、仓库、Delivery、任务和发布记录语义不变。
2. 将 SQLite 单 Worker 领取替换为持久化队列和 Worker 租约，保持现有业务幂等键不变。
3. 将单实例幂等实现升级为跨实例事务、锁和发布协调。
4. 将旧任务的阶段性过期检查升级为主动取消和最新 head SHA 调度。
5. 引入受控仓库工作区和相关上下文检索。
6. 将单轮 Review Engine 扩展为策略流水线和 Finding Merger。
7. 扩展仓库级规则、限流和费用预算管理。
8. 完善集中指标、追踪、告警和运营状态查询。

在 SQLite 数据和幂等语义完成 PostgreSQL 等价迁移前，不应开始多 Worker 水平扩展；在工作区隔离和资源限制完成前，不应启用完整仓库上下文。

## 15. V2 完成标准

V2 在满足以下条件时可以交付：

- GitHub App 安装和仓库状态可持久管理。
- Webhook、任务和发布在多实例环境中保持幂等。
- Worker 故障不会永久丢失已接受任务。
- 新 PR 提交能够取消或阻止旧任务发布。
- Review 能使用受控的相关仓库上下文，不执行不可信代码。
- 多策略结果能够合并、校验并通过一次 Review 发布。
- 重试、失败、取消、Token、费用和队列状态可观测。
- 仓库级规则、限流和费用预算能够约束业务执行。
- 业务测试覆盖本文档第 13 节定义的关键行为。
