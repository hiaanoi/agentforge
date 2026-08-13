# AgentForge 秋招产品化设计

## 状态

设计已由用户逐部分确认，并通过独立架构审计，可进入实施计划。

目标岗位是 Agent 应用开发岗。首个求职版本把现有 AgentForge Runtime
产品化为一个可安装、可演示、可恢复、可审计的代码修复 Agent，而不是继续扩展成
通用多 Agent 平台。

## 目标

招聘者应能在三分钟内理解并运行以下主路径：

```text
输入一个带缺陷的代码仓库
  -> Agent 读取与搜索代码
  -> 提出受控修改
  -> 用户审批
  -> 执行可见测试并读取结构化反馈
  -> 修正代码
  -> 执行绑定源码版本的最终验证
  -> 输出 diff、成本、审计事实和 VERIFIED 结论
```

AgentForge 的卖点不是“工具数量多”，而是把 Agent 应用最容易被忽略的可靠性问题
做成可运行证据：审批、幂等、恢复、租约与 fencing、源码版本绑定、故障注入和诚实的
未知状态。

## 产品定位

### 目标用户

- 第一用户：秋招面试官和技术招聘者。
- 第二用户：希望在本地代码仓库上运行受控修复流程的开发者。
- 非目标用户：需要云端多租户 Agent 平台、任意 Shell 或大规模并行执行的团队。

### 产品承诺

- 给定一个已配置的项目，Agent 可以分析、修改并验证代码。
- 写入和测试等副作用受 Profile、审批和持久化事实约束。
- 进程退出后，可以在明确安全的边界恢复原 Run。
- 当 Provider 或进程副作用无法确定时，系统返回 `INDETERMINATE`，不伪造成功，
  不盲目重放。
- 每个公开结论都能追溯到 Run、Event、Profile、源码摘要和验证记录。

### 非目标

- 不实现任意 Shell、OS 级 Sandbox 或容器平台。
- 不实现固定 planner/executor、多 Agent 协作工作流。
- 不在首版实现 Web 服务、全屏 TUI、团队协作或云端多租户。
- 不承诺 Provider exactly-once、任意中断点恢复或生产就绪。
- 不把内部 curated fixtures 表述为官方 SWE-bench 成绩。
- Anthropic、MCP 和长期记忆不属于秋招首版发布门槛。

## 调研结论

调研对象包括 Pico、learn-claude-code、mini-swe-agent、SWE-agent、OpenHands SDK、
OpenAI Codex、Cline 和 Kode。

共同启示：

- Agent 核心循环应保持简单，复杂性主要位于工具协议、上下文、权限、恢复和产品外壳。
- 一个深的应用入口比 CLI 直接拼装 repositories、tools 和 Runtime 更容易测试和演进。
- 面试作品需要能安装、能演示、能复现，而不能只展示内部架构。
- AgentForge 已有 Runtime、审批、测试 Profile、持久化和评测基础，优势是 durable
  runtime；主要缺口是公开产品入口、会话模型、跨进程所有权、发布证据与易用性。

参考项目：

