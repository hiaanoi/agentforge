# AgentForge mini-native Control Plane Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Embed the proven mini-SWE-agent repair loop inside AgentForge so the existing control plane owns every model/tool/approval/checkpoint event and the resulting system exceeds 31/50 on the frozen SWE-bench Verified protocol.

**Architecture:** Add a `MINI_NATIVE` engine beside `NATIVE` and `MINI_LINEAR`. Vendor only the mini-SWE-agent core loop and tools at the pinned upstream commit, and connect it to a small AgentForge host protocol for model calls, tool execution, approvals, checkpoints, telemetry, and candidate publication. Keep the current `AgentRuntime` and persistence workflows as the source of truth; the vendored loop never writes the database directly.

**Tech Stack:** Python 3.11/3.14, Pydantic, asyncio, existing AgentForge runtime/persistence/mutation/test coordinators, pinned mini-SWE-agent source, pytest, ruff, mypy, Docker SWE-bench harness.

---

### Task 1: Register the new engine and lock the vendored source

**Files:**
- Modify: `src/agentforge/repair_engines/models.py`
- Modify: `src/agentforge/application/runtime_factory.py`
- Modify: `src/agentforge/runtime/engine.py`
- Create: `src/agentforge/repair_engines/mini_native/__init__.py`
- Create: `src/agentforge/repair_engines/mini_native/NOTICE.md`
- Modify: `THIRD_PARTY_NOTICES.md`
- Test: `tests/unit/test_repair_engine_config.py`

- [ ] **Step 1: Write the failing engine-selection test**

Add a test that parses `repair_engine = "mini_native"`, asserts
`RepairEngineKind.MINI_NATIVE`, and asserts the runtime factory accepts the value
without selecting the existing `mini_linear` candidate path.

- [ ] **Step 2: Run the focused test and confirm it fails**

Run:

```bash
uv run --frozen pytest tests/unit/test_repair_engine_config.py -q
```

Expected: failure because `MINI_NATIVE` is not an enum member.

- [ ] **Step 3: Add the enum and source notice**

Add `MINI_NATIVE = "mini_native"` to `RepairEngineKind`. Create `NOTICE.md` with
the exact upstream repository, commit `25941c89cfbc91eb40b3f8756348c91d9977d57e`,
license path, and the statement that only the core loop/tools are vendored.
Update the top-level third-party notice index to link to that file.

- [ ] **Step 4: Run the focused test and static checks**

Run:

```bash
uv run --frozen pytest tests/unit/test_repair_engine_config.py -q
uv run --frozen ruff check src/agentforge/repair_engines/models.py src/agentforge/application/runtime_factory.py src/agentforge/runtime/engine.py
uv run --frozen mypy src/agentforge/repair_engines/models.py src/agentforge/application/runtime_factory.py src/agentforge/runtime/engine.py
```

Expected: focused tests pass; ruff and mypy report no errors.

- [ ] **Step 5: Commit the engine registration**

```bash
git add src/agentforge/repair_engines/models.py src/agentforge/repair_engines/mini_native THIRD_PARTY_NOTICES.md tests/unit/test_repair_engine_config.py
git commit -m "feat(repair): register mini-native engine"
```

### Task 2: Define the AgentForge host protocol for vendored actions

**Files:**
- Create: `src/agentforge/repair_engines/mini_native/contracts.py`
- Create: `src/agentforge/repair_engines/mini_native/host.py`
- Test: `tests/unit/test_mini_native_host.py`

- [ ] **Step 1: Write failing contract tests**

Test the public `RepairAction`/`RepairActionResult` models with four action kinds:
`READ`, `WRITE`, `TEST`, and `FINAL`. Assert that a write action contains a
non-empty approval key, that a result preserves `returncode`, bounded output, and
duration, and that serialized arguments never include an API key.

- [ ] **Step 2: Run the tests and confirm the new module is missing**

```bash
uv run --frozen pytest tests/unit/test_mini_native_host.py -q
```

Expected: collection failure because `mini_native.contracts` does not exist.

- [ ] **Step 3: Implement the narrow host protocol**

Define:

```python
class MiniNativeHost(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...
    async def execute(self, action: RepairAction) -> RepairActionResult: ...
    async def checkpoint(self, state: MiniNativeState) -> None: ...
    async def publish(self, run_id: UUID) -> CandidatePatchResult: ...
```

Keep the vendored loop dependent only on this protocol. Put action classification,
secret-safe argument summaries, output truncation, and deterministic action ids in
`host.py`; do not import repositories or SQLAlchemy in the vendored package.

- [ ] **Step 4: Run focused tests and static checks**

