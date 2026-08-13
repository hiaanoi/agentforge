# 任务失败根因诊断

本文记录两个真实模型任务首次诊断实验的结果。两个任务都通过了 visible test，但在最终验证
中失败。本文是诊断材料，不是新的 benchmark 成绩。

## 证据边界

两次运行都正常完成，没有 Provider、工作区、进程、持久化或授权错误。两个任务的 visible
development test 都通过，最终 hidden verification 失败。报告将结果分类为
`FINAL_HIDDEN_TEST_FAILED`。

故意带 bug 的 Fixture workspace 保持不变。reference 文件和 hidden test 没有进入模型上下文，
也没有作为公开 prompt 内容使用。

## Phase Capture 任务

### 观察到的失败

模型修改通过了“清空当前 phase”的 visible test，但最终验证发现：之前已经返回给调用方的
phase 列表也被清空了。也就是说，清理当前 phase 影响了历史 phase 的记录。

### 根因

实现把 handler 当前使用的可变 buffer 和每个 phase 的持久记录混在了一起，导致多个 phase
共享同一个列表。清理当前 phase 时，也修改了之前 phase 所观察的同一个列表对象。

### 必须保持的不变量

- 每个 phase 都拥有独立的 records 列表；
- `clear()` 只原地修改当前 phase 的列表；
- 当前 phase 之前已经暴露的引用仍然保持有效；
- 历史 phase 的引用不能被清空或污染；
- 清理之后的新消息仍然能通过当前 phase 原有引用观察到。

## Durable Dispatch 任务

### 观察到的失败

模型修改通过了“dispatch 后重启不会重复 dispatch”的 visible 场景，但最终验证发现：当
receipt 已经被 claim、却还没有 durable dispatch 记录时，另一个 worker 仍然可以替换当前 owner。

### 根因

claim 转换把所有非 COMPLETED receipt 都当成可替换，没有区分两个不同的 crash window：

- 已经存在 durable dispatch 事实，可以安全恢复并复用；
- 只有 claim、还没有 dispatch 事实，无法确认旧 worker 是否正在执行副作用。

在第二种情况下替换 owner，可能让副作用执行两次。

### 必须保持的不变量

- 未被占用的 receipt 可以被 claim；
- 已 claim 但没有 durable dispatch 的 receipt 不能被自动替换；
- 已有 durable dispatch 的 receipt 可以恢复并复用原事实；
- completion 必须受当前 owner 条件约束；
- 重复处理必须返回持久化结果，不能创建新的 dispatch。

## 诊断干预

上一轮 `task_diagnostic` Prompt 使用了通用不变量提示，没有提升两个任务的结果。当前新增了
独立版本的 `task_contract_guidance` 变体，明确写出上述两个已确认契约。

该变体与 baseline 以及上一轮诊断结果分开保存。后续可以准备新的真实模型 Canary 验证它，
但不能在没有独立重复证据的情况下把结果合并到 baseline。

随后对两个目标任务各执行了一次 `task_contract_guidance` Canary。两个任务仍然是
`FINAL_HIDDEN_TEST_FAILED`，没有基础设施失败。本次运行使用 13 次物理请求、36,014 个 token，
预计成本 `$0.034248`。SWE-bench 使用 4 次请求和 6,373 个 token；持久化任务使用 9 次请求和
29,641 个 token。因此，在这次有界实验中，明确契约也没有带来可测量的成功率提升。

## 工程结论

这两个失败不是 Runtime 丢失状态，也不是 AgentForge 执行了不安全副作用，而是模型满足了
直接可见症状，却遗漏了对象 identity、状态所有权和 crash window 相关的深层契约。下一轮实验
应同时比较最终不变量结果、模型轨迹长度和成本。
