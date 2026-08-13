# AgentForge 评测指南

本文说明如何复现 AgentForge 的评测证据，并区分离线正确性、真实模型行为和官方 benchmark 成绩。

## 三层评测

### 1. 单元和集成回归

先运行完整本地验证：

```powershell
uv run --frozen pytest -ra
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen python -m compileall src
git diff --check
```

这一层验证 Runtime、持久化、Policy、进程控制、文件修改和评测基础设施，不衡量模型修复能力。

### 2. 离线 Pilot 矩阵

离线矩阵使用真实 AgentForge Runtime 和确定性的 MockModelProvider。每个任务拥有全新的工作区，
由评测器执行 baseline，之后经过审批绑定的文件修改、受控开发测试、diff 校验和 hidden final
verification。它用于低成本验证完整控制流，不消耗真实模型 token。

当前矩阵包含四个 Fixture，每个 Fixture 重复三次。这个结果适合做回归检测，但不能证明真实
模型可以解决这些任务。

### 3. 真实模型 Canary 和 Study

真实模型运行要求：

- Git 工作区干净；
- 源码、依赖和协议 manifest 冻结；
- 明确的操作员确认 digest；
- 固定模型和 response model；
- 固定价格快照；
- 隔离的临时工作区；
- 完整的 usage 和结果分类。

评测器记录模型请求、逻辑调用、工具调用、文件修改、测试执行、Provider 偏差、token、成本和失败类别。
Study 可以处于未完成状态；未执行 slot 不能被计为失败或成功。

## 失败分类

- **模型质量失败：** Runtime 和测试正常执行，但最终验证失败，例如 `FINAL_HIDDEN_TEST_FAILED`。
- **基础设施失败：** Provider、进程、工作区或持久化依赖自身失败，不是模型修改内容导致。
- **配置失败：** 冻结绑定或授权无效，运行无法开始。
- **Indeterminate：** 无法证明副作用是否已经完成，因此禁止不安全重放。

只有可评分的模型质量结果进入 pass@1/pass@3。基础设施失败和 indeterminate 必须单独报告。

## 当前真实模型证据

当前 `gpt-5.4-mini` 矩阵中，QuixBugs 和 BugsInPy 各自完成了 3/3 次独立成功。裁剪版 SWE-bench
和自建持久化任务都通过了 visible test，但各自失败于一个 hidden invariant。随后对两个失败任务
各执行了一次任务级诊断 Canary，结果仍是 `FINAL_HIDDEN_TEST_FAILED`，因此不能称为 benchmark 提升。

公开报告只保留脱敏信息，包括 hash、数量、分类和有界摘要，不包含 API Key、原始模型响应、hidden tests、
参考文件或未脱敏工具参数。

## 任务级诊断流程

任务失败后按以下步骤处理：

1. 保留 baseline 报告和协议不变；
2. 检查最终 diff，以及 visible/hidden 测试结果分类；
3. 用不复制 hidden test 实现细节的方式描述缺失的不变量；
4. 创建带独立版本和 digest 的 prompt 或 Fixture 干预；
5. 只运行一次低成本 Canary；
6. 对比成功率、失败类别、轨迹长度、工具数、测试数和成本；
7. 只有在多个独立重复中有效时，才考虑进入正式策略，否则保持为诊断实验。

这套流程借鉴公开 coding-agent harness 的共同做法：冻结运行身份、隔离任务工作区、保存轨迹，
并报告重复成功率和成本，而不是只展示一次成功案例。

后续 `task_contract_guidance` 实验同样是 0/2，两个目标任务仍然是
`FINAL_HIDDEN_TEST_FAILED`。这个负结果说明，在检查模型最终 diff 和动作轨迹之前，不应继续
只靠增加 Prompt 提示来调参。

Runtime 现在会把 development test 失败渲染为有界的 `repair_feedback`，包含 schema 版本、失败类型、
退出码、失败节点、断言摘要、测试摘要和确定性的下一步动作。该反馈会持久化到 checkpoint，并在
下一步返回给模型。它只来自 development test 输出；final hidden verification 仍会清空 stdout/stderr，
不会生成 hidden-test 反馈。

## 不做的声明

AgentForge 当前不声称拥有官方 SWE-bench 成绩、通用自动修复能力、生产部署就绪状态、OS 级 sandbox，
也不声称与 OpenHands、SWE-agent 或 Aider 等项目等价。这些项目仅作为评测和工程设计参考，
它们的公开成绩不属于 AgentForge。