```bash
uv run --frozen pytest tests/unit/test_mini_native_host.py -q
uv run --frozen ruff check src/agentforge/repair_engines/mini_native
uv run --frozen mypy src/agentforge/repair_engines/mini_native
```

Expected: all focused tests pass and static checks are clean.

- [ ] **Step 5: Commit the host boundary**

```bash
git add src/agentforge/repair_engines/mini_native tests/unit/test_mini_native_host.py
git commit -m "feat(repair): add mini-native host protocol"
```

### Task 3: Vendor and port the mini-SWE-agent core loop

**Files:**
- Create: `src/agentforge/repair_engines/mini_native/vendor/__init__.py`
- Create: `src/agentforge/repair_engines/mini_native/vendor/loop.py`
- Create: `src/agentforge/repair_engines/mini_native/vendor/context.py`
- Create: `src/agentforge/repair_engines/mini_native/loop.py`
- Test: `tests/unit/test_mini_native_loop.py`

- [ ] **Step 1: Add a deterministic fake-host loop test**

Use a fake host that returns the sequence `read → edit → test(fail) → edit → test(pass) → final`.
Assert that the loop executes all six actions, includes the failed test output in the
next model request, and returns a submitted result only after the passing test.

- [ ] **Step 2: Run the test and confirm it fails**

```bash
uv run --frozen pytest tests/unit/test_mini_native_loop.py -q
```

Expected: failure because `MiniNativeRepairEngine` is not implemented.

- [ ] **Step 3: Copy only the pinned core and add the adapter**

Copy the upstream loop/context-compaction logic into `vendor/`, preserving the
upstream license header. Adapt its model/tool boundary to `MiniNativeHost` in
`loop.py`. The adapter must preserve the upstream ordering of observation, model
request, tool result, and context compaction; it must not call subprocess directly.

- [ ] **Step 4: Run the loop tests and static checks**

```bash
uv run --frozen pytest tests/unit/test_mini_native_loop.py -q
uv run --frozen ruff check src/agentforge/repair_engines/mini_native
uv run --frozen mypy src/agentforge/repair_engines/mini_native
```

Expected: the fake-host loop passes and static checks are clean.

- [ ] **Step 5: Commit the vendored loop**

```bash
git add src/agentforge/repair_engines/mini_native tests/unit/test_mini_native_loop.py
git commit -m "feat(repair): embed mini-swe repair loop"
```

### Task 4: Bridge actions into AgentForge mutation and test workflows

**Files:**
- Modify: `src/agentforge/runtime/engine.py`
- Modify: `src/agentforge/application/runtime_factory.py`
- Create: `src/agentforge/repair_engines/mini_native/agentforge_host.py`
- Test: `tests/integration/test_mini_native_runtime.py`
- Test: `tests/integration/test_candidate_patch_approval.py`

- [ ] **Step 1: Write the integration test for one complete action sequence**

Build a real `RuntimeComponentFactory` fixture with `RepairEngineKind.MINI_NATIVE`.
Drive a run through read, write approval, resume, test failure, second write
approval, passing test, and candidate publish. Assert that the run id is stable,
each mutation has an approval binding, event rows exist for every action, and the
published patch excludes `.agentforge`.

- [ ] **Step 2: Run the integration test and capture the first failure**

```bash
uv run --frozen pytest tests/integration/test_mini_native_runtime.py -q
```

Expected: failure because the runtime factory has no MINI_NATIVE host wiring.

- [ ] **Step 3: Implement the AgentForge host**

Implement `AgentForgeMiniNativeHost` using the existing `ModelExecutor`,
`MutationCoordinator`, `TestExecutionCoordinator`, `EventRepository`,
`CheckpointRepository`, `CandidatePatchPublisher`, and `CandidatePatchStore`.
Route read actions to the existing read tools, write actions to the mutation
coordinator, tests to the test coordinator, and final publication to the existing
candidate tool. Use the current `RunOwnership` authority for every persistence
write.

- [ ] **Step 4: Wire MINI_NATIVE into `AgentRuntime`**

Add one branch beside `_run_mini_linear` in `_execute_owned`. Construct the native
engine with `AgentForgeMiniNativeHost`; do not alter the existing NATIVE or
MINI_LINEAR branches. Update `RuntimeComponentFactory` so MINI_NATIVE receives the
candidate publisher/store and the same tool registry needed by MINI_LINEAR.

- [ ] **Step 5: Run integration and regression tests**

