# Mini Linear Repair Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` task-by-task. Keep the scope to the adapter and its evidence; do not generalize it into a plugin platform.

**Goal:** Run the proven mini-SWE-agent linear repair loop inside an AgentForge-owned candidate workspace, then publish only its final patch through AgentForge approval, audit, and verification.

**Architecture:** `MiniLinearRepairEngine` is one concrete repair-engine adapter, selected by a single `repair_engine` configuration value. It owns prompt/history/action iteration in a disposable candidate workspace; AgentForge owns candidate creation, step budget, event persistence, final patch validation, one approval, publication, and final verification. The existing native runtime stays unchanged and selectable for comparison.

**Tech Stack:** Python, existing AgentForge runtime/persistence, pinned mini-SWE-agent v2.4.6 behavior and MIT attribution, Docker-backed SWE-bench workspace, pytest, official SWE-bench harness.

---

## Explicit scope cuts

- No generic plugin registry.
- No per-shell-command human approval in the first version; commands run only in an unpublished candidate workspace.
- No multi-agent planning, retrieval, memory database, or new vector store.
- No new production database table unless an existing checkpoint cannot carry one small engine snapshot.
- No claim that AgentForge audit is superior until the fault/recovery comparison is run.

## Acceptance gate

1. On the existing Pass 2 ten tasks, the adapter produces at least the same number of submitted patches as native mini under the same model and 100-step cap.
2. On a new frozen holdout, hybrid repair score is not lower than native mini by more than the measured pilot variance.
3. Only a final patch can reach the canonical workspace, and it must pass existing path/diff policy plus one approval.
4. Existing full suite remains green.

### Task 1: Introduce one small repair-engine seam

**Files:**

- Create: `src/agentforge/repair_engines/protocol.py`
- Create: `src/agentforge/repair_engines/models.py`
- Modify: `src/agentforge/application/config.py`
- Modify: `src/agentforge/application/runtime_factory.py`
- Test: `tests/unit/test_repair_engine_config.py`

- [ ] **Step 1: Write the failing configuration test**

```python
def test_product_config_accepts_only_native_or_mini_linear_engine() -> None:
    assert load_config({"repair_engine": "mini_linear"}).repair_engine == "mini_linear"
    with pytest.raises(UnsafeConfigurationError):
        load_config({"repair_engine": "anything_else"})
```

- [ ] **Step 2: Run the test and observe RED**

```bash
uv run --frozen pytest tests/unit/test_repair_engine_config.py -q
```

Expected: unknown configuration field rejection.

- [ ] **Step 3: Add only these types**

```python
class RepairEngine(Protocol):
    async def run(self, request: RepairEngineRequest) -> RepairEngineResult: ...

class RepairEngineKind(StrEnum):
    NATIVE = "native"
    MINI_LINEAR = "mini_linear"
```

`RepairEngineRequest` contains task text, candidate root, max steps, model provider, and a callback that records a completed model turn. `RepairEngineResult` contains final candidate patch, submitted flag, model calls, and serialized linear history.

- [ ] **Step 4: Register native as the default adapter**

The runtime factory selects `NativeRepairEngine` unless config explicitly says `mini_linear`. Do not move native runtime logic in this task; the native adapter may delegate to the existing path.

- [ ] **Step 5: Verify and commit**

```bash
uv run --frozen pytest tests/unit/test_repair_engine_config.py tests/integration/test_shared_runtime_factory.py -q
git add src/agentforge/repair_engines src/agentforge/application/config.py src/agentforge/application/runtime_factory.py tests/unit/test_repair_engine_config.py
git commit -m "feat(runtime): add repair engine selection seam"
```

### Task 2: Build an unpublished candidate workspace

**Files:**

- Create: `src/agentforge/runtime/candidate_workspace.py`
- Modify: `src/agentforge/runtime/engine.py`
- Test: `tests/integration/test_mini_candidate_workspace.py`

- [ ] **Step 1: Write the failing isolation test**

```python
async def test_candidate_write_does_not_change_canonical_workspace_before_approval(tmp_path):
    run = await start_mini_linear_run(tmp_path)
    await execute_candidate_command(run, "printf 'x=2\\n' > src/module.py")
    assert canonical_text(run, "src/module.py") == "x=1\n"
    assert candidate_text(run, "src/module.py") == "x=2\n"
```

- [ ] **Step 2: Run RED**

