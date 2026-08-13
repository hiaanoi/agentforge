# AgentForge 项目导读

## 30 秒介绍

AgentForge 是一个面向代码修复 Agent 的耐久运行时。它关注的不是“让模型调用工具”，而是让
Agent 在人工审批、进程重启、并发争用和最终验证后，仍留下可审计、可恢复、可解释的事实。

它把模型调用、文件修改、测试执行和修复结果纳入同一条 SQLite 持久化链：命令有 receipt，
副作用有审批和内容绑定，worker 有 lease/fencing，验证有 source/verifier evidence。因而系统
可以区分已完成、失败、未知和不确定，而不是在崩溃后盲目重试。

## 三分钟演示顺序

1. 构建 wheel，并运行 [Core CLI 演示](core-demo.md)。
2. `trust --yes` 分别确认 development 与 verification profile 的精确 identity。
3. `agentforge exec` 创建 Run；看到 `edit_file` 进入 `PAUSED`，记录 approval ID。
4. 用新的 CLI 进程 `approve`，再以新的 CLI 进程 `resume` 同一 Run。
5. 对 `run_tests` 重复审批/恢复；最后一次 verification approval 通过后，展示
   `run_finished ... outcome=VERIFIED`。
6. 打开 `inspect` 与 [架构说明](architecture.md)，说明事件、receipt、lease 与验证证据都在
   durable state 中，而不是只存在于终端输出。

这条 demo 使用 Mock provider，不展示真实模型成绩；它展示的是副作用和恢复语义。

如果要把这条流程制作成 GitHub 视频，请使用[录制脚本与清单](demo-recording.zh-CN.md)。它将现场演示
和录制剪辑分开，并列出字幕、隐私审查与重录条件。

## 五个工程故事

### 1. 耐久状态，而不是只靠内存 Agent loop

问题：模型调用、工具调用和进程状态通常散落在内存里，重启后无法可靠判断“是否已经做过”。

做法：Run、event、checkpoint、provider attempt、approval、receipt 和 repair state 进入 SQLite。
命令通过 idempotency key 重放；未知外部调用不会被伪装成安全成功。

可追溯资料：[耐久审批与恢复报告](milestone_03_report.md)、
[模型与上下文报告](milestone_04_report.md)。

### 2. 审批绑定副作用，而不是“点确认后再随便执行”

问题：文件修改和测试进程是副作用；重复执行、目标漂移或内容变化都可能造成错误结果。

做法：`edit_file`/`write_file`/`run_tests` 先生成审批。mutation 绑定目标路径、旧内容、预期新
内容 hash 和 source revision；测试绑定精确 TestProfile identity。审批被消费后，执行记录仍可
持久化恢复。

可追溯资料：[安全 mutation 报告](milestone_05_report.md)、
[测试执行报告](milestone_06_report.md)。

### 3. lease 与 fencing 解决跨进程争用

问题：一个进程卡死或网络/调度延迟时，另一个进程可能接管同一 Run；旧 worker 若继续写入会
破坏新状态。

做法：Run lease 使用 fencing token。每个 owned write 都验证当前 authority；陈旧 authority 被
拒绝。A2 `AgentApplication` 还将 Start/Resume 命令的 owner/observer、过期接管和 receipt
终结持久化，避免两个 CLI process 重复驱动同一副作用。

可追溯资料：[架构说明](architecture.md)、[Core CLI 演示](core-demo.md)。

### 4. `VERIFIED` 是证据结论，不是普通“测试通过”

问题：测试命令若在可变 workspace 或外部路径中运行，结果无法说明实际验证了什么。

做法：verification profile 与 source/verifier capsule 绑定 digest、purpose、source revision 和
process evidence；前后完整性检查失败或证据不完整时 fail closed。`VERIFIED` 仅在 repair policy、
workspace diff 和最终 verification 都满足时产生。

可追溯资料：[修复评测报告](milestone_07a_report.md)、[安全模型](security_model.md)。

### 5. 评测先保证统计真实性，再讨论模型效果

问题：少量成功样本、未完成 slot 或基础设施故障容易被误写成模型“通过率”。

做法：AgentForge 使用冻结 protocol、planned/executed/valid/scored/replacement slot、失败分类和
denominator-correct `pass@k`。离线 Pilot 复用真实 runtime 链；real-model Study 基础设施保留
显式授权、成本与公开 artifact 边界。

可追溯资料：[评测指南](evaluation_guide.md)、[7-B2.4 报告](milestone_07b2_4_report.md)。

## 按时间预算阅读

### 2 分钟：判断项目是否值得深入

阅读根目录 [README](../README.md) 的“它解决什么问题”“三分钟 Core CLI 演示”“当前边界”。

### 10 分钟：理解产品能否恢复和验证

运行或阅读 [Core CLI 演示](core-demo.md)，再看 [架构说明](architecture.md) 的 approval、mutation
和 test execution flow。

### 30 分钟：核对工程深度和证据边界

阅读 [安全模型](security_model.md)、[评测指南](evaluation_guide.md)、
[7-A 报告](milestone_07a_report.md) 与 [7-B2.4 报告](milestone_07b2_4_report.md)。

## 诚实边界

- `NON_HERMETIC` verification runtime 是完整性证据方案，不是 OS 级沙箱；同账户高权限外部
  进程不在该威胁模型内。
- Core demo 是本地 Mock workflow，不代表真实模型、通用修复能力或官方 benchmark 成绩。
- 历史 real-model canary/Study 只按各自冻结 manifest 和报告解释；未完成 schema-v2 study 不产生
  新的 headline score。
- 真实模型运行需要单独授权、凭据和成本预算；不得公开 API key、私有 manifest、隐藏测试、
  verifier 路径或原始 provider 输出。
- 当前产品形态是 CLI；持久化多轮 chat/REPL 与面向公开发布的完整证据包仍属于后续 B/C 范围。
