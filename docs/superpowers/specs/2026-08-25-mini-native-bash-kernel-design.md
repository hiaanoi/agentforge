# AgentForge 完整 mini-SWE bash 修复内核设计

## 目标

在 AgentForge 中完整复现冻结版本 mini-SWE-agent 的修复交互：模型使用原生
`bash` 工具、原生工作流提示、Responses API 工具调用格式、命令观察结果和
上下文压缩；AgentForge 只负责运行生命周期、审批、持久化、审计、恢复、候选
补丁发布和 SWE-bench 评测。目标是在同一模型、同一 Docker workspace、同一
官方 harness 下，使修复能力至少达到并争取超过官方 mini-SWE-agent。

## 背景判断

现有 `mini_native` 已经接通 AgentForge 的 run、approval、checkpoint 和
telemetry，但给模型暴露的是 `read_file/edit_file/run_tests` 专用工具。这与
mini-SWE-agent 的 `bash` 交互不同，导致模型在 SWE-bench 任务中反复读取文件，
无法稳定进入编辑和测试阶段。继续修补专用工具映射不能恢复原始修复能力，必须
把上游修复内核和命令观察格式完整搬入。

## 范围

### 纳入

- 固定上游 mini-SWE-agent commit `25941c89cfbc91eb40b3f8756348c91d9977d57e`。
- 上游核心 agent loop、Responses API `bash` tool schema、action parser、
  observation formatter、instance/system prompt、上下文压缩和提交标记。
- AgentForge 环境桥接：在现有 SWE-bench Docker workspace 内执行 bash，返回与
  上游相同的 observation payload。
- 每次模型调用、bash action、命令结果、approval、checkpoint、恢复和候选
  patch 的 AgentForge 事件与 SQLite 持久化。
- 写命令的 mutation approval、测试命令的 test coordinator、候选补丁发布和
  官方 harness exporter。
- 模型调用 usage、请求 ID、耗时和不可用 telemetry 标记。

### 不纳入

- mini-SWE-agent CLI、无关 benchmark、文档站和发布脚本的复制。
- 新的沙箱系统、复杂静态 shell 分析、命令白名单语言或第二套数据库。
- 对 native、mini_linear 的行为改写。
- 通过 AgentForge 内部“已完成”状态冒充 SWE-bench resolved；官方 harness 仍是
  唯一能力裁判。

## 方案

### Vendored upstream core

在 `src/agentforge/repair_engines/mini_native/vendor/` 中保留上游所需的最小
源码切片和 MIT/来源说明。vendor 层只依赖抽象 callback，不导入 AgentForge
SQLite、审批或运行时对象。上游 loop 的输入输出保持原形：

```text
build_model_request(history) -> ModelRequest
model.generate(request) -> bash action
environment.execute(action) -> observation
append observation -> next request
```

AgentForge host 负责将这些 callback 绑定到自身的 provider 和 control plane。

### Bash environment bridge

新增 `MiniNativeEnvironment`，接口与上游环境一致：

```python
class MiniNativeEnvironment(Protocol):
    async def execute(
        self, command: str, *, cwd: str, timeout_seconds: float
    ) -> BashObservation: ...
```

实现直接在当前 SWE-bench Docker workspace 执行 `/bin/bash -lc <command>`，
保留 stdout、returncode、timeout 和异常信息的上游 observation 结构。每个命令
执行前后通过 `AgentForgeMiniNativeHost` 写入事件并保存 checkpoint。

### 轻量动作分类

不构建复杂安全解析器，只使用简单、可审计的分类：

- 读类：`ls`, `find`, `grep`, `rg`, `cat`, `sed -n`, `head`, `tail`, `git
  status`, `git diff`, `python` 的纯读取形式，自动执行。
- 测试类：`pytest`, `tox`, `nox`, `python -m pytest` 等，转入现有测试协调器，
  记录 profile、退出码、stdout/stderr 和耗时。
- 写类：包含 `sed -i`, `perl -i`, 重定向、`cp/mv/rm`, 写入脚本或调用编辑
  API 的命令，创建 mutation approval；批准后执行并记录 workspace digest。
- 无法判断的命令：按普通 bash 动作执行并记录，不增加额外规则层。

分类只决定是否暂停和如何记录，不修改命令文本，不改变上游模型交互。

### 提交与补丁

上游提交标记 `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` 被识别为 FINAL action。
AgentForge 捕获 workspace diff，保存候选 patch，并通过现有 candidate publish
approval 发布。最终 patch 不要求 AgentForge 自行判断 SWE-bench 成功，评测由
官方 harness 完成。

## 数据流

```text
StartRun
  -> MiniNativeRepairEngine
  -> upstream prompt + bash schema
  -> provider.generate
  -> BashAction
  -> AgentForge host
       -> classify command
       -> auto read / test approval / mutation approval
       -> Docker bash execution
       -> upstream observation
       -> event + checkpoint
  -> next model request
  -> submit marker
  -> candidate patch approval + exporter
```

恢复从最近的 response/action checkpoint 继续：已完成的 bash 命令不重放，待审批
命令只在批准后执行，模型历史使用上游格式重建。模型调用中断、命令超时、审批
暂停和进程重启都保留同一 run_id、workspace 和事件链。

## 文件边界

- `src/agentforge/repair_engines/mini_native/vendor/`: 上游 loop、prompt、
  action/observation 格式和来源说明。
- `src/agentforge/repair_engines/mini_native/environment.py`: Docker bash
  environment 与 observation 模型。
- `src/agentforge/repair_engines/mini_native/classifier.py`: 轻量命令分类。
- `src/agentforge/repair_engines/mini_native/loop.py`: AgentForge 装配层，不再
  自定义 read/edit/test 工具循环。
- `src/agentforge/repair_engines/mini_native/agentforge_host.py`: provider、
  environment、approval、event、checkpoint 和 candidate bridge。
- `src/agentforge/application/runtime_factory.py`: 仅为 `MINI_NATIVE` 装配
  bash environment 和 host。
- `tests/unit/test_mini_native_bash.py`: action/observation/classifier 行为。
- `tests/integration/test_mini_native_bash_runtime.py`: 真实 Application、
  approval、恢复和 candidate publish 闭环。
- `tests/integration/test_mini_native_upstream_parity.py`: 与冻结上游 prompt、
  tool schema、observation 格式的 parity 断言。

## 验收与停止条件

1. 上游 parity 单测全部通过：模型请求中只有一个 `bash` tool，工具调用和
   observation 可往返解析，提交标记可识别。
2. AgentForge 集成测试通过：bash 读命令自动执行；写命令暂停并批准后继续；
   测试命令有结果；重启恢复不重放已完成动作；候选 patch 可导出。
3. 一个真实 SWE-bench 任务完成“bash 读取 → 编辑 → 测试 → 提交”。
4. 固定 10 题 canary 中工具协议失败为 0，且 resolved 不低于官方 mini 在同
   10 题上的结果，才进入 50 题。
5. 如果 parity 和单题闭环通过但 canary 仍明显低于官方，停止继续加控制平面
   功能，直接基于逐题轨迹判断模型/提示词/环境差异。

## 取舍

此方案优先修复真实能力而非增加边界检查。AgentForge 保留审批和审计，但不尝试
把 bash 变成另一个受限 DSL；这样才能让同一个模型看到与官方 mini-SWE-agent
一致的修复工作流，并让对比结果具有说服力。