```bash
uv run --frozen pytest tests/integration/test_mini_native_runtime.py tests/integration/test_candidate_patch_approval.py tests/integration/test_shared_runtime_factory.py -q
uv run --frozen ruff check src/agentforge/runtime/engine.py src/agentforge/application/runtime_factory.py src/agentforge/repair_engines/mini_native
uv run --frozen mypy src/agentforge/runtime/engine.py src/agentforge/application/runtime_factory.py src/agentforge/repair_engines/mini_native
```

Expected: the new lifecycle test and existing approval/factory tests pass.

- [ ] **Step 6: Commit the host integration**

```bash
git add src/agentforge/runtime/engine.py src/agentforge/application/runtime_factory.py src/agentforge/repair_engines/mini_native tests/integration/test_mini_native_runtime.py tests/integration/test_candidate_patch_approval.py tests/integration/test_shared_runtime_factory.py
git commit -m "feat(runtime): route mini-native actions through control plane"
```

### Task 5: Add durable recovery and telemetry parity

**Files:**
- Modify: `src/agentforge/repair_engines/mini_native/contracts.py`
- Modify: `src/agentforge/repair_engines/mini_native/loop.py`
- Modify: `src/agentforge/repair_engines/mini_native/agentforge_host.py`
- Modify: `src/agentforge/runtime/engine.py`
- Test: `tests/integration/test_mini_native_recovery.py`
- Test: `tests/unit/test_mini_native_telemetry.py`

- [ ] **Step 1: Write recovery and telemetry tests**

Test interruption after a model response and interruption while waiting for a
mutation approval. Resume from the saved checkpoint and assert no duplicate edit,
the same run id, monotonically increasing event cursor, and one final candidate.
Test that provider usage fields are persisted when present and are explicitly
`unavailable` when absent.

- [ ] **Step 2: Run the tests and confirm they fail**

```bash
uv run --frozen pytest tests/integration/test_mini_native_recovery.py tests/unit/test_mini_native_telemetry.py -q
```

Expected: failures for missing state serialization and telemetry projection.

- [ ] **Step 3: Persist loop state at every action boundary**

Store the current model-call id, action id, history digest, pending approval id,
last test result, and next step in the existing checkpoint payload. On resume,
replay only the uncommitted action and continue from the recorded next step.

- [ ] **Step 4: Add telemetry projection**

Map provider usage, model calls, tool calls, approvals, edits, tests, wall time,
and failure class into the existing evaluation telemetry record. Preserve `None`
or an explicit unavailable marker when the provider omits usage; never infer tokens
from character counts.

- [ ] **Step 5: Run recovery, telemetry, and full existing runtime tests**

```bash
uv run --frozen pytest tests/integration/test_mini_native_recovery.py tests/unit/test_mini_native_telemetry.py tests/integration/test_shared_runtime_factory.py tests/integration/test_candidate_patch_approval.py -q
```

Expected: all selected tests pass with zero failures.

- [ ] **Step 6: Commit recovery and telemetry**

```bash
git add src/agentforge/repair_engines/mini_native src/agentforge/runtime/engine.py tests/integration/test_mini_native_recovery.py tests/unit/test_mini_native_telemetry.py
git commit -m "feat(runtime): make mini-native resumable and measurable"
```

### Task 6: Add the engine to the evaluation protocol and canary runner

**Files:**
- Modify: `src/agentforge/evaluation/verified10_runner.py`
- Modify: `src/agentforge/evaluation/verified10_cli.py`
- Modify: `src/agentforge/evaluation/verified10_support.py`
- Test: `tests/integration/test_verified10_comparison_cli.py`
- Create: `evaluation/protocols/verified50-openai-gpt54mini-mini-native-canary.json`

- [ ] **Step 1: Write the CLI contract test**

Add a test that accepts `--repair-engine mini_native`, writes the selected engine
into the generated runtime config, preserves the frozen protocol digest, and keeps
the same model, temperature, task order, Docker image binding, and budget values.

- [ ] **Step 2: Run the focused CLI test and confirm it fails**

```bash
uv run --frozen pytest tests/integration/test_verified10_comparison_cli.py -q -k mini_native
```

Expected: parser or runtime-config assertion failure because the new engine is not
yet accepted by the evaluation runner.

- [ ] **Step 3: Wire the new engine into evaluation**

Add `mini_native` to the CLI choices and pass it through `_preflight_agentforge`,
the runtime config generator, and `_execute_agentforge`. Do not alter the frozen
task list or model identity. Add a deterministic 10-task canary protocol that is a
projection of the existing 50-task protocol and records the parent protocol hash.

- [ ] **Step 4: Run CLI and protocol tests**

