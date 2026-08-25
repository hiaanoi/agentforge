# SWE-bench Verified 50 题配对评测报告

## 结论先行

在同一批 50 个 SWE-bench Verified 公开任务、同一 Docker harness、同一
`gpt-5.4-mini` 中转模型和同一 pass-2 预算下：

| 评测臂 | resolved | resolved rate | 非空补丁 | 内部模型调用 | 总耗时 |
|---|---:|---:|---:|---:|---:|
| AgentForge（mini_linear + 审批/持久化/审计） | 21/50 | 42% | 45/50 | 421 | 4,586.9 s |
| 官方 mini-SWE-agent | 31/50 | 62% | 49/50 | 899 | 9,416.9 s |

因此，这次公开基准的直接结论是：AgentForge 当前修复能力落后 mini-SWE-agent
20 个百分点（10 道题）。不能声称 AgentForge 已经打赢或打平 mini-SWE-agent。

## 配对结果

- 两者都解决：19 题。
- 仅 AgentForge 解决：2 题：`sympy__sympy-17318`、`django__django-14007`。
- 仅 mini-SWE-agent 解决：12 题。
- 两者都未解决：17 题。
- 两臂官方 harness 均为 50/50 submitted、0 infrastructure failure。

AgentForge 的官方分类：21 RESOLVED、21 UNRESOLVED、3 AMBIGUOUS_FAILURE、5
EMPTY_PATCH。mini-SWE-agent：31 RESOLVED、15 UNRESOLVED、3
AMBIGUOUS_FAILURE、1 EMPTY_PATCH。

## 成本与效率

AgentForge 每题平均约 8.4 次模型调用、91.7 秒；mini-SWE-agent 每题平均约
18.0 次调用、188.3 秒。AgentForge 侧可读取到 6,211,856 个 provider token；
mini-SWE-agent 当前没有可用 token telemetry，因此本报告不伪造美元成本，也不把
调用次数直接当作 token 成本。两臂的预算、温度（0）、模型和任务清单保持一致，
但 token 级成本仍应标记为“未测量”。

## 对 AgentForge 设计价值的准确表述

这次结果没有证明控制平面提升了修复率，但证明了它有独立的工程价值：50 个任务
均经过持久化 campaign、逐题 workspace、审批/恢复链路、预测导出、artifact hash
绑定和官方 harness 评分；模型失败、空补丁、协议失败和 harness 结果被分开记录。
这使得“修复能力”和“可审计执行能力”可以独立比较，而不是把一个成功的 demo 当成
基准成绩。

## 下一步决策

下一步应保留 AgentForge 的持久化、审批、审计、恢复和评测控制平面，把官方
mini-SWE-agent 的修复循环作为可插拔 repair engine 接入，再用同一 50 题协议复测。
验收标准不是“控制平面事件更多”，而是：在不降低审计/恢复能力的前提下，resolved
rate 至少达到 mini-SWE-agent 当前的 62%，并对失败题给出可复现的逐题证据。

## 可复现证据

- 协议：`verified50-openai-gpt54mini`
- 协议任务选择哈希：`62a6a6683556c48bc2d30ca5b83421e3d83baefa9fb3149df7878e3d9fc82d61`
- 服务器结果目录：`/home/ubuntu/verified50-gpt54mini-agentforge-rerun-20260823-1`
- 服务器产物：`comparison.json`、`comparison.md`、`artifact-manifest.json`、两臂
  predictions、两份 official harness report 和 score metadata。
