# AgentForge mini-native 修复引擎设计

## 背景与目标

50 个 SWE-bench Verified 公开任务的同模型配对评测显示：AgentForge 当前
`mini_linear` 修复循环为 21/50 resolved（42%），官方 mini-SWE-agent 为
31/50（62%）。AgentForge 的持久化、审批、审计、恢复和证据链有独立价值，
但当前修复循环缺少 mini-SWE-agent 已验证的 shell、测试反馈和多轮修正能力。

本设计的目标是在保留 AgentForge 控制平面的前提下，内嵌 mini-SWE-agent 的
最小核心修复循环，并使每个模型调用、工具动作、审批、测试、checkpoint 和
最终补丁都能被 AgentForge 接管和审计。最终验收目标是同一冻结 50 题协议下
达到至少 32/50 resolved，超过当前 mini-SWE-agent 的 31/50。

## 非目标

- 不复制 mini-SWE-agent 的 CLI、文档、无关 benchmark 和发布脚本。
- 不改变 AgentForge 的既有持久化 schema、审批语义、lease/recovery 机制。
- 不把 AgentForge 内部 `COMPLETED` 当作 SWE-bench 成功；官方 harness 仍是
  唯一的修复能力裁判。
- 不在第一阶段增加新的模型、训练流程或检索系统。

## 架构

新增 `mini_native` repair engine，放在现有 `agentforge.repair_engines` 下。
它内嵌 mini-SWE-agent 的核心 agent loop、上下文压缩、shell/file/test 工具
和模型调用，但通过 AgentForge 定义的 host callbacks 执行动作：

```text
AgentForge Runtime
  ├─ Run / lease / checkpoint / recovery
  ├─ ApprovalWorkflow + MutationPolicy
  ├─ EventLog + telemetry
  ├─ MiniNativeRepairEngine
  │    ├─ model loop
  │    ├─ shell / read / edit / test tools
  │    └─ context compaction
  └─ candidate patch + official harness exporter
```

mini-SWE-agent 上游快照固定为当前评测使用的 commit
`25941c89cfbc91eb40b3f8756348c91d9977d57e`，并在 vendored 目录中保留
上游许可证和来源说明。核心代码通过小型 adapter 使用 AgentForge 的运行时
接口，不让 vendored 代码直接写 SQLite 或绕过审批。

## 动作与审批流程

每个工具动作统一转换为 `RepairAction`，至少包含 action id、run id、工具名、
参数摘要、工作目录、读写分类和父模型调用 id。

- 只读 shell、文件读取、目录列表：自动执行并写入事件。
- 文件写入、删除、移动、会改变 checkout 的 shell：创建 mutation approval，
  只有批准后执行。
- 测试命令：允许在受控 workspace 内执行，记录命令、退出码、stdout/stderr
  摘要和耗时；测试失败作为下一轮模型上下文，而不是立即终止。
- 每次动作完成后保存 checkpoint；进程或 API 中断时从最后一个可验证边界恢复。
- 最终 candidate publish 只有在最后一轮 patch 与测试状态一致时允许；否则保留
  `UNVERIFIED_FINAL`，但仍导出可复现 patch 供 harness 评分。

策略层不能因为工具名是 `bash` 就一律拒绝。它必须解析动作的实际路径和效果：
读操作自动放行，写操作审批，越界或控制目录写入拒绝。这样既保留安全边界，
又不破坏模型的正常 shell 工作流。

## 预算与模型

mini-native 使用协议中的模型、温度、最大步骤、最大模型请求数和 wall time，
不另设隐藏预算。每次请求记录 provider request id、prompt/completion/total
tokens（若 relay 返回），并将缺失 telemetry 显式标记为 unavailable。AgentForge
与 mini-SWE-agent 的比较继续使用同一模型、同一任务顺序、同一 Docker 镜像和
同一官方 harness。

## 兼容与迁移

- 保留 `mini_linear`，新增 `mini_native`，通过 repair engine 选择项切换。
- 既有 native/mini_linear 单测不改变语义。
- vendored mini 的依赖只增加到 evaluation/repair-engine extra，不污染核心运行时。
- 上游 commit、许可证、变更摘要和 adapter 边界写入 `THIRD_PARTY_NOTICES.md`
  或相邻说明文件。

## 验证策略

### 单元与集成

- action adapter：读/写/测试分类和参数脱敏。
- approval bridge：写动作必须暂停，批准后恢复同一 run。
- checkpoint/recovery：模型调用、工具调用或进程中断后不丢失 run id、事件和
  workspace 状态。
- tool protocol：合法 bash/read/edit/test 动作不产生协议错误；越界写入仍拒绝。
- telemetry：有 usage 时保存 token，无 usage 时保存明确 unavailable。
- product integration：真实 AgentForge Application + MiniNative engine 完成一
  个审批、测试失败、再修正、candidate publish 的闭环。

### 评测门槛

1. 先在冻结 50 题中选择固定 10 题 canary，使用同一模型和 Docker harness。
2. canary 必须满足：0 个工具协议失败、空补丁不超过 1 题、resolved 不低于
   mini-SWE-agent 在同 10 题上的结果。
3. 只有 canary 通过才运行完整 50 题；完整验收要求至少 32/50 resolved，且
   官方 harness infrastructure failure 为 0。
4. 任一门槛失败，停止继续堆控制平面功能，优先分析逐题轨迹和失败分类。

## 风险与取舍

- vendored 代码会增加维护成本，但比进程外适配器更容易接管每个工具动作，符合
  “控制平面 + 修复循环”目标。
- 审批可能降低速度，因此默认只对 mutation 设置 approval，不阻塞读和测试。
- mini telemetry 可能仍不完整；这不阻止能力评测，但成本结论必须继续标记为
  未归一化。
- 如果 mini-native 在相同任务上仍低于 31/50，结论应是 AgentForge 控制平面
  能力成立、修复器仍不达标，不再把简化循环作为主修复实现。

## 交付物

- `mini_native` repair engine 和 host callback adapter。
- vendored 上游来源与许可证说明。
- 单元、集成和 10 题 canary 结果。
- 完整 50 题 predictions、官方 harness reports、逐题失败分类、调用/耗时/
  token telemetry 和最终比较报告。