```bash
uv run --frozen pytest tests/integration/test_mini_candidate_workspace.py::test_candidate_write_does_not_change_canonical_workspace_before_approval -q
```

- [ ] **Step 3: Implement the minimal candidate lifecycle**

Create `.agentforge/candidates/<run-id>/` by copying the already captured canonical workspace once at run start. Execute mini actions only with this directory as cwd. Candidate paths are excluded from final patch capture and cannot be used as canonical source.

- [ ] **Step 4: Add final-patch publication test**

```python
async def test_approved_candidate_patch_is_applied_once_then_verified(tmp_path):
    run = await start_mini_linear_run(tmp_path)
    await submit_candidate_patch(run)
    approval = pending_final_patch_approval(run)
    await approve(approval)
    assert canonical_text(run, "src/module.py") == "x=2\n"
    assert final_diff(run).contains("x=2")
```

- [ ] **Step 5: Verify and commit**

```bash
uv run --frozen pytest tests/integration/test_mini_candidate_workspace.py tests/security/test_workspace_paths.py -q
git add src/agentforge/runtime/candidate_workspace.py src/agentforge/runtime/engine.py tests/integration/test_mini_candidate_workspace.py
git commit -m "feat(runtime): isolate mini repair candidate workspace"
```

### Task 3: Port the mini linear control flow, not its benchmark runner

**Files:**

- Create: `src/agentforge/repair_engines/mini_linear.py`
- Create: `src/agentforge/repair_engines/mini_prompt.py`
- Modify: `pyproject.toml`
- Create: `THIRD_PARTY_NOTICES.md`
- Test: `tests/unit/test_mini_linear_engine.py`
- Test: `tests/integration/test_mini_linear_engine.py`

- [ ] **Step 1: Write parser and turn-order RED tests**

```python
async def test_linear_engine_appends_command_output_before_next_model_turn() -> None:
    engine = MiniLinearRepairEngine(fake_model([bash("cat src/module.py"), submit()]))
    result = await engine.run(request)
    assert result.history[1]["role"] == "assistant"
    assert "x=1" in result.history[2]["content"]
```

```python
def test_linear_engine_rejects_command_outside_candidate_root() -> None:
    assert parse_action("```bash\ncd / && rm -rf x\n```").is_rejected
```

- [ ] **Step 2: Run RED**

```bash
uv run --frozen pytest tests/unit/test_mini_linear_engine.py -q
```

- [ ] **Step 3: Implement the small loop**

Use mini's public v2 behavior as the reference: one linear message list; model response containing one shell block; action executes in a fresh subshell against the candidate workspace; stdout/stderr becomes the next observation; explicit submit ends the loop. Do not import mini's SWE-bench runner or Docker environment.

Use the existing AgentForge provider and budget accounting on every model turn. Persist a compact checkpoint after every completed command containing the message history and candidate-root identity.

- [ ] **Step 4: Add MIT attribution**

`THIRD_PARTY_NOTICES.md` must identify mini-SWE-agent, its pinned revision, MIT license, and that AgentForge ports only the linear control-flow behavior. Do not copy benchmark prompts verbatim without retaining attribution and source reference.

- [ ] **Step 5: Verify and commit**

```bash
uv run --frozen pytest tests/unit/test_mini_linear_engine.py tests/integration/test_mini_linear_engine.py -q
uv run --frozen ruff check src/agentforge/repair_engines tests/unit/test_mini_linear_engine.py
git add src/agentforge/repair_engines pyproject.toml THIRD_PARTY_NOTICES.md tests/unit/test_mini_linear_engine.py tests/integration/test_mini_linear_engine.py
git commit -m "feat(repair): add mini linear repair adapter"
```

### Task 4: Connect submit to AgentForge approval and recovery

**Files:**

- Modify: `src/agentforge/application/app.py`
- Modify: `src/agentforge/runtime/engine.py`
- Modify: `src/agentforge/persistence/repair_workflow.py`
- Test: `tests/integration/test_mini_linear_resume.py`

- [ ] **Step 1: Write the approval-resume RED test**

```python
async def test_mini_linear_submit_pauses_then_resumes_after_final_patch_approval(tmp_path):
    run = await start_mini_linear_run_with_submit(tmp_path)
    assert details(run).lifecycle_status is LifecycleStatus.PAUSED
    await approve(pending_final_patch_approval(run))
    await resume(run)
    assert details(run).lifecycle_status is LifecycleStatus.TERMINAL
```

