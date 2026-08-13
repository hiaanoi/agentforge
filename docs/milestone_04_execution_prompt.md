# AgentForge Milestone 4 Execution Prompt

继续开发项目：

`D:\aaa\agentforge`

开始规划并实现：

**Milestone 4: Real Model Adapter and Context Engineering**

本 Prompt 是完整执行规格。实施前必须先完成架构审计、正式设计文档和 TDD 实施计划；设计确认后再编码。不要创建 Git commit，不要进入 Milestone 5。

---

# 一、当前稳定基线

当前已完成并验收：

- Milestone 0：PASS
- Milestone 1：PASS
- Milestone 2：PASS
- Milestone 3：PASS

稳定基线：

- commit：`b4ed3ee0b882d847bdae45029b554b5724713e0a`
- tag：`milestone-3-complete`
- Python：3.14.3
- M3 最终测试：104 passed，2 skipped，0 failed
- Ruff：通过
- mypy strict：32 source files，无问题
- compileall：通过

执行前必须检查：

```powershell
Get-Location
git branch --show-current
git status --short
git log --oneline --decorate -3
git rev-parse milestone-3-complete
git rev-parse b4ed3ee
```

要求：

1. 当前目录必须为 `D:\aaa\agentforge`；
2. `milestone-3-complete` 必须指向 `b4ed3ee`；
3. 工作区必须干净；
4. 当前分支如果尚不存在，则从基线创建并切换：

   ```powershell
   git switch -c feat/milestone-4
   ```

5. 如果已经位于 `feat/milestone-4`，不要重复创建；
6. 不修改 `main`；
7. 不修改 `milestone-3-complete`；
8. 不创建 commit，不 push，不 merge，不 rebase，不 stash，不 reset，不 clean。

如果基线不一致，立即停止并报告。

---

# 二、里程碑目标

让真实远程模型安全进入现有 AgentForge Runtime，并使用当前只读 Repository Tools 完成真实仓库分析任务，同时不能绕过：

- Policy Engine
- Tool Budget
- Model Budget
- Approval
- Checkpoint
- Durable Resume
- Event Audit
- Workspace 隔离
- 敏感文件保护
- Loop Detection
- Context Policy

关键目标不是“成功调用一次 OpenAI API”，而是证明：

> 真实模型可以通过 Provider 无关接口进入 AgentForge，在受预算、上下文、审批、审计和恢复约束的 Runtime 中完成多轮只读工具调用。

最终演示任务示例：

> 分析 AgentForge 仓库中 AgentRuntime、PolicyEngine 和 ToolExecutor 的调用关系，说明一次工具调用从模型决策到事件持久化的完整路径，并引用实际 fixture 文件路径。

模型应完成至少两个只读工具调用，例如：

```text
list_files
-> search_text
-> read_file
-> final answer
```

---

# 三、分阶段门禁

M4 必须拆成两个内部验收阶段，不允许全部实现后一次性测试。

## M4A：Real Model Foundation

实现：

- Provider 无关模型领域对象；
- OpenAI Responses Adapter；
- Function Calling Schema 转换；
- `call_id` 映射；
- 模型错误、超时和重试；
- 模型请求预算和 usage；
- Provider/attempt 事件审计；
- RuntimeSnapshot v2 最小升级；
- Fake Client 测试；
- 默认关闭的 Live Fixture 测试。

M4A 的测试、Ruff 和 mypy 全绿后，才能进入 M4B。

## M4B：Context Reliability

实现：

- ContextBuilder；
- ToolResultRenderer；
- 确定性上下文裁剪；
- 确定性重复循环检测；
- LoopState 和 ContextState 持久化；
- 重启后一致性；
- 完整只读分析 Demo。

---

# 四、本阶段范围

本阶段实现：

1. 保留并兼容 MockModelProvider；
2. 新增 OpenAIModelProvider；
3. 使用官方 OpenAI Python SDK 和 Responses API；
4. 统一 ModelRequest、ModelResponse、ModelUsage、ModelAttempt 和 ModelError；
5. Provider 级超时和有限重试；
6. 模型请求预算和 Token 累计；
7. Responses Function Tool Schema 转换；
8. Provider-neutral `call_id`；
9. ContextBuilder；
10. ToolResultRenderer；
11. 确定性上下文压缩；
12. 精确重复循环检测；
13. RuntimeSnapshot v2；
14. 未版本化 checkpoint、v1 和 v2 的兼容策略；
15. 模型调用事件审计；
16. 默认关闭的真实 API Live Test；
17. 真实只读 fixture 仓库分析 Demo。

