# AgentForge SWE Budget Ablation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the two real AgentForge runtime blockers observed in the official Verified-10 run and execute a bounded three-task, 100-model-call ablation with official SWE-bench scoring.

**Architecture:** The runtime owns terminal consistency: every terminal `Run` has a matching terminal `RepairState`. The loop detector retains protection against repeated writes and failed tools, but successful repeated reads get a bounded non-fatal path. The existing Verified-10 runner gains an explicit subset mode that preserves normal workspace provenance while exporting only the declared ablation predictions.

**Tech Stack:** Python 3.14, Pydantic, SQLAlchemy, pytest, uv, Docker, SWE-bench harness.

---

### Task 1: Keep terminal run and repair state consistent

**Files:**
- Modify: `src/agentforge/runtime/engine.py:1710-1768`
- Modify: `src/agentforge/persistence/repair_workflow.py:665-726`
- Test: `tests/integration/test_product_runtime_terminal_state.py`

- [ ] **Step 1: Write the failing product-runtime regression**

Create a run with a repair state, cause the engine's generic loop failure path,
then assert `RunDetails` and `ExportRunDetails` both return a terminal view:

```python
def test_generic_runtime_failure_terminalizes_repair_state_for_inspection(...):
    run_id = start_running_product_run(...)
    force_repeated_successful_action_loop(...)
    details = application.query(RunDetails(run_id=run_id))
    exported = application.query(ExportRunDetails(run_id=run_id))
    assert details.lifecycle_status is LifecycleStatus.TERMINAL
    assert details.outcome_status is OutcomeStatus.FAILED
    assert exported.run_id == run_id
```

- [ ] **Step 2: Prove the regression fails**

Run:

```bash
uv run --frozen pytest tests/integration/test_product_runtime_terminal_state.py::test_generic_runtime_failure_terminalizes_repair_state_for_inspection -q
```

Expected: `ProductProjectionError` caused by `runs.status=FAILED` with a
`repair_states.status=RUNNING` row.

- [ ] **Step 3: Add one idempotent generic terminalization path**

Add a `RepairWorkflow.terminalize_runtime_failure(...)` method which loads the
running state under the existing lease, transitions it with
`RepairCompletionStatus.RUNTIME_FAILURE` and
`RepairTerminationReason.RUNTIME_FAILURE`, and returns unchanged if already
terminal. Update `RuntimeEngine._fail` to call this method before persisting
the `RunStatus.FAILED` event when a repair workflow exists. Do not call the
method from `_finish_repair_run`, because that method already receives a
terminal repair state.

```python
if self._repairs is not None:
    self._repairs.terminalize_runtime_failure(
        run.run_id,
        authority=self._authority(ownership, run.run_id),
    )
```

- [ ] **Step 4: Verify the regression and existing failure paths**

Run:

```bash
uv run --frozen pytest tests/integration/test_product_runtime_terminal_state.py tests/unit/test_evaluation_outcome_classification.py -q
```

Expected: all pass; the persisted repair status is terminal before projection.

- [ ] **Step 5: Commit**

```bash
git add src/agentforge/runtime/engine.py src/agentforge/persistence/repair_workflow.py tests/integration/test_product_runtime_terminal_state.py
git commit -m "fix(runtime): terminalize repair state on generic failures"
```

### Task 2: Distinguish benign repeated reads from real tool loops

**Files:**
- Modify: `src/agentforge/runtime/engine.py:1290-1385`
- Modify: `src/agentforge/runtime/loop_detection.py`
- Test: `tests/unit/test_loop_detection.py`
- Test: `tests/integration/test_runtime_loop_recovery.py`

- [ ] **Step 1: Write failing loop-policy tests**

Cover three exact cases:

```python
def test_three_successful_identical_read_file_actions_are_not_terminal(): ...
def test_repeated_successful_edit_file_actions_remain_terminal(): ...
def test_repeated_failed_read_file_actions_remain_terminal(): ...
```

The first test uses the exact same successful `read_file` action and result
digest three times. The latter two use identical `edit_file` success and
`read_file` failure observations respectively.

- [ ] **Step 2: Prove the first test fails under current behavior**

Run:

```bash
uv run --frozen pytest tests/unit/test_loop_detection.py -q
```

Expected: successful repeated `read_file` reaches `LOOP_DETECTED` at repeat
count three.

- [ ] **Step 3: Add a read-only repetition classification**

Pass tool name and tool success into the detector. For successful
`read_file`, `list_files`, `search_text`, and `get_git_diff`, retain the
warning event and normal budget accounting but make the third repeat
non-terminal. Apply a dedicated cap of eight identical successful read-only
observations; the ninth repeat becomes terminal. Keep the current terminal
behavior for mutations and any failed tool outcome.

```python
benign_read = success and tool_name in _READ_ONLY_LOOP_TOOLS
terminal = repeat_count >= (9 if benign_read else 3)
```

- [ ] **Step 4: Add the product integration regression**

Drive three identical successful reads through `RuntimeEngine`, then inspect
the run. Assert that the run remains `RUNNING`, emits `LOOP_WARNING`, and
does not emit `LOOP_DETECTED`; drive repeated edit and failed-read controls
and assert they terminalize as `FAILED`.

- [ ] **Step 5: Verify and commit**

Run:

```bash
uv run --frozen pytest tests/unit/test_loop_detection.py tests/integration/test_runtime_loop_recovery.py -q
```

Then:

