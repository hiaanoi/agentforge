# DeepSeek + SWE-bench canary result / 单实例验证结果

Date / 日期: 2026-08-16

## Claim / 结论

The pinned SWE-bench infrastructure smoke test passed, and AgentForge completed
bounded, auditable DeepSeek-backed execution. The model did **not** produce a
prediction for `sympy__sympy-20590`; therefore this is not a resolved SWE-bench
instance and no model success rate is claimed.

固定版本的 SWE-bench 基础设施冒烟测试通过，AgentForge 也完成了有界、可审计的
DeepSeek 实际调用。但模型没有为 `sympy__sympy-20590` 生成预测，因此该实例并未
解决，本文不声称任何模型成功率。

## Frozen inputs / 固定输入

- AgentForge tested source commit: `8f7885cee76a3f7f5e39a704191e92f0f255ab0b`
- SWE-bench commit: `4e6126978a16bdfebc6538db8f28cacc2c8b77dc`
- Dataset: `SWE-bench/SWE-bench_Lite`, test split
- Instance: `sympy__sympy-20590`
- Repository: `sympy/sympy`
- Base commit: `cffd4e0f86fefd4802349a9f9b19ed70934ea354`
- Provider/model identity: `deepseek/deepseek-v4-pro`
- Runtime budget: 14 model requests, 50 reads, 20 runtime steps, one worker

## Infrastructure evidence / 基础设施证据

- The opt-in authenticated DeepSeek live test passed: `1 passed`.
- AgentForge Core and Recovery Linux demos exited with code `0`.
- The official pinned SWE-bench harness gold smoke completed one instance:
  `completed=1`, `resolved=1`, `errors=0`, `unstopped_containers=0`.
- Gold smoke report SHA-256:
  `6407c1a791f667ebd985b992425f4f9a476f4fc4c86497dd04d011976d98d80c`.

The gold patch was used only to validate the official harness and container.
It was never stored in the AgentForge task input or shown to the model.

Gold patch 仅用于验证官方 harness 与容器，未保存到 AgentForge 任务输入，也未提供给模型。

## AgentForge canary attempts / AgentForge 实际运行

| Run ID | Source commit | Model calls | Tool calls | Tokens | Terminal result |
| --- | --- | ---: | ---: | ---: | --- |
| `0abe092d-d90c-45fe-b53b-ecfd40414d18` | `40fc117` | 9 | 9 | 47,984 | `MODEL_TOOL_FAILED`: file-scoped `search_text` was rejected |
| `e29f6773-5be2-4de1-81f9-1a79bc4740cb` | `93791a0` | 8 | 8 | 21,602 | `MODEL_TOOL_FAILED`: `read_file` lacked bounded offset reads |
| `ae6793b6-ef2b-4981-b967-9792c15fa918` | `8f7885c` | 14 | 14 | 54,584 | `BUDGET_EXHAUSTED`: repeated reads, no proposed mutation |

Total observed usage across the three bounded attempts was 124,170 tokens.
The final attempt made 13 `read_file` calls and one `search_text` call. It made
no write request, created no approval, and left the workspace clean at the exact
base commit.

三次有界尝试共观察到 124,170 tokens。最终一次进行了 13 次 `read_file` 和一次
`search_text`，没有提出写入、没有生成审批，并在结束后保持工作区位于精确基线且无改动。

## Product findings / 产品发现

The canary exposed four reusable integration defects that were fixed with
regression coverage:

1. DeepSeek responses may contain explanatory text alongside function calls;
   tool calls now take precedence and the discarded text is not persisted.
2. Workspace capture now excludes `.git` before bounded file reads.
3. `search_text` now accepts either a directory or one workspace file while
   retaining sensitive-file protection.
4. `read_file` now supports bounded byte offsets while retaining full-file
   SHA-256 evidence and sensitive-file protection.

本次 canary 暴露并修复了四个可复用的集成缺陷：混合文本/工具调用归一化、捕获前排除
`.git`、单文件文本搜索、以及带完整摘要证据的有界偏移读取；相关安全边界均有回归测试。

## Interpretation and next step / 解释与下一步

The final failure is classified as model/tool-planning quality, not provider,
network, Docker, dataset, or harness failure. Raising the budget alone is not
recommended because the final trace was already repeating nearly identical
read windows. The next meaningful experiment is to keep the same frozen
instance and budgets while changing only the model or adding a model-independent
loop-correction policy, then compare the audited traces.

最终失败归类为模型的工具规划质量问题，而不是 provider、网络、Docker、数据集或 harness
故障。不建议只提高预算，因为最终轨迹已在重复近似读取窗口。下一项有意义的实验应保持同一
实例与预算，只替换模型，或加入与模型无关的循环纠正策略，再比较审计轨迹。