本阶段禁止实现：

- OpenAI Agents SDK
- Anthropic Provider
- write_file
- apply_patch
- shell
- run_tests
- 任意代码修改工具
- FastAPI
- CLI 产品化接口
- MCP
- LangGraph
- Multi-Agent
- RAG
- Docker
- Redis
- Celery
- Web 前端
- 自动代码修复
- 第二个 LLM 上下文摘要器

---

# 五、远程模型安全边界

真实 Provider 是新的数据外发边界。

必须明确：

1. 默认 Provider 仍为 Mock；
2. 真实 OpenAI Provider 必须显式配置后启用；
3. Repository Tool 的普通文本结果可能发送给远程 Provider；
4. 敏感文件名规则不能保证发现普通文件正文中的所有秘密；
5. ContextBuilder 不得重新暴露已经脱敏的参数；
6. API Key 不得进入数据库、Event、Checkpoint、日志、异常或 repr；
7. Provider 配置中的密钥使用 `SecretStr` 或等价不可序列化类型；
8. 密钥字段必须 `repr=False` 且从 model dump 中排除；
9. OpenAI 请求默认使用：

   ```python
   store=False
   parallel_tool_calls=False
   ```

10. M4 不使用 `previous_response_id` 作为会话机制；上下文由本地 checkpoint 完整重建；
11. OpenAI SDK 内建重试必须关闭，所有重试由 AgentForge 显式管理；
12. Provider 原始响应和原始异常对象不得持久化。

安全文档必须明确记录远程数据外发风险。

---

# 六、Provider 架构

Provider 无关接口：

```python
class ModelProvider(Protocol):
    @property
    def name(self) -> str:
        ...

    async def generate(
        self,
        request: ModelRequest,
    ) -> ModelResponse:
        ...
```

实现：

- MockModelProvider
- OpenAIModelProvider

要求：

1. Runtime、ContextBuilder、ToolExecutor 和领域层不得依赖 OpenAI SDK 类型；
2. 所有 OpenAI 类型只存在于 Provider Adapter 内；
3. OpenAI 客户端不能散落到多个模块；
4. 使用 AsyncOpenAI 或当前官方异步客户端；
5. SDK 客户端配置 `max_retries=0`；
6. Provider 自己实现可测试重试；
7. 不硬编码模型名称；
8. 不硬编码价格；
9. Provider request ID 仅作为审计元数据，不是恢复依据；
10. OpenAI Provider 必须支持 fake client 注入。

配置至少支持：

- `OPENAI_API_KEY`
- `AGENTFORGE_OPENAI_MODEL`
- `AGENTFORGE_MODEL_TIMEOUT_SECONDS`
- `AGENTFORGE_MODEL_MAX_RETRIES`
- `AGENTFORGE_OPENAI_STORE`，默认 false

没有 API Key 时：

- 默认测试必须正常运行；
- Mock Provider 必须完全可用；
- Live Test 必须自动 skip。

---

# 七、Responses API 与 Function Calling

工具必须转换成 Responses API 的 Function Tool 格式，而不是 Chat Completions 的嵌套格式。

每个工具至少包含：

```json
{
  "type": "function",
  "name": "tool_name",
  "description": "description",
  "parameters": {},
  "strict": true
}
```

必须实现发送前 SchemaNormalizer：

- 设置 `additionalProperties=false`；
- 处理嵌套 object；
- 处理 `$defs` 和引用；
- 处理 optional/nullable 字段；
- 对不支持的 schema 明确失败；
- Schema 转换失败时不得发送网络请求；
- OpenAI strict schema 不能替代 AgentForge Pydantic 参数校验。

请求必须设置：

```python
parallel_tool_calls=False
store=False
```

一次响应只允许：

- 一个 ToolCall
或
- 一个 FinalAnswer

映射规则：

