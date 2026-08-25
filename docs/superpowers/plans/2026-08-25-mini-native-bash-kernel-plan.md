# 完整 mini-SWE bash 修复内核 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline) or superpowers:subagent-driven-development (recommended) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 AgentForge 控制平面内完整复现冻结版 mini-SWE-agent 的 bash 修复循环，并通过 AgentForge 的审批、审计、checkpoint、恢复和候选补丁发布完成真实 SWE-bench 修复。

**Architecture:** Vendor 上游 action/prompt/observation/loop 逻辑；新增 Docker workspace bash environment 和轻量命令分类；AgentForge host 在命令边界接入现有 ToolExecutor、MutationCoordinator、TestExecutionCoordinator、EventRepository 和 RuntimeSnapshot。`MINI_NATIVE` 使用新 bash loop，native/mini_linear 不变。

**Tech Stack:** Python 3.11+, Pydantic, asyncio, SQLAlchemy/SQLite, Docker CLI, pytest, uv, SWE-bench Verified harness。

---

### Task 1: Vendor upstream bash protocol

**Files:**
- Create: `src/agentforge/repair_engines/mini_native/vendor/bash_protocol.py`
- Modify: `src/agentforge/repair_engines/mini_native/vendor/NOTICE.md`
- Test: `tests/unit/test_mini_native_bash_protocol.py`

- [ ] **Step 1: Write failing parity tests**

Add tests that assert the public protocol:

```python
def test_bash_tool_schema_matches_upstream_shape():
    assert bash_tool_schema() == {
        "type": "function",
        "name": "bash",
        "description": "Execute a bash command",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The bash command to execute"}},
            "required": ["command"],
        },
    }

def test_observation_formatter_preserves_short_and_long_output():
    assert format_observation({"output": "ok", "returncode": 0}) == '{"returncode": 0, "output": "ok"}'
    long = format_observation({"output": "x" * 12_000, "returncode": 1})
    assert '"output_head"' in long and '"output_tail"' in long

def test_submit_marker_is_detected_only_as_first_output_line():
    assert parse_submit_output("COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\npatch") == "patch"
    assert parse_submit_output("prefix\nCOMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT") is None
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run --frozen pytest tests/unit/test_mini_native_bash_protocol.py -q
```

Expected: import or missing-function failures.

- [ ] **Step 3: Implement the protocol module**

Implement `bash_tool_schema`, `format_observation`, `parse_submit_output`, and the
frozen upstream system/instance templates. Keep the output fields and JSON shape
identical to the pinned mini-SWE-agent source; do not add AgentForge fields to the
model-facing observation.

- [ ] **Step 4: Run parity tests and static checks**

```bash
uv run --frozen pytest tests/unit/test_mini_native_bash_protocol.py -q
uv run --frozen ruff check src/agentforge/repair_engines/mini_native/vendor/bash_protocol.py tests/unit/test_mini_native_bash_protocol.py
```

- [ ] **Step 5: Commit**

```bash
git add src/agentforge/repair_engines/mini_native/vendor tests/unit/test_mini_native_bash_protocol.py
git commit -m "feat(repair): vendor mini-swe bash protocol"
```

### Task 2: Add Docker bash environment and command classifier

**Files:**
- Create: `src/agentforge/repair_engines/mini_native/environment.py`
- Create: `src/agentforge/repair_engines/mini_native/classifier.py`
- Test: `tests/unit/test_mini_native_environment.py`

- [ ] **Step 1: Write failing environment tests**

```python
@pytest.mark.asyncio
async def test_environment_executes_bash_in_workspace():
    env = FakeBashEnvironment()
    result = await env.execute("printf 'ok'", cwd="/workspace", timeout_seconds=5)
    assert result.returncode == 0
    assert result.output == "ok"

def test_classifier_splits_read_test_write():
    assert classify_command("rg -n bug src") == CommandKind.READ
    assert classify_command("python -m pytest tests/test_bug.py") == CommandKind.TEST
    assert classify_command("sed -i 's/old/new/' src/a.py") == CommandKind.WRITE
    assert classify_command("custom_tool --repair") == CommandKind.OTHER
```