- [Pico](https://gitee.com/htxoffical/pico)
- [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)
- [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)
- [SWE-agent](https://github.com/SWE-agent/SWE-agent)
- [OpenHands SDK](https://github.com/OpenHands/software-agent-sdk)
- [OpenAI Codex](https://github.com/openai/codex)
- [Cline](https://github.com/cline/cline)

## 产品架构

```text
CLI Adapter
    |
    v
AgentApplication
    |---- Conversation product model
    |---- existing AgentRuntime
    |---- existing ModelProvider seam
    |---- existing TestProfile / execution chain
    `---- Product Event and View projection
```

### CLI Adapter

CLI 只负责：

- 参数与配置来源解析；
- 调用 `AgentApplication`；
- 渲染事件和视图；
- 维护稳定退出码；
- 将 Ctrl-C 转换为明确的用户选择或进程退出。

CLI 不直接访问 repository，不装配 Runtime，不决定恢复策略，也不解释内部异常。

### AgentApplication

`AgentApplication` 是产品单一入口和深模块。它隐藏：

- command 幂等；
- Conversation 与 Run 的映射；
- Runtime 装配；
- lease 获取、续租和 fencing；
- 持久事件到产品事件的投影；
- 敏感信息分级；
- 稳定错误码和退出码映射。

对外保留两个接口：

```python
async def stream(
    command: ApplicationCommand,
    *,
    after_cursor: int | None = None,
) -> AsyncIterator[ProductEvent]: ...
def query(query: ApplicationQuery) -> ProductView: ...
```

`after_cursor` 是传输重连参数，不进入 command request digest。相同 command 重试时，
Application 根据 Receipt 找到原 `run_id` 或 `conversation_id`，不重复执行 command，只从
持久化事实中重放 cursor 之后的事件，或按 Receipt 状态重新附着/安全驱动原 command。

### Conversation 与 Run

- Conversation 是用户概念，保存连续交互历史。
- Turn 表示一次用户输入及其唯一执行关系。
- Run 是 Runtime 的执行事实，继续作为审批、工具、测试、恢复和审计的边界。
- Conversation 不创建第二套 Agent loop。

## 应用契约

### Commands

- `StartConversation`
- `StartRun`
- `SubmitMessage`
- `ResumeRun`
- `DecideApproval`
- `TrustProfile`
- `CancelRun`

所有 command 都包含 `command_id`；用户消息另有 `client_message_id`。

相同幂等键和相同请求返回原结果。相同幂等键和不同请求返回稳定的
`IDEMPOTENCY_CONFLICT`。

命令映射：

```text
agentforge exec -> StartRun
agentforge chat -> StartConversation；每条用户消息 -> SubmitMessage
```

`StartConversation` 只创建 Conversation，不创建 Run。`StartRun` 与 `SubmitMessage` 是
创建 Run 的两个入口。`TrustProfile` 是 Run 创建之前的项目级安全决策，CLI 不得直接写
信任表。

### Queries

- `ConversationHistory`
- `RunDetails`
- `PendingApprovals`
- `DoctorReport`
- `ProfileTrustDetails`
- `ExportRunDetails`

`DoctorReport` 只执行无副作用的本地检查，不返回密钥、原始异常或不必要的敏感路径。

Query 使用判别式类型映射返回精确 View。`RunDetails` 返回本地敏感的
`LocalRunDetailsView`；`ExportRunDetails` 返回 allowlisted `ExportRunDetailsView`。
导出是显式 Query，不使用 `safe=False` 一类布尔开关。

### Product Events

产品事件从统一的持久化 EventLog 投影，不建立第二套事实日志。现有 Run-only Event 表
演进为 scope-aware log：

```text
scope_type = run | conversation | workspace
scope_id
run_id nullable
run_sequence nullable
global_cursor non-null
```

Run 事件保留 Run 内序号；Conversation 与 workspace 事件只依赖全局 cursor。Profile
trust 使用 workspace scope。每个产品事件至少包含：

- `schema_version`
- `event_id`
- `scope_type` 和 `scope_id`
- 可选 `run_id`
- 全局 `cursor`
- 可选的 Run 内 `sequence_number`
- `occurred_at`
- allowlisted payload

后台执行 Runtime，stream 按全局 cursor 增量读取持久化 Event。关闭 iterator 不隐式
取消 Run；取消必须通过 `CancelRun` 并写入审计事实。

canonical 映射规则：

- `verification_completed` 只来自 final verification 终态事实；
- `run_finished` 只来自 Run 的 completed、failed 或 cancelled 终态事实；
- 测试工具的 generic event 与 testing event 不重复投影成两个完成事件；
- conversation、approval decision 和 resume 产生独立产品事件，即使没有模型步骤。

Receipt 至少保存 command type、request digest、状态和 result reference。所有 command 的
Receipt 与该 command 的首个持久化效果原子提交：

- `StartRun`、`SubmitMessage`：Receipt 与完整 Run bundle 原子创建；
- `StartConversation`：Receipt 与 ConversationRow 原子创建；
- `DecideApproval`：Receipt 与审批 CAS 和 Event 原子提交；
- `ResumeRun`：Receipt 与 lease 获取和恢复意图原子提交；
- `TrustProfile`：Receipt 与 Profile trust 决策原子提交。

Receipt 状态机：

```text
ACCEPTED -> IN_PROGRESS -> COMPLETED
                        -> FAILED
                        -> INDETERMINATE
```

相同 command 重试不得创建第二个 Run。已完成 command 只重放持久事件；未完成 command
可以重新附着到现有 executor，在 `CREATED/PREPARED` 等安全状态下重新驱动同一个持久化
command，或返回 `PAUSED/UNKNOWN` 并要求新的 `ResumeRun`。Run bundle 已提交但后台任务
尚未启动时，`CREATED` 是可以安全开始原 Run 的阶段，不允许形成永久孤立 Run。

### Error Contract

`ApplicationError` 包含：

- `code`
- `safe_message`
- `retryable`
- `exit_code`
- `error_id`
- 可选 `run_id`

错误由固定 catalog 生成，未知异常映射为通用安全消息，禁止把 `str(exc)` 直接暴露给
CLI。

## 持久化模型与不变量

新增或明确以下产品记录：

- `ConversationRow`
- `ConversationTurnRow`
- `ConversationMessageRow`
- `ApplicationCommandReceiptRow`
- `RunLeaseRow`
- `TrustedProfileRow`
- workspace/source/config/profile bindings
- schema version record

### 原子创建

一个产品 Run 的初始事务必须共同创建：

- Run 与 RepairState；
- `RUN_CREATED` Event；
- ModelRuntimeState；
- workspace/source binding；
- profile/config digest binding；
- CommandReceipt 和幂等关系。

崩溃后只允许“没有 Run”或“一个完整 Run”，不允许孤儿 Run 或没有初始事实的 Run。

为使该事务边界可落地，增加 `AgentApplication` 内部的 `ApplicationUnitOfWork`：

```text
ApplicationUnitOfWork
  - session-bound Run / Repair / Model initialization
  - session-bound bindings and CommandReceipt operations
  - transaction-scoped EventLog.append(...)
  - commit / rollback
```

现有 repositories 和 workflows 逐步支持接收同一个 Session；不得复制为一套平行
repository。`RunCreationWorkflow` 使用这个 Unit of Work 完成整个 bundle，而不是顺序调用
多个各自提交事务的公开方法。

### Conversation 唯一性

至少建立以下唯一约束：

- `(conversation_id, client_message_id)`；
- 每个 user message 只能属于一个 Turn；
- 每个 Turn 只能固定一个 Run；
- assistant final message 的 `source_run_id` 唯一。

消息按稳定 ordinal 排序，不依赖墙钟时间。

### Lease 与 Fencing

`RunLeaseRow` 包含：

- `owner_id`
- `lease_token`
- 单调 `fencing_token`
- `version`
- `acquired_at`
- `heartbeat_at`
- `expires_at`

获取、续租、释放使用 CAS。Run 状态写入、Event 追加、Checkpoint、side-effect claim 和
终态写入都校验 fencing token。租约过期不自动证明外部副作用已经停止。

executor 在安全暂停点释放执行 lease。`DecideApproval` 获取短生命周期 command lease，
原子提交审批 CAS、Receipt 和 Event 后释放；`ResumeRun` 再获取执行 lease。active executor
持有 lease 时，外部 cancel 只能写 control request，不能直接写 `RUN_CANCELLED`。旧 owner
的后续关键写入全部失败。

### Source Revision Binding

绑定用户待修项目，而不是 AgentForge 自身源码。版本链区分：

- `initial_source_digest`：Run 创建时的基线；
- `expected_source_digest`：当前合法期望版本；
- `source_revision_number`：每次已提交合法 mutation 后单调递增。

规则：

1. 每个步骤开始前要求 `actual == expected`；
2. mutation 绑定 `before_digest == expected`；
3. mutation 成功后以 fencing/CAS 原子推进 expected digest 和 revision；
4. `actual != expected` 才表示外部或人工漂移；
5. final verification 绑定开始时 expected digest，验证前、执行记录、验证后和当前摘要
   必须一致。

Git HEAD 是基线元数据，不作为 Agent 产生未提交修改后的当前版本。绑定字段包括：

- 解析后的 workspace root identity；
- Git HEAD（存在时）；
- 工作树内容摘要；
- 配置摘要；
- Profile 摘要。

在 execute、resume、副作用开始前和最终验证前重新校验。发现非预期改动时，在模型调用
或写文件前 fail closed。

源码摘要算法必须版本化并固定：include/exclude 规则、`.git`、`.agentforge` 和构建产物
处理、symlink 行为、路径规范化与排序、换行和大小写规则、大文件上限以及读取失败时的
fail-closed 行为。Windows/Linux 对同一逻辑源码应产生可解释、可复现的结果。

文件系统写入与 SQLite 事务不能原子提交，因此 source revision chain 必须联结既有
mutation journal：

```text
PREPARED  before_digest + expected_after_digest 已持久化
WRITING   文件可能未写、已写或部分写
COMMITTED mutation record 与 expected_source_digest 在同一数据库事务推进
```

从 `WRITING` 恢复时：

- `actual == before_digest`：确认尚未生效，可在新 fence 下安全继续；
- `actual == expected_after_digest`：确认写入已完成，可在新 fence 下 finalize 并推进
  revision；
- 两者都不等：转 `INDETERMINATE/UNKNOWN`，禁止继续或覆盖现场。

### Event Cursor 与 Schema

- 全局 cursor 由数据库安全分配，用于跨 Run streaming 和分页。
- Run-local sequence 保留作为单 Run 内排序，但不能继续通过无保护的
  `max(sequence)+1` 并发分配。
- 首版不必构建通用迁移框架，但必须有 schema version、空库初始化以及不兼容版本的
  fail-closed 行为。

所有 Event 写入必须收口到 transaction-scoped `EventLog.append`，一次完成 global cursor
分配、适用 scope 的写入权限校验、Run-local sequence 分配和 Event 插入。Run 可保存
`next_event_sequence` 并通过 CAS 推进；禁止 workflows 各自执行 `max(sequence)+1`。

EventLog 使用判别式写入 authority：

```text
RunCreationAuthority
RunLeaseAuthority(fencing_token)
ConversationCommandAuthority(conversation_version, command_receipt)
WorkspaceCommandAuthority(workspace_identity, command_receipt)
```

- 初始 `RUN_CREATED` 使用与 Run bundle 同事务的 creation authority；
- 执行期 Run 事件使用 Run lease fencing；
- Conversation 事件使用 conversation version 与 Receipt；
- Profile trust 使用 workspace identity 与 Receipt；
- active cancel 先写 control row 与 Receipt，不允许外部进程冒充 executor 写 Run 终态。

## Agent 行为与工具

### 主循环

继续使用单模型主循环。模型基于任务、上下文和工具结果决定下一步，不增加固定
planner/executor 或多 Agent 工作流。

### 首版工具

- `list_files`
- `read_file`
- `search`
- `get_git_diff`
- `git_status`
- `git_log`
- `write_file`
- `edit_file`
- `run_profile`

`git_status` 和 `git_log` 是固定参数、只读工具，复用现有受控进程执行器。

不提供 arbitrary shell。模型调用 `run_profile` 时只传 `profile_id`。

### Command Profiles

产品文案可以称为 Command Profiles，但实现继续复用并深化现有
`TestProfileRegistry`、execution coordinator、bindings 和 process records，不建立
平行的 Profile 体系。

Profile 增加 purpose：

- `development`
- `verification`
- `utility`，首版可不开放

项目级 Profile 第一次启用时必须显式信任，展示解析后的 executable、argv、cwd、配置
来源和 Profile digest。持久化信任记录只保存必要 identity、版本和摘要，不保存 secret
env 或不必要的原始参数。

`ProfileTrustDetails` 查询产生 pre-run trust challenge，`TrustProfile` command 完成决策。
任何 CLI 子命令都不得绕过 `AgentApplication` 直接修改 `TrustedProfileRow`。

执行保持 `shell=False`。

### Provider

复用现有：

```python
ModelProvider.generate(ModelRequest) -> ModelResponse
```

首个产品版本只实现 OpenAI 纵切；Mock Provider 用于确定性测试和离线 Demo。Anthropic
延后，不创建仅为展示而存在的新 `complete()` 抽象。

Provider attempt journal 使用以下持久状态：

```text
PREPARED -> DISPATCHING -> COMPLETED
                       -> FAILED
                       -> INDETERMINATE
```

`PREPARED` 表示确定尚未尝试发送，可以安全继续。调用 Provider 之前先提交
`DISPATCHING`；若进程从 `DISPATCHING` 恢复，只能判断请求可能已发送，必须转为
`INDETERMINATE`，不得自动重放。避免使用 `SENT` 命名，因为本地数据库不能证明远端
已经收到请求。

### Resume 语义

首版只承诺在可以证明安全的边界恢复：

- 等待审批；
- `CREATED/PREPARED`：尚未开始外部副作用，可以安全开始；
- 测试终态已持久化：可以安全消费已有结果；
- 已有确定终态事实的步骤。

测试处于 `STARTED` 且无法证明进程树已经终止时，只能投影 `UNKNOWN`，不可重新执行。

Provider 请求已发送但响应未持久化时标记 `INDETERMINATE`，不自动重发，也不声称不会
重复计费。两个进程不得同时 resume 同一个 Run。

### 公开状态投影

生命周期与结果使用两个正交字段：

```text
lifecycle_status:
  CREATED | RUNNING | PAUSED | TERMINAL

outcome_status:
  VERIFIED | UNVERIFIED | FAILED | UNKNOWN | null
```

`RUNNING + null` 表示尚无结果；`PAUSED + UNVERIFIED` 表示等待审批且尚未最终验证；
`TERMINAL + UNKNOWN` 表示 Provider、进程或副作用结果不可确定。终态必须有非空 outcome。
`INDETERMINATE` 和无法确认已终止的进程都投影为 `UNKNOWN`，不能折叠为普通 `FAILED`。

## CLI 与用户体验

首版使用普通终端流式输出，不实现全屏 TUI。

最终命令集合：

```text
agentforge chat
agentforge exec
agentforge resume
agentforge inspect
agentforge doctor
agentforge approvals
agentforge approve
agentforge reject
agentforge cancel
```

最小发布纵切可以先交付除 `chat` 和完整 `cancel` 外的命令。

### 配置优先级

```text
CLI flags
  > project .agentforge/config.toml
  > user config
  > environment
  > safe defaults
```

API key 只从环境变量或系统凭据读取，不写入项目配置、数据库、Event 或 artifact。

### 审批与退出码

- `exec` 遇到审批时暂停，输出 approval ID、Run ID 和稳定退出码。
- `approve` 或 `reject` 可以由另一个进程执行。
- `resume` 恢复原 Run，不创建新 Run。
- Core Release 中 Ctrl-C 只提供继续等待或 detach；关闭 stream 本身不取消 Run。
- Interactive Release 完成协作取消状态机后，Ctrl-C 才可以写入取消请求。
- 第二次强制退出只表示 CLI 离开，不伪造 Runtime 已取消。

### 敏感信息分级

- `LocalProductView` 可以包含本地用户和 assistant 正文，并明确标记为敏感。
- 默认 CLI 只展示任务所需的最小安全内容。
- `ExportView` 严格 allowlist 和 redaction，不包含 raw prompt、API key、完整工具参数、
  不必要的绝对路径或消息正文。
- Product Event payload 永远使用 export-safe allowlist；消息正文只通过显式的本地敏感
  View 获取。

## 评测与证据

证据分为三层，禁止混合分母或合并成一个成功率。

### A. Deterministic Harness Regression

使用 Mock Provider 验证协议、状态机和工具链，目标是稳定 12/12。它证明工程回归，
不证明真实模型修复能力。

### B. Deterministic Fault Injection

八个 canonical crash windows：

1. approval decision 提交前；
2. approval decision 提交后；
3. mutation `PREPARED`；
4. mutation `WRITING`；
5. test `STARTED`；
6. Provider 请求持久化后、发送前；
7. Provider 已发送、响应持久化前；
8. lease takeover 与 stale writer。

对 Provider 已发送但未持久化的窗口，正确结果是 fail closed 的未知状态，不是
exactly-once。

### C. Real-model Curated Portfolio

选择四个明确冻结的任务实例，每个实例三次独立运行：

- QuixBugs；
- BugsInPy；
- cropped SWE-bench task；
- 一个自建 durable recovery task。

这是求职作品的 curated matrix，不是通用 benchmark。baseline 与 candidate 固定：

- commit/source/dependency；
- Provider 和精确 model snapshot；
- prompt、tool schema、context policy；
- Profile、hidden verifier、预算和停止条件；
- 平台和价格信息。

baseline/candidate 交错运行以降低 Provider 时间漂移，只做 task-paired descriptive
comparison，不声称统计显著。

### 指标

对每任务 `n=3`、成功数 `c`：

```text
pass@1 = c / 3
pass@3 = 1 - C(3-c, 3) / C(3, 3)
first_attempt_success = 第一槽是否成功
any_success_in_3 = c > 0
majority_success = c >= 2
stable_success = c == 3
```

只有三个有效 repetition 全部完成时才计算 pass@3。报告同时给出：

- planned slots；
- executed slots；
- valid/invalid/scored；
- replacement；
- unexecuted；
- success/scored；
- conservative success/planned。

小样本延迟使用 median 和 range，并区分端到端时间与 Provider 时间。

旧的顺序敏感 `pass@1` artifact 必须重新生成或标记为 legacy。

## 产品验收与发布证据

发布门槛分为两层。

### Core Release Gate

- A0、A1、A2 和 C-lite 完成；
- fresh venv 安装 wheel 和 console script；
- `exec/inspect/doctor/approvals/approve/reject/resume` 的 CLI contract、退出码及
  stdout/stderr；
- 跨进程审批与恢复；
- inspect schema、public artifact scan、Windows/Linux CI；
- deterministic regression、三个核心 failpoint 和可追溯的 real-model report。

Core Release 不包含 chat 和 active-process cancel。Ctrl-C 只允许 detach。

### Interactive Release Gate

- B 完成；
- `chat`、`cancel` 和所有 slash commands 的 CLI contract；
- Conversation 幂等、跨进程恢复和协作取消状态机；
- active process 的取消结果诚实区分 `REQUESTED/CANCELLED/INDETERMINATE`。

完整产品验收最终包括：

- fresh venv 安装 wheel；
- console script；
- 全部 CLI contract、退出码及 stdout/stderr；
- 跨进程审批与恢复；
- inspect schema 稳定性；
- public artifact secret scan；
- Windows 和 Linux 实际 CI；
- offline deterministic regression；
- fault injection；
- 完整且可追溯的 real-model report。

复杂任务成功不是发布门槛；真实失败必须保留并解释。

## Demo 与求职材料

### 三分钟主 Demo

```text
输入 buggy repo
 -> Agent 搜索定位
 -> 修改审批
 -> visible test 失败
 -> 结构化反馈进入上下文
 -> Agent 二次修改
 -> final verification
 -> 展示 diff、成本、审计链和 VERIFIED
```

### 九十秒恢复 Demo

```text
运行到审批边界
 -> 退出 CLI
 -> 新进程列出 approval
 -> approve
 -> resume 原 Run
 -> 完成且 mutation 只发生一次
```

### 发布材料

- 中英文 README；
- architecture 和 run-recovery 图；
- 五分钟 quickstart；
- 固定 Demo fixture 和脚本；
- real-model report 与原始事实；
- 与 Pico/其他 harness 的边界对比；
- 失败案例与安全限制；
- STAR 项目描述；
- 面试追问题库。

## 实施路线图

### A0：Evidence Truth，1–2 天

- 标准 pass@k 与报告 schema；
- legacy artifact 标记；
- 文档数字纠偏；
- 单一 evidence summary 生成链。

独立验收：公式、分母、不完整实验和公开 artifact 一致性测试全部通过。

### A1：Durable Product Kernel，7–12 天

- 配置、workspace、Profile 和错误码；
- schema version、CommandReceipt、source binding、Event cursor；
- `ApplicationUnitOfWork` 和统一 `EventLog`；
- 原子 Run 创建；
- lease/fencing；
- Provider attempt journal；
- execute/resume/verification 校验；
- 三个 Core Release failpoint：
  1. approval decision 已提交、客户端确认前；
  2. mutation `WRITING`；
  3. lease takeover 与 stale writer。

Provider `DISPATCHING` 是 A1 强烈建议的第四个 failpoint，最迟在完整 C 中成为发布门槛。

独立验收：原子创建、双进程竞争、stale writer 拒绝、source drift 拒绝、审批恢复。

### A2：AgentApplication + Minimal CLI，4–6 天

- 深 `AgentApplication`；
- 共享 Runtime 装配；
- `exec/inspect/doctor/approvals/approve/reject/resume`；
- wheel 和 console script；
- Windows/Linux 基础离线 CI；
- OpenAI 单 Provider 纵切。

独立验收：fresh install 后完成跨进程审批修复 Demo。

### B：Interactive Conversation，4–7 天

- Conversation/Turn/Message；
- chat REPL 和 slash commands；
- 会话幂等和跨进程恢复；
- 显式本地敏感视图；
- 协作 cancel 状态机。

独立验收：多轮会话在审批点退出后由新进程恢复并完成。

### C：Evidence Release

C-lite 预计 3–5 天，包含 Core Release 所需的三个核心 failpoint、双平台 CI、主 Demo、
恢复 Demo 和真实失败报告。完整 C 另需 5–8 天及真实模型运行时间。

- 完整八个 failpoint；
- 4 tasks × 3 runs；
- 完整双平台 CI；
- artifact、Demo、文档、STAR 和面试材料。

独立验收：一条离线命令从冻结的原始运行事实重新生成并校验完整证据报告；该命令不
执行真实模型。主 Demo 和恢复 Demo 可稳定复现。

### D：Stretch

- Anthropic；
- 一个 MCP adapter；
- 长期项目记忆。

D 不计入秋招发布门槛，也不与 A–C 并行启动。

## 时间不足时的裁剪顺序

最佳最小发布包是 Core Release：`A0 + A1 + A2 + C-lite`。

若时间进一步不足：

1. 保留 A0，错误指标不能带入求职材料；
2. 保留 A1 的原子创建、source binding、lease/fencing 和审批恢复；
3. A2 只保留主 Demo 必需命令；
4. C 保留双平台 CI、三个核心 failpoint、真实失败报告和两段 Demo；
5. 简化 B；
6. 删除 D。

## AgentForge 与 Pico 的产品差异摘要

| 维度 | Pico | 当前 AgentForge | 本设计完成后 |
|---|---|---|---|
| 核心定位 | 小型、易理解的 Agent/harness | durable Runtime 与 evaluator | 可靠代码修复 Agent 产品 |
| Agent 循环 | 强调最小闭环和可读性 | 已有模型、工具、审批状态机 | 保留单循环，经 `AgentApplication` 对外 |
| 工具执行 | 轻量、产品路径直接 | 受控工具和测试 Profile 较强 | 增加可信配置与公开 CLI |
| 持久化恢复 | 轻量或非主卖点 | Run、Event、Checkpoint 已较深入 | 增加幂等、原子创建、lease/fencing |
| 审批安全 | 可见交互能力 | 审批绑定是现有优势 | 跨进程审批与可证明安全的 resume |
| 用户体验 | 更接近可直接体验的 harness | 当前缺少公共产品入口 | wheel、CLI、chat、inspect、doctor |
| 评测证据 | 不是 AgentForge 的同类重点 | evaluator 丰富但指标有历史债务 | 三层证据、标准 pass@k、故障注入 |
| 求职叙事 | 简洁、容易快速理解 | 工程深但像后台研究项目 | 先展示工具闭环，再展示可靠性深度 |

相同点是都保留简单 Agent loop、工具调用和代码任务闭环；核心不同点是 AgentForge 把
审批、持久化、验证绑定和崩溃恢复做得更深。AgentForge 当前相对 Pico 的主要差距不是
Runtime 能力，而是产品入口、交互体验、可信发布和三分钟内可理解的证据链。

## 设计决策摘要

- 产品是“可靠代码修复 Agent”，不是通用 Agent 框架展示。
- CLI 是薄 Adapter，`AgentApplication` 是深模块。
- Conversation 是产品模型，Run 是执行事实。
- 复用现有 Runtime、TestProfile、process records 和 ModelProvider。
- 单模型循环、受控工具、无 arbitrary shell。
- OpenAI first，Mock 用于测试。
- 幂等、原子创建、source binding、lease/fencing 和 Event cursor 是首版内核。
- 只恢复可证明安全的阶段，未知副作用保持 `INDETERMINATE`。
- 评测分层、使用标准 pass@k、公开失败和完整分母。
- 秋招优先完成可信纵切和证据，不堆 Provider、MCP 与 Memory logo。