1. 恰好一个合法 function call：`ToolCall`
2. 没有 function call 且存在有效文本：`FinalAnswer`
3. 多个 function call：`MODEL_PROTOCOL_ERROR`
4. 同时存在歧义性 function call 和 final：`MODEL_PROTOCOL_ERROR`
5. 无 function call 且无有效文本：`MODEL_PROTOCOL_ERROR`
6. Function arguments 非法 JSON：`MODEL_OUTPUT_INVALID`
7. 不得静默忽略额外调用
8. 不得默认取第一个调用

内部 `ToolCall` 必须新增：

```python
call_id: str | None
```

要求：

- OpenAI function call 的 `call_id` 映射到内部 ToolCall；
- tool result 返回 Provider 时必须关联相同 `call_id`；
- `call_id` 必须进入 checkpoint；
- Mock Provider 可以使用 `None` 或确定性测试 ID；
- `call_id` 不得被用作 AgentForge 的 Run、Approval 或 Checkpoint 主键。

---

# 八、模型领域对象

设计或完善：

- ModelRequest
- ModelResponse
- ModelUsage
- ModelAttempt
- ModelError
- ModelProviderConfig
- ProviderTool
- ContextItem
- ContextPolicy
- ContextBuildResult
- ContextState
- LoopState

ModelUsage：

- `input_tokens: int | None`
- `output_tokens: int | None`
- `total_tokens: int | None`
- `cached_input_tokens: int | None`
- `reasoning_tokens: int | None`

Provider 未返回的字段使用 None，不得猜测。

ModelResponse 至少包含：

- `action`
- `usage`
- `provider`
- `model`
- `provider_request_id`
- `duration_ms`
- `attempt_count`
- `sanitized_metadata`

Provider 原始响应不得进入数据库、Event 或 Checkpoint。

Context history 不应继续无限扩展为无结构的 `list[JsonValue]`。新增 Provider-neutral ContextItem，至少区分：

- system instruction
- user task
- tool call
- tool result
- approval result
- runtime error
- loop warning

---

# 九、模型预算语义

必须区分：

- `model_turn`：一次逻辑模型决策；
- `model_request`：一次实际网络请求，包括 retry。

兼容规则：

- 现有 `Run.current_step` 和 `Run.max_steps` 继续表示逻辑 model turn；
- 不新增会与 current_step 漂移的第二套 model_turn_count；
- 新增持久化的 `model_request_count`；
- 旧 `RunBudget.max_model_calls` 如需替换，必须提供兼容 alias 和测试；
- 不静默改变 M0-M3 的 step 语义。

建议预算字段：

- `max_model_requests`
- `max_output_tokens_per_request`
- `max_total_input_tokens`
- `max_total_output_tokens`
- `max_total_tokens`
- `max_estimated_cost`，可选

必须区分：

## 请求前确定性限制

- context item 数量
- 字符数
- UTF-8 字节数
- 已累计真实 Token
- 本次最大输出预留

## 请求后真实统计

- Provider 返回的真实 input/output/total token

禁止：

- 把字符估算记为真实 token；
- 在没有价格表时伪造费用；
- 在无法精确知道 input token 时声称做了精确请求前 Token 计算。

要求：

1. 每个实际网络请求前检查 request budget；
2. request count 在发送前持久化，避免重启后清零；
3. 每次 retry 增加 model_request_count；
4. 一个逻辑 generate 只消耗一个 model turn；
5. Provider usage 返回后累计真实 token；
6. Budget 状态进入 Run 和 RuntimeSnapshot；
7. 不同 Run 完全隔离；
8. 价格表如实现，必须可配置、可版本化；
9. 本阶段可以只统计 Token，不实现费用。

如果新增 Run 数据库列，必须实现已有 SQLite 数据库的幂等升级方案；不能假设 `create_all()` 会为已有表增加列。

---

# 十、模型 Attempt、错误和重试

至少定义稳定错误码：

- MODEL_AUTH_ERROR
- MODEL_RATE_LIMITED
- MODEL_TIMEOUT
- MODEL_TRANSPORT_ERROR
- MODEL_BAD_REQUEST
- MODEL_PROVIDER_ERROR
- MODEL_PROTOCOL_ERROR
- MODEL_OUTPUT_INVALID
- MODEL_BUDGET_EXCEEDED

只允许自动重试：

- 临时网络错误
- timeout
- rate limit
- 明确可重试的服务端错误