```bash
git add src/agentforge/runtime/engine.py src/agentforge/runtime/loop_detection.py tests/unit/test_loop_detection.py tests/integration/test_runtime_loop_recovery.py
git commit -m "fix(runtime): tolerate bounded repeated reads"
```

### Task 3: Add the immutable 100-call ablation budget

**Files:**
- Modify: `src/agentforge/domain/repair.py:14-18,132-172`
- Test: `tests/unit/test_repair_task_policy.py`
- Test: `tests/integration/test_atomic_run_creation.py`

- [ ] **Step 1: Write failing profile-matrix tests**

Add `BudgetProfile.SWE_BENCH_ABLATION_100` to the expected fixed profile
matrix with:

```python
(100, 160, 16, 16, 4, 6, 3600)
```

Assert every individual budget override remains rejected and that reopening a
persisted policy preserves the profile and all seven fixed limits.

- [ ] **Step 2: Prove the tests fail**

Run:

```bash
uv run --frozen pytest tests/unit/test_repair_task_policy.py tests/integration/test_atomic_run_creation.py -q
```

Expected: missing enum member/fixed budget binding.

- [ ] **Step 3: Implement the enum and fixed budget**

Add only the new enum member and its immutable `_FIXED_BUDGETS` entry. Do not
allow per-run overrides.

- [ ] **Step 4: Verify and commit**

Run the command from Step 2 and require all tests to pass, then:

```bash
git add src/agentforge/domain/repair.py tests/unit/test_repair_task_policy.py tests/integration/test_atomic_run_creation.py
git commit -m "feat(repair): add SWE ablation budget profile"
```

### Task 4: Add a narrow three-task ablation runner

**Files:**
- Create: `src/agentforge/evaluation/swe_ablation.py`
- Create: `evaluation/run_swe_ablation.py`
- Modify: `src/agentforge/evaluation/verified10_runner.py`
- Test: `tests/unit/test_swe_ablation.py`
- Test: `tests/integration/test_swe_ablation_cli.py`

- [ ] **Step 1: Write the failing ablation contract tests**

Define the immutable declared set:

```python
SWE_ABLATION_INSTANCE_IDS = (
    "django__django-12419",
    "django__django-13343",
    "matplotlib__matplotlib-24026",
)
```

Test that the runner rejects any additional ID, duplicate ID, protocol hash
mismatch, existing output root, and any profile other than
`SWE_BENCH_ABLATION_100`. Test that the generated runtime TOML names the new
profile and exposes 100 model calls.

- [ ] **Step 2: Prove the contract fails**

Run:

```bash
uv run --frozen pytest tests/unit/test_swe_ablation.py tests/integration/test_swe_ablation_cli.py -q
```

Expected: missing module/CLI command.

- [ ] **Step 3: Implement a wrapper, not a second evaluator**

`SWEAblationCampaign` wraps the existing provenance, workspace preparation,
AgentForge execution, attempt ledger, and prediction exporter. It filters only
after `validate_public_task` has validated the frozen Verified-10 data. It
uses a new output root and writes an explicit three-task manifest containing
the source protocol SHA, selected IDs, profile, model identity, and Docker
digests. `evaluation/run_swe_ablation.py` exposes `prepare`, `run-agentforge`,
`finalize-predictions`, and `status` subcommands.

- [ ] **Step 4: Verify CLI behavior**

Run:

```bash
uv run --frozen pytest tests/unit/test_swe_ablation.py tests/integration/test_swe_ablation_cli.py tests/unit/test_verified10_campaign.py -q
```

Expected: the new runner writes only three ledger rows and a standard
SWE-bench prediction file.

- [ ] **Step 5: Commit**

```bash
git add src/agentforge/evaluation/swe_ablation.py evaluation/run_swe_ablation.py src/agentforge/evaluation/verified10_runner.py tests/unit/test_swe_ablation.py tests/integration/test_swe_ablation_cli.py
git commit -m "feat(evaluation): add targeted SWE budget ablation"
```

### Task 5: Verify locally, deploy, and score the ablation

**Files:**
- Modify: `README.md`
- Modify: `docs/evaluation/verified10-pass1-reporting.md`

- [ ] **Step 1: Run local verification**

```bash
uv run --frozen ruff check src tests
uv run --frozen mypy src
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest -q
uv build
```

- [ ] **Step 2: Publish the exact source revision to the existing Tencent VM**

Archive the committed worktree revision, upload it to a new server source
directory, and run `prepare` with the pinned harness, cached dataset Python,
and a never-before-used ablation output root. Confirm the manifest has exactly
the three declared IDs and `SWE_BENCH_ABLATION_100`.

- [ ] **Step 3: Run the live three-task experiment**

In a detached tmux session, prompt for the DeepSeek key without echoing it,
run only the AgentForge ablation, finalize predictions, and unset both model
key environment variables on exit. Do not shut down the instance.

- [ ] **Step 4: Run official scoring and apply the decision rule**

Use the pinned SWE-bench source with the existing working harness Python:

```bash
PYTHONPATH="$HARNESS" "$HARNESS_PYTHON" -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Verified --split test \
  --predictions_path "$OUTPUT/agentforge-predictions.json" \
  --max_workers 2 --timeout 3600 --run_id swe-ablation-100 \
  --report_dir "$OUTPUT/official-reports"
```

Record submitted, empty, completed, and resolved IDs. Continue only at two or
more resolved of the declared three tasks; otherwise write the migration
decision and do not begin another native-agent budget increase.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md docs/evaluation/verified10-pass1-reporting.md
git commit -m "docs: document SWE budget ablation protocol"
```