- [ ] **Step 2: Run RED**

```bash
uv run --frozen pytest tests/unit/test_mini_native_environment.py -q
```

- [ ] **Step 3: Implement environment and classifier**

`DockerBashEnvironment` starts or reuses the task container and invokes
`docker exec -w <cwd> <container> bash -lc <command>`. It returns a
`BashObservation` with `output`, `returncode`, `exception_info`, `timed_out`, and
`duration_ms`. The classifier is a short command-prefix/redirect heuristic only;
it must not parse shell into a second language or reject unknown commands.

- [ ] **Step 4: Run tests and commit**

```bash
uv run --frozen pytest tests/unit/test_mini_native_environment.py -q
uv run --frozen ruff check src/agentforge/repair_engines/mini_native/environment.py src/agentforge/repair_engines/mini_native/classifier.py tests/unit/test_mini_native_environment.py
git add src/agentforge/repair_engines/mini_native/environment.py src/agentforge/repair_engines/mini_native/classifier.py tests/unit/test_mini_native_environment.py
git commit -m "feat(repair): add mini-swe bash environment"
```

### Task 3: Replace the model-facing mini-native loop with bash

**Files:**
- Modify: `src/agentforge/repair_engines/mini_native/loop.py`
- Modify: `src/agentforge/repair_engines/mini_native/contracts.py`
- Test: `tests/unit/test_mini_native_bash_loop.py`

- [ ] **Step 1: Write failing loop parity test**

Use a fake model returning bash calls and a fake environment returning observations:

```python
@pytest.mark.asyncio
async def test_bash_loop_repeats_observation_until_submit():
    result = await MiniNativeRepairEngine(host).run(
        run_id=run_id, task="fix bug", max_steps=4, working_directory="/workspace"
    )
    assert host.commands == ["ls", "sed -i 's/old/new/' src/a.py", "python -m pytest -q"]
    assert result.submitted
    assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in host.requests[-1].history[-1]["output"]
```

- [ ] **Step 2: Run RED**

```bash
uv run --frozen pytest tests/unit/test_mini_native_bash_loop.py -q
```

- [ ] **Step 3: Implement bash loop**

The loop must build `ModelRequest` with exactly one `bash` tool and upstream prompt,
parse one action, call the host environment, append the unmodified upstream
observation, checkpoint, and stop only on the upstream submit marker. Remove the
model-facing specialized read/edit/test tool list from this path. Keep the existing
`RepairAction` conversion only as an internal audit representation.

- [ ] **Step 4: Run loop tests and commit**

```bash
uv run --frozen pytest tests/unit/test_mini_native_bash_loop.py tests/unit/test_mini_native_loop.py -q
uv run --frozen mypy src/agentforge/repair_engines/mini_native/loop.py
git add src/agentforge/repair_engines/mini_native/loop.py src/agentforge/repair_engines/mini_native/contracts.py tests/unit/test_mini_native_bash_loop.py
git commit -m "feat(repair): run mini-native loop with bash actions"
```

### Task 4: Bridge bash actions into approvals, tests, events, and recovery

**Files:**
- Modify: `src/agentforge/repair_engines/mini_native/agentforge_host.py`
- Modify: `src/agentforge/runtime/engine.py`
- Modify: `src/agentforge/runtime/snapshots.py`
- Test: `tests/integration/test_mini_native_bash_runtime.py`

- [ ] **Step 1: Write failing product integration test**

Exercise a real AgentApplication/Runtime factory with this sequence:

```text
bash `rg` -> automatic execution
bash `sed -i` -> approval requested
approve + resume -> mutation committed
bash `python -m pytest` -> managed test result
bash submit marker -> candidate publish approval
approve + resume -> candidate patch saved
```

Assert the run id is stable, events contain `MODEL_REQUESTED`, `BASH_REQUESTED`,
`APPROVAL_REQUESTED`, `BASH_COMPLETED`, `CHECKPOINT_SAVED`, and `RUN_COMPLETED`, and
the command is not replayed after resume.

- [ ] **Step 2: Run RED**

```bash
uv run --frozen pytest tests/integration/test_mini_native_bash_runtime.py -q
```