禁止自动重试：

- API Key 或认证错误
- 权限错误
- 请求参数错误
- Schema 转换错误
- Budget 超限
- checkpoint 损坏
- 非法协议超过上限
- 明确不可恢复错误

重试要求：

- 指数退避
- bounded jitter
- 最大 retry 数
- 注入 sleep/backoff
- 测试不真实等待
- SDK 内建 retry 关闭
- 每个物理 request 都可审计
- 不返回原始 Provider 异常消息给模型

ModelAttempt 至少记录：

- logical_call_id
- attempt_number
- started/completed/failed 状态
- error_type
- retryable
- duration_ms
- usage

进程在请求发出后、响应持久化前崩溃时，必须保留已消耗 request budget。允许恢复后重新请求，但必须记录可能产生重复计费，不能把旧 request 从计数中删除。

---

# 十一、模型事件语义

不要复用一个事件同时表示 logical turn 和 physical request。

新增或明确：

- MODEL_TURN_STARTED
- MODEL_ATTEMPT_STARTED
- MODEL_ATTEMPT_FAILED
- MODEL_RETRY_SCHEDULED
- MODEL_RESPONDED
- MODEL_FAILED
- CONTEXT_COMPACTED
- LOOP_WARNING
- LOOP_DETECTED
- BUDGET_EXCEEDED

现有 MODEL_REQUESTED 的兼容语义必须写入 ADR；可以保留为 logical turn 事件，但不能在每次 retry 重复写入。

Event 可记录：

- provider
- model
- logical_call_id
- attempt
- duration_ms
- usage
- action_type
- context counts/sizes
- error_type
- retry_delay
- budget snapshot
- sanitized provider metadata

Event 禁止记录：

- API Key
- 完整系统 Prompt
- 完整模型输入
- 完整模型输出
- 完整文件内容
- 完整工具结果
- 原始 Provider 错误响应
- 原始未脱敏参数

---

# 十二、ContextBuilder

ContextBuilder 必须独立于 OpenAIModelProvider。

输入：

- 原始用户任务
- RuntimeSnapshot
- 可用工具 Schema
- ContextPolicy
- 当前 Run
- 最新 Approval 决策
- LoopState
- Budget 状态

输出：

- Provider-neutral ModelRequest
- ContextBuildResult
- ContextState

ContextBuildResult 至少包含：

- item count before/after
- character count before/after
- UTF-8 bytes before/after
- compressed item summaries
- removed item summaries
- policy version

始终保留：

1. 版本化系统指令；
2. 原始用户任务；
3. 当前 pending action；
4. 最近完整 tool call/result 对；
5. 最新审批结果；
6. 最新错误；
7. resume phase；
8. Budget 状态；
9. loop warning。

禁止：

- 只保留 tool call 或只保留 tool result；
- 删除未消费审批；
- 删除当前错误；
- 将完整仓库一次性加入上下文；
- 恢复被脱敏参数；
- 将 API Key 或凭据放入上下文。

系统指令必须有明确版本号，进入 ContextPolicy 和 Snapshot。

---

# 十三、ToolResultRenderer

实现确定性 Renderer。

所有 renderer 输出：

- `original_size`
- `rendered_size`
- `truncated`
- `sha256_digest`
- `returned_count`，适用时
- `total_count`，仅当工具真实提供

禁止把返回数量伪装为完整总数。

## list_files

保留有限路径、returned_count、truncated 和 digest。

## search_text

保留文件路径、行号、有限片段、returned_count、truncated 和 digest。

## read_file

适配当前真实输出字段：

- path
- byte_size
- bytes_read
- content
- truncated

渲染保留头部、尾部、中间省略标记和 digest。

## get_git_diff

保留有限 diff、原始字符数、渲染后大小、truncated 和 digest。

Event 只记录统计和摘要，不保存完整渲染内容。

---

# 十四、确定性上下文压缩

第一版不新增 tokenizer 依赖。

预算维度：

- 字符数
- UTF-8 字节数
- item 数量

压缩顺序：

1. 从最旧的完整 tool call/result 对开始；
2. 先调用 ToolResultRenderer 压缩旧结果；
3. 再删除已经压缩且低价值的旧对；
4. 保留当前步骤和最近结果；
5. Approval、错误、恢复状态和 loop warning 优先保留；
6. 顺序稳定；
7. 相同输入产生相同输出；
8. 不调用第二个 LLM 总结。