```bash
uv run --frozen pytest tests/integration/test_verified10_comparison_cli.py -q -k 'mini_native or protocol'
uv run --frozen ruff check src/agentforge/evaluation/verified10_runner.py src/agentforge/evaluation/verified10_cli.py src/agentforge/evaluation/verified10_support.py
uv run --frozen mypy src/agentforge/evaluation/verified10_runner.py src/agentforge/evaluation/verified10_cli.py src/agentforge/evaluation/verified10_support.py
```

Expected: all selected tests pass and static checks are clean.

- [ ] **Step 5: Commit the evaluation binding**

```bash
git add src/agentforge/evaluation evaluation/protocols/verified50-openai-gpt54mini-mini-native-canary.json tests/integration/test_verified10_comparison_cli.py
git commit -m "feat(evaluation): add mini-native canary protocol"
```

### Task 7: Run the 10-task canary and enforce the promotion gate

**Files:**
- Use: `evaluation/run_verified10_comparison.py`
- Use: `evaluation/protocols/verified50-openai-gpt54mini-mini-native-canary.json`
- Create: `docs/evaluation/mini-native-canary-2026-08-25.md`

- [ ] **Step 1: Prepare the canary on the evaluation server**

Run the existing `prepare` command with the pinned harness, cached full dataset,
and a new output directory; verify `agentforge_admission=10/10` and all image
digests are present.

- [ ] **Step 2: Run AgentForge mini-native with the same relay/model**

Run `run-agentforge --repair-engine mini_native --provider-kind openai --model gpt-5.4-mini`
inside tmux. Keep the API key only in the tmux environment and run `status` until
all ten attempts are terminal.

- [ ] **Step 3: Finalize and score the canary with the official harness**

Run `finalize-predictions`, `score`, and `report`. Record resolved count, empty
patch count, protocol failures, model calls, token telemetry, and wall time.

- [ ] **Step 4: Apply the promotion gate**

Promote to the 50-task run only if the canary has zero tool protocol failures,
empty patches ≤ 1, and resolved count at least equal to mini-SWE-agent on the same
ten task ids. Otherwise stop and inspect the per-task trajectories before changing
the engine.

- [ ] **Step 5: Commit the canary report**

```bash
git add docs/evaluation/mini-native-canary-2026-08-25.md
git commit -m "docs(evaluation): record mini-native canary gate"
```

### Task 8: Run the promoted 50-task comparison and publish the result

**Files:**
- Use: `evaluation/protocols/verified50-openai-gpt54mini.json`
- Use: `evaluation/run_verified10_comparison.py`
- Modify: `docs/verified50-gpt54mini-paired-report.zh-CN.md`

- [ ] **Step 1: Run the full 50-task mini-native arm**

Use a fresh output directory and the exact frozen protocol. Record `status` until
all 50 attempts are terminal; preserve the prior AgentForge and official mini
baselines unchanged.

- [ ] **Step 2: Export and score mini-native predictions**

Run the existing finalization and official harness score commands with the cached
50-row dataset, `--max_workers 4`, and the same Docker image digests.

- [ ] **Step 3: Compute the paired comparison**

Run `report` and verify the JSON contains 50 instance rows, both score metadata
files, predictions hashes, ledger hashes, resolved counts, failure classes, model
calls, wall time, and token availability markers.

- [ ] **Step 4: Apply the final success gate**

Declare the repair capability improved only if mini-native resolves at least 32/50,
has zero official infrastructure failures, and preserves the approval/checkpoint/
audit evidence for every terminal attempt. If it resolves ≤31/50, stop feature work
on additional control-plane layers and diagnose the repair loop using the paired
trajectories.

- [ ] **Step 5: Update and commit the recruiting report**

Update `docs/verified50-gpt54mini-paired-report.zh-CN.md` with the mini-native row,
paired wins/losses, failure taxonomy, token telemetry limitations, and exact artifact
directory. Run `git diff --check`, then:

```bash
git add docs/verified50-gpt54mini-paired-report.zh-CN.md
git commit -m "docs(evaluation): publish mini-native comparison"
```

## Self-review checklist

- The design spec's core-loop embedding, host callbacks, approval bridge,
  checkpoint/recovery, telemetry, canary, and 50-task promotion gate each have a
  dedicated task above.
- No task changes the frozen dataset, model, Docker harness, or official score
  definition.
- `RepairEngineKind.MINI_NATIVE`, `MiniNativeHost`, `MiniNativeRepairEngine`, and
  the evaluation CLI flag are named consistently across all tasks.
- The only intentional unmeasured quantity is mini-SWE-agent token usage; the plan
  records it as unavailable instead of estimating cost from calls.