- [ ] **Step 3: Implement the bridge**

`AgentForgeMiniNativeHost.execute_bash` classifies the command, records the action,
and routes read/other to the environment, writes through the existing mutation
approval path, tests through the existing test coordinator, and submit through the
candidate publisher. Add only the snapshot fields needed to persist pending bash
command, observation, and submit state. Resume consumes a claimed approval once and
continues from the saved observation.

- [ ] **Step 4: Run integration/static checks and commit**

```bash
uv run --frozen pytest tests/integration/test_mini_native_bash_runtime.py tests/integration/test_mini_native_recovery.py -q
uv run --frozen ruff check src/agentforge/repair_engines/mini_native src/agentforge/runtime
uv run --frozen mypy src/agentforge/repair_engines/mini_native/agentforge_host.py src/agentforge/runtime/engine.py
git add src/agentforge/repair_engines/mini_native src/agentforge/runtime
git commit -m "feat(runtime): bridge mini-swe bash through approvals"
```

### Task 5: Wire runtime factory and retain existing engines

**Files:**
- Modify: `src/agentforge/application/runtime_factory.py`
- Modify: `src/agentforge/evaluation/pilot_factory.py`
- Test: `tests/integration/test_shared_runtime_factory.py`

- [ ] **Step 1: Add factory regression**

Assert `RepairEngineKind.MINI_NATIVE` creates the bash environment/host while
`NATIVE` and `MINI_LINEAR` keep their current tool registry and behavior.

- [ ] **Step 2: Implement the wiring**

Pass the task Docker container/workspace and `DockerBashEnvironment` only to the
mini-native host. Do not register bash as a model-facing tool for other engines.

- [ ] **Step 3: Verify and commit**

```bash
uv run --frozen pytest tests/integration/test_shared_runtime_factory.py tests/integration/test_mini_native_bash_runtime.py -q
git add src/agentforge/application/runtime_factory.py src/agentforge/evaluation/pilot_factory.py tests/integration/test_shared_runtime_factory.py
git commit -m "feat(runtime): wire full mini-native bash engine"
```

### Task 6: End-to-end public-task smoke and canary

**Files:**
- Modify: `evaluation/run_verified10_comparison.py`
- Test: `tests/integration/test_verified10_comparison_cli.py`
- Create: `docs/verified10-mini-native-bash-report.zh-CN.md`

- [ ] **Step 1: Add CLI contract test**

Assert the existing `prepare`, `run-agentforge`, `status`, and `finalize` commands
select the bash engine without changing the frozen protocol digest.

- [ ] **Step 2: Run local contract tests**

```bash
uv run --frozen pytest tests/integration/test_verified10_comparison_cli.py -q
```

- [ ] **Step 3: Sync only the committed source and protocol to Tencent Cloud**

Run the existing pinned prepare command with the fixed 10-task protocol, then start
one real task through the relay API. Stop immediately if the task is still reading
without an edit after 20 model calls.

- [ ] **Step 4: Run the fixed canary and official harness**

Record per-task model calls, command counts, approvals, tests, resolved status,
failure category, wall time, and available token usage. Do not claim success from
AgentForge `RUN_COMPLETED`; use the official SWE-bench report.

- [ ] **Step 5: Write report and commit**

```bash
git add docs/verified10-mini-native-bash-report.zh-CN.md tests/integration/test_verified10_comparison_cli.py evaluation/run_verified10_comparison.py
git commit -m "docs(evaluation): report mini-native bash canary"
```

## Verification gate

Before claiming completion, run:

```bash
uv run --frozen pytest tests/unit/test_mini_native_bash_protocol.py tests/unit/test_mini_native_environment.py tests/unit/test_mini_native_bash_loop.py tests/integration/test_mini_native_bash_runtime.py tests/integration/test_mini_native_recovery.py -q
uv run --frozen ruff check src tests
uv run --frozen mypy src/agentforge/repair_engines/mini_native src/agentforge/runtime/engine.py
git diff --check
```

Completion requires a real public-task result and an official harness report; local
unit/integration tests alone are not evidence that the repair capability works.