必须测试 call/result 成对保留。

---

# 十五、Loop Detection

M4 第一版只实现可以精确定义的重复检测，不实现模糊的语义进展判断。

摘要：

```text
action_digest = SHA-256(tool_name + canonical validated arguments)
result_digest = SHA-256(canonical rendered result or stable error)
```

LoopState 至少包含：

- recent_action_digests
- recent_result_digests
- consecutive_same_action_result
- consecutive_same_error
- warning_count

检测：

1. 相同 action digest 连续重复；
2. 相同 action + result digest 连续重复；
3. 相同稳定 error code 连续重复。

不要在本阶段实现无法精确定义的“没有新增信息”语义判断。

语义：

- warning 阈值必须配置，默认值写入 ContextPolicy；
- 首次达到 warning 阈值：注入结构化提示并写 LOOP_WARNING；
- 达到终止阈值：Run FAILED，写 LOOP_DETECTED；
- 不同参数不能误判；
- 相同动作但不同结果不能立即终止；
- 新 action/result 重置对应连续计数；
- LoopState 进入 checkpoint；
- 重启后计数不清零；
- LOOP_DETECTED 后不得再次请求模型。

---

# 十六、RuntimeSnapshot v2 和迁移

RuntimeSnapshot v2 至少包含：

- schema_version = 2
- run_id
- step_number
- history / typed context items
- pending_tool_call，包括 call_id
- pending_approval_id
- tool_call_digest
- resume_phase
- model usage totals
- model_request_count
- loop_state
- context_state
- last_provider_metadata
- last_model_error
- context_policy_version
- system_prompt_version

M4A 必须一次性定义完整的 v2 字段结构。ContextState 和 LoopState 在 M4A 可以使用确定性
空默认值；M4B 只能开始填充这些既有字段，不能在保持 `schema_version=2` 不变的情况下
继续修改 v2 的结构。

迁移矩阵必须明确：

| 输入 | 行为 |
|---|---|
| M1/M2 未版本化 `{"history": ...}` | 确定性迁移，或明确标记不支持审批恢复 |
| M3 RuntimeSnapshot v1 | 迁移到 v2，缺失字段使用确定性默认值 |
| RuntimeSnapshot v2 | 正常加载 |
| 未知未来版本 | 明确失败 |

要求：

- 不静默丢弃未知字段；
- 迁移函数纯函数化、可单测；
- v1 migration 相同输入得到相同输出；
- 保留 M3 pending tool、approval、digest 和 resume phase；
- M3 三个崩溃窗口继续通过；
- Provider request/response ID 不能替代本地 Snapshot。

---

# 十七、Runtime 集成

Runtime 改造后：

1. ContextBuilder 构造 Provider-neutral ModelRequest；
2. model turn 开始时持久化逻辑事件；
3. 每次实际 attempt 前持久化 request budget；
4. Provider 返回 ModelResponse；
5. usage 累计并持久化；
6. action 进入原有 Tool/Approval Runtime；
7. ToolResultRenderer 生成模型上下文结果；
8. LoopState 更新；
9. RuntimeSnapshot v2 保存；
10. restart 后从本地 Snapshot 恢复。

真实 Provider 不得绕过：

- ToolRegistry
- Pydantic 参数校验
- Policy Engine
- Approval
- Tool Budget
- Model Budget
- Event Audit

---

# 十八、默认测试

默认 pytest 禁止真实网络。

OpenAI Provider 单元测试使用 fake client。

至少测试：

1. Responses Function Schema 转换；
2. strict schema 校验；
3. 请求设置 `parallel_tool_calls=False`；
4. 请求设置 `store=False`；
5. SDK 内建 retry 为 0；
6. 单 function call；
7. call_id 映射；
8. function_call_output 使用相同 call_id；
9. final text；
10. 多 function call 拒绝；
11. 空响应拒绝；
12. 非法 arguments；
13. usage 映射；
14. timeout 映射；
15. rate limit retry；
16. auth 不 retry；
17. transport 有限 retry；
18. 每次 retry 计入 model_request；
19. model turn 只计一次；
20. Budget 超限不请求；
21. API Key 不进入 repr、日志、Event、异常；
22. Provider 原始对象不进入 checkpoint；
23. Schema 错误时不请求网络。