- [ ] **Step 2: Write the restart RED test**

```python
async def test_recreated_application_resumes_mini_linear_from_saved_history(tmp_path):
    run = await stop_after_first_command(tmp_path)
    reopened = recreate_application(tmp_path)
    await reopened.resume(run)
    assert model_call_count(run) == 2
    assert candidate_command_count(run) == 2
```

- [ ] **Step 3: Implement only final-patch approval and one-turn checkpoints**

On `submit`, capture candidate diff, invoke existing diff policy, request one final-patch approval, then use the existing mutation/test path to publish and verify. On restart, rebuild the linear loop from the last checkpoint; do not replay already completed commands.

- [ ] **Step 4: Verify and commit**

```bash
uv run --frozen pytest tests/integration/test_mini_linear_resume.py tests/integration/test_final_verification.py -q
git add src/agentforge/application/app.py src/agentforge/runtime/engine.py src/agentforge/persistence/repair_workflow.py tests/integration/test_mini_linear_resume.py
git commit -m "feat(repair): approve and resume mini linear runs"
```

### Task 5: Reproduce the baseline before adding one differentiator

**Files:**

- Create: `evaluation/protocols/verified10-mini-hybrid-smoke.json`
- Modify: `src/agentforge/evaluation/verified10_runner.py`
- Modify: `docs/evaluation/verified10-pass2-results.md`
- Test: `tests/integration/test_verified10_comparison_cli.py`

- [ ] **Step 1: Write the runner RED test**

```python
def test_hybrid_smoke_uses_mini_linear_engine_and_existing_official_export(fake_runner):
    result = run_hybrid_smoke(fake_runner)
    assert "repair_engine = \"mini_linear\"" in result.runtime_toml
    assert result.prediction_ids == FROZEN_SMOKE_IDS
```

- [ ] **Step 2: Run RED**

```bash
uv run --frozen pytest tests/integration/test_verified10_comparison_cli.py -q
```

- [ ] **Step 3: Run only the existing ten tasks as a reproduction smoke**

Success means hybrid submits at least eight patches and official harness resolves at least eight tasks. Failure means inspect the adapter once; do not add a second enhancement.

- [ ] **Step 4: Add one differentiator only after reproduction passes**

Use one public development test after mini submit. Feed a failed test summary back once, then require resubmission. Do not use SWE-bench hidden tests or issue-specific hints.

- [ ] **Step 5: Commit**

```bash
git add evaluation/protocols/verified10-mini-hybrid-smoke.json src/agentforge/evaluation/verified10_runner.py docs/evaluation/verified10-pass2-results.md tests/integration/test_verified10_comparison_cli.py
git commit -m "feat(evaluation): add mini hybrid reproduction smoke"
```

### Task 6: Confirm on a new holdout and report honestly

**Files:**

- Create: `evaluation/protocols/verified30-mini-hybrid-holdout.json`
- Create: `docs/evaluation/mini-hybrid-holdout-results.md`

- [ ] **Step 1: Freeze holdout before any model call**

Select 30 new Verified tasks by deterministic hash/order, excluding all ten Pass 2 tasks. Commit the IDs, task hashes, model, order, budgets, baseline command, and official harness commit.

- [ ] **Step 2: Run paired native mini and hybrid**

Use the same model, temperature, API-call/step cap, wall-time cap, image digest, and official harness. Alternate task order between arms to avoid provider-time bias.

- [ ] **Step 3: Run fault injection separately**

Kill five hybrid runs after deterministic turn counts. Record recovery success, repeated model calls, duplicate writes, and final patch digest. Do not claim a mini reliability deficit unless the same injection is run against native mini.

- [ ] **Step 4: Write only supported claims**

Report repair rate with paired task table, official report hashes, recovery outcomes, audit completeness, and overhead. Claim a repair-rate win only if hybrid exceeds mini on this unseen holdout; otherwise claim repair-rate parity only when results support it.

- [ ] **Step 5: Final verification and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest -q
uv run --frozen ruff check src tests
uv run --frozen mypy src
uv build
git add evaluation/protocols/verified30-mini-hybrid-holdout.json docs/evaluation/mini-hybrid-holdout-results.md
git commit -m "docs: report mini hybrid holdout evaluation"
```
