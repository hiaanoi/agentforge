# DeepSeek Flash + SWE-bench canary result

Date: 2026-08-16
日期：2026-08-16

## Executive result / 核心结论

The pinned Linux, Docker, dataset, DeepSeek, and official SWE-bench harness path
is operational. AgentForge also enforced source provenance, fixed profile trust,
write boundaries, approval gates, test ownership, audit events, and bounded
termination during real model-backed repair attempts.

固定版本的 Linux、Docker、数据集、DeepSeek 与官方 SWE-bench harness 链路均可运行。
在真实模型修复尝试中，AgentForge 也正确执行了源码溯源、精确测试配置信任、写入边界、
审批门、测试所有权、审计事件与有界终止。

DeepSeek `deepseek-v4-flash` did **not** produce a mutation for either runnable
canary (`sympy__sympy-20590` or `pallets__flask-4045`). Both workspaces remained
clean at their exact base commits. Consequently, no model prediction was sent
to the official harness and no resolved SWE-bench instance is claimed.

DeepSeek `deepseek-v4-flash` 在两个可运行 canary（`sympy__sympy-20590` 与
`pallets__flask-4045`）上都没有产生修改，两个工作区均保持在精确基线且无改动。
因此没有可提交给官方 harness 的模型预测，本报告不声称解决了任何 SWE-bench 实例。

## Frozen environment / 固定环境

- AgentForge final tested source: `be9904077283cc68889230bbda49153fa203f014`
- SWE-bench source: `4e6126978a16bdfebc6538db8f28cacc2c8b77dc`
- Dataset: `SWE-bench/SWE-bench_Lite`, test split, 300 rows
- Provider/model: `deepseek/deepseek-v4-flash`
- Host: Ubuntu 22.04, 8 vCPU, 32 GiB RAM, 200 GiB SSD
- Evaluation worker count: 1
- AgentForge repair budgets: fixed `ENGINEERING` (10 model calls) and fixed
  `CHALLENGE` (14 model calls); policy write and test boundaries were not relaxed

## Infrastructure evidence / 基础设施证据

All three selected official images completed a one-instance gold smoke with
`completed=1`, `resolved=1`, `errors=0`. Gold patches were used only by the
official harness to validate infrastructure. They were never stored in the
AgentForge task input, workspace, verifier, or model conversation.

三个选定实例的官方镜像均通过单实例 gold smoke：`completed=1`、`resolved=1`、
`errors=0`。gold patch 只由官方 harness 用于验证基础设施，从未写入 AgentForge
任务输入、工作区、验证器或模型对话。

| Instance | Base commit | Gold report SHA-256 |
| --- | --- | --- |
| `sympy__sympy-20590` | `cffd4e0f86fefd4802349a9f9b19ed70934ea354` | `6407c1a791f667ebd985b992425f4f9a476f4fc4c86497dd04d011976d98d80c` |
| `django__django-11049` | `17455e924e243e7a55e8a38f45966d8cbb27c273` | `4c0d4d15add8243df464e1227528ff92712ace4368afb3fa16216135ecfc9807` |
| `pallets__flask-4045` | `d8c37f43724cd9fb0870f77877b7c4c7e38a19e0` | `b8755a2db0bdf0290c57f7b15f0bb9311b6592c0147dc6ee68f9e5b1a23382b7` |

AgentForge Core and Recovery Linux demos also completed with exit code `0`.
The pinned DeepSeek live provider test passed before the canary runs.

## Flash repair attempts / Flash 真实修复尝试

Token values are the provider-reported total token counts. Cached input tokens
are already represented in provider usage and are not added again.