---

# 十九、Context 和 Loop 测试

Context 至少测试：

1. 系统指令和版本；
2. 原始任务；
3. tool call/result 成对；
4. call_id 保留；
5. 旧结果按预算压缩；
6. read_file 头尾；
7. search_text 稳定渲染；
8. Approval 结果不被裁剪；
9. 当前错误不被裁剪；
10. 敏感参数不重新出现；
11. 相同输入结果一致；
12. 压缩统计；
13. 重启后输出一致。

Loop 至少测试：

1. 相同 action/result warning；
2. 达到阈值终止；
3. 不同参数不误判；
4. 相同 action、新 result 不立即终止；
5. 相同错误检测；
6. LoopState 持久化；
7. restart 后继续；
8. LOOP_DETECTED 后不再请求模型。

---

# 二十、Snapshot 和回归测试

至少测试：

1. 未版本化 checkpoint 处理；
2. v1 -> v2；
3. v2 正常恢复；
4. 未知版本拒绝；
5. M3 审批恢复；
6. M3 三个崩溃窗口；
7. M2 Tool Policy；
8. M0/M1 Runtime；
9. MockModelProvider 原测试；
10. 不同 Run 的 model budget、context 和 loop state 隔离。

---

# 二十一、Live Test 和验收分层

真实 API 测试默认不运行。

启用条件：

- `RUN_LIVE_TESTS=1`
- `OPENAI_API_KEY`
- `AGENTFORGE_OPENAI_MODEL`

网络阻断 fixture 只能对显式 `live` marker 做最小范围 opt-out。普通测试仍必须禁止网络。

Live Test：

1. 只使用临时 fixture 仓库；
2. 不访问用户真实项目；
3. 不修改文件；
4. 最多 5 个 model requests；
5. 限制工具调用；
6. 限制总 token；
7. 明确 timeout；
8. 不打印密钥；
9. 错误脱敏；
10. pytest marker 标记。

Case 1：fixture 仓库结构问答。

Case 2：fixture Runtime、Policy、ToolExecutor 调用链分析。

至少一个执行后的 Live Case 应：

- 使用两个或以上 Repository Tools；
- 生成 FinalAnswer；
- 引用真实 fixture 路径；
- 产生 Model、Tool、Budget、Checkpoint 和 Event 记录。

验收必须分层：

## 离线 M4 PASS

- Fake Client 和所有默认测试通过；
- Live Test 未启用时只记为 skipped；
- Ruff、mypy、compileall 通过。

## Live Validation

- 有 API Key 时单独执行；
- 成功则记录 LIVE PASS；
- 未执行时记录 NOT RUN，不影响离线 M4 PASS；
- 执行失败时报告真实脱敏结果，不伪造成通过。

可选手工 Demo 如需分析当前 AgentForge 仓库，必须由用户显式授权；自动 Live Test 不读取用户真实项目。

---

# 二十二、依赖约束

新增 OpenAI SDK 前：

1. 检查当前官方稳定 SDK 与 Python 3.14.3 兼容；
2. 使用 `uv add` 添加；
3. 在 pyproject 中使用合理的兼容版本范围；
4. 记录实际解析版本和原因；
5. 不手动编辑 uv.lock；
6. 不修改 Python 版本；
7. 不修改依赖源；
8. 不升级无关依赖；
9. 确认 uv.lock 不包含阿里云源。

除官方 OpenAI SDK 外，优先使用标准库和现有依赖。第一版不引入 tokenizer。

---

# 二十三、实施顺序

严格顺序：

```text
M4A.1 架构审计和正式设计
M4A.2 Model 领域对象和错误
M4A.3 Responses Schema Adapter
M4A.4 OpenAI Provider + Fake Client
M4A.5 Attempt、Retry、Budget、Events
M4A.6 完整 Snapshot v2 schema 和最小迁移
M4A.7 Runtime Provider 集成
M4A.8 M4A 全量门禁

M4B.1 ContextItem 和 ContextBuilder
M4B.2 ToolResultRenderer
M4B.3 确定性压缩
M4B.4 Loop Detection
M4B.5 Snapshot/Restart 集成
M4B.6 Live Fixture Demo
M4B.7 文档和最终验收
```

每个模块必须：

1. 先写失败测试；
2. 确认 RED 原因正确；
3. 写最小实现；
4. 确认 GREEN；
5. 运行相关回归；
6. 运行 Ruff 和 mypy；
7. 修复后才能进入下一模块。

---

# 二十四、实现前必须输出

编码前先输出：

1. 当前 Provider/Runtime 架构审计；
2. M4A/M4B 范围；
3. Model 领域对象；
4. Provider Adapter 接口；
5. call_id 和 Function Calling 数据流；
6. 模型预算语义；
7. Attempt/Retry 状态图；
8. ContextItem 和 ContextBuilder 设计；
9. Loop Detection 精确定义；
10. Snapshot 迁移矩阵；
11. 数据库迁移策略；
12. 文件级 TDD 实施计划。

设计确认后再编码。

---

# 二十五、文档

新增或更新：

- `docs/milestone_04_design.md`
- `docs/milestone_04_test_plan.md`
- `docs/milestone_04_report.md`
- `docs/architecture.md`
- `docs/security_model.md`
- `docs/implementation_plan.md`
- `README.md`
- `.env.example`

必要 ADR：

- Provider SDK seam；
- `store=False` 和本地恢复真相；
- 一轮单 ToolCall 和 `parallel_tool_calls=False`；
- call_id 持久化；
- strict schema 转换；
- retry 和 physical request 计数；
- Context 压缩；
- Loop Detection；
- Snapshot v2 和数据库迁移。

文档不得声称未执行的 Live 能力已经通过。

---

# 二十六、最终质量验收

运行：

```powershell
uv run --frozen pytest -ra
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen python -m compileall src
git diff --check
```

另外检查：

- M0-M3 全部回归；
- 默认测试无网络；
- Live marker 未启用时安全 skip；
- API Key 不进入 Git、数据库、Event、Checkpoint、日志和异常；
- Provider 原始对象不持久化；
- pyproject 和 uv.lock 一致；
- uv.lock 无阿里云源；
- Python 仍为 3.14.3；
- 暂存区为空；
- 没有 commit；
- 没有 push。

---

# 二十七、完成报告格式

## 1. 初始架构审计

- Provider 设计
- Runtime 接口变化
- 数据库和 Snapshot 变化
- 主要风险

## 2. M4A 实现

- Model Domain
- Responses Schema Adapter
- OpenAIModelProvider
- call_id
- Retry/Attempt
- Model Budget
- Model Events

## 3. M4B 实现

- ContextBuilder
- ToolResultRenderer
- Context Compression
- Loop Detection
- Snapshot v2
- Runtime Integration

## 4. 文件变更

列出新增、修改和删除文件。

## 5. Provider 安全边界

- API Key
- `store=False`
- 数据外发
- 重试
- 错误映射
- Provider 状态与本地状态

## 6. Budget

- turn/request 区分
- 请求前限制
- 真实 Token 累计
- restart 语义

## 7. Context 策略

- 保留规则
- 压缩规则
- 大小限制
- 敏感信息规则

## 8. Loop Detection

- digest
- warning/终止阈值
- reset 语义
- restart 语义

## 9. Snapshot 和数据库迁移

- 未版本化 checkpoint
- v1 -> v2
- v2
- 未知版本
- M3 崩溃窗口

## 10. 测试结果

- collected
- passed
- failed
- skipped
- offline acceptance
- Live Test 是否执行
- Live Test 结果

## 11. 静态检查

- Ruff
- mypy
- compileall
- git diff --check

## 12. 依赖变化

- openai 版本
- pyproject.toml
- uv.lock
- Python
- 依赖源

## 13. 未完成内容

必须明确列出。

## 14. 已知问题和技术债

必须明确列出。

## 15. Milestone 4 判定

分别给出：

- Offline M4：PASS / CONDITIONAL PASS / FAIL
- Live Validation：PASS / FAIL / NOT RUN

## 16. Git 状态

确认：

- 当前分支 `feat/milestone-4`
- 没有 commit
- 没有 push
- 暂存区状态
- 工作区修改列表

完成后停止，不进入 Milestone 5，不创建 commit。