| Instance | Run ID | AgentForge source | Model calls | Tool activity | Tokens | Result |
| --- | --- | --- | ---: | --- | ---: | --- |
| SymPy | `77ffc964-8bb5-40e0-abc6-8c428840dd80` | before bounded tool-error recovery | 12 | 7 reads, 5 searches | 61,223 | malformed tool arguments exhausted correction path |
| SymPy | `bef5d671-ee7d-490c-b352-ef080020dc4a` | `1eabcbd` | 14 | status/log/list/read/search only | 43,473 | `MODEL_CALL_LIMIT`, no mutation |
| SymPy | `c5e69c23-00e4-4896-b197-a62c77e4606d` | `be99040` | 14 | status/log/read/search only | 68,862 | `MODEL_CALL_LIMIT`, no mutation |
| Flask | `ef38fd3f-ae88-4396-999c-30062db6df00` | `be99040`, ENGINEERING | 10 | status/list/read/search only | 41,009 | `MODEL_CALL_LIMIT`, no mutation |
| Flask | `dd695102-cfa1-4f52-9d9e-2bc1ef225e6f` | `be99040`, ENGINEERING | 10 | log/list/read/search only | 34,129 | `MODEL_CALL_LIMIT`, no mutation |
| Flask | `af053fbb-2b50-4e70-a129-5ed4b1d53e4f` | `be99040`, CHALLENGE | 14 | log/read/search only | 68,869 | `MODEL_CALL_LIMIT`, no mutation |

Total observed Flash usage was 317,565 tokens. The final Flask attempt used the
largest built-in fixed repair budget. It found and read relevant Flask source,
but made no write request, created no approval, ran no development test, and
left Git clean. Repeating with a larger custom budget would change the product
policy and is not justified by this trace.

Flash 六次有界尝试共观察到 317,565 tokens。最后一次 Flask 尝试使用产品内置的
最高固定修复预算；模型定位并读取了相关源码，但没有请求写入、没有产生审批、没有运行
开发测试，Git 仍保持干净。继续使用自定义更大预算会改变产品策略，而且当前轨迹不支持
这样做。

## Django compatibility finding / Django 兼容性发现

`django__django-11049` passed the official gold smoke, but AgentForge refused to
capture the source baseline because the repository contains four tracked
documentation symlinks. The product deliberately rejects links and reparse
points during trusted source capture. No model request was made and no source
was changed.

`django__django-11049` 通过了官方 gold smoke，但仓库包含四个被 Git 跟踪的文档
符号链接，AgentForge 在可信源码快照阶段按设计拒绝链接与重解析点。因此该实例没有发起
模型请求，也没有修改源码。这是明确的仓库兼容性限制，不应通过删除链接或放宽安全检查
来掩盖。

## Product defects found and fixed / 发现并修复的产品缺陷

The canary work found six reusable integration defects. All fixes have regression
coverage, and the final local verification completed with `1666 passed, 22
skipped`; Ruff and mypy also passed.

1. DeepSeek mixed explanatory text and function calls are normalized safely.
2. `.git` is excluded before bounded workspace capture.
3. `search_text` supports a directory or one workspace file while retaining
   sensitive-file protection.
4. `read_file` supports bounded offsets while retaining full-file digest evidence.
5. Recoverable model tool mistakes receive a bounded correction opportunity;
   security, approval, timeout, and execution errors remain fail-closed
   (`1eabcbd`).
6. Product runs now default to constrained repair instructions instead of the
   legacy read-only analysis prompt (`be99040`).

本次 canary 共发现并修复六项可复用集成缺陷，均有回归测试。最终本地验证结果为
`1666 passed, 22 skipped`，Ruff 与 mypy 也通过。

## Interpretation / 解释

The experiment separates three questions:

- **Infrastructure:** passed.
- **AgentForge control-plane behavior:** passed for bounded execution, audit,
  trust, policy enforcement, clean failure, and recovery demos.
- **Flash autonomous repair quality:** failed on the selected real tasks because
  the model never progressed from inspection to mutation within the product's
  fixed budgets.

本实验将问题拆成三层：基础设施已通过；AgentForge 控制面在有界执行、审计、信任、
策略执行、干净失败与恢复演示方面通过；Flash 的自主修复质量未通过，因为它在产品固定
预算内没有从调查推进到修改。

This is a useful negative result, not a benchmark score. The next controlled
comparison should keep the same commits, tasks, profiles, and budgets while
changing only the model. A stronger tool-using model is the recommended next
variable; repeatedly increasing Flash's budget is not.

这是有价值的负结果，但不是基准分数。下一项受控实验应保持提交、任务、测试配置与预算
不变，只替换模型。建议下一变量是更强的工具调用模型，而不是反复增加 Flash 预算。
