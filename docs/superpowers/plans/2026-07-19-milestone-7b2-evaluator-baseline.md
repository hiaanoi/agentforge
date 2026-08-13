# Milestone 7-B2.2 Evaluator-Owned Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require every formal repair Pilot to persist and verify an evaluator-owned visible failing baseline before the first model request without consuming model-controlled budgets.

**Architecture:** Extract M6 process supervision into a shared managed execution core while preserving the approval coordinator's public behavior. Add a separate baseline domain model, SQLite workflow, coordinator, pytest fingerprint parser, protected context item, and EvaluationHarness gate. Formal fixture manifests bind exact normalized visible failed node IDs; uncertain or mismatched outcomes stop before model execution.

**Tech Stack:** Python 3.14, asyncio, Pydantic v2, SQLAlchemy 2, SQLite, pytest, Ruff, mypy strict.

---

### Task 1: Formal Baseline Contract and Parser

**Files:**
- Create: `src/agentforge/evaluation/baseline_models.py`
- Create: `src/agentforge/evaluation/baseline_parser.py`
- Modify: `src/agentforge/evaluation/task_definition.py`
- Test: `tests/unit/test_evaluation_baseline_parser.py`
- Test: `tests/unit/test_evaluation_task_definition.py`

- [ ] Write failing tests for strict `ExpectedBaselineFailure`, canonical fingerprint digests, Windows/POSIX node normalization, exact-set matching, unexpected pass, collection/import errors, and bounded redaction.
- [ ] Run `uv run --frozen pytest tests/unit/test_evaluation_baseline_parser.py tests/unit/test_evaluation_task_definition.py -q` and verify RED failures come from missing baseline types and manifest field.
- [ ] Implement `ExpectedBaselineFailure`, `BaselineFailureSummary`, execution status/reason enums, `BaselineExecutionRecord`, and `PytestBaselineResultParser`.
- [ ] Make `EvaluationTaskDefinition.expected_baseline_failure` required and reject hidden-suite node IDs, duplicate IDs, unsupported runner/match mode, and oversized input.
- [ ] Run the focused tests and verify GREEN.

### Task 2: Shared M6 Managed Execution Core

**Files:**
- Create: `src/agentforge/process/managed.py`
- Modify: `src/agentforge/runtime/test_execution.py`
- Test: `tests/unit/test_managed_test_execution_core.py`
- Test: `tests/integration/test_test_execution_runtime.py`
- Test: `tests/integration/test_test_execution_recovery.py`

- [ ] Write failing tests proving namespaced execution keys isolate active supervisors, duplicate keys cannot launch twice, cancellation delegates to the complete-tree supervisor, and outcomes are returned unchanged.
- [ ] Run the new unit test and verify RED because `ManagedTestExecutionCore` is absent.
- [ ] Implement immutable `ManagedExecutionKey`, `ManagedExecutionOrigin`, and `ManagedTestExecutionCore.execute/cancel` using the existing `asyncio.to_thread`, shield, cancellation, and active-lock behavior.
- [ ] Refactor `TestExecutionCoordinator.execute_approved/cancel_active` to delegate only process lifetime to the shared core while retaining binding validation, Approval CAS, Tool budget, persistence, and ToolResult construction.
- [ ] Run the focused core and M6 integration/recovery tests and verify GREEN with unchanged M6 event and budget behavior.

### Task 3: Baseline SQLite Fact Store and CAS Workflow

**Files:**
- Modify: `src/agentforge/persistence/tables.py`
- Create: `src/agentforge/evaluation/baseline_persistence.py`
- Test: `tests/unit/test_evaluation_baseline_persistence.py`

- [ ] Write failing repository/workflow tests for idempotent creation, identity conflicts, per-Run uniqueness, Run isolation, single CAS claim, terminal CAS, terminal immutability, and persisted round trips.
- [ ] Run the focused test and verify RED because the table and repository are absent.
- [ ] Add `EvaluationBaselineExecutionRow` with the approved bindings, process facts, digests, safe summary JSON, versions, and timestamps; do not add complete output or environment JSON columns.
- [ ] Implement `BaselineExecutionRepository` and `BaselineExecutionWorkflow` with atomic `CREATED -> STARTED` and `STARTED -> terminal` updates.
- [ ] Append only sanitized baseline audit payloads through the existing event table in the same transaction as state changes.
- [ ] Run the focused persistence tests and verify GREEN.

### Task 4: Evaluator-Owned Baseline Coordinator

**Files:**
- Create: `src/agentforge/evaluation/baseline.py`
- Modify: `src/agentforge/tools/testing/profiles.py`
- Test: `tests/integration/test_evaluation_baseline_coordinator.py`
- Test: `tests/integration/test_evaluation_baseline_recovery.py`

- [ ] Write failing tests for expected failure verification, unexpected pass, mismatch, timeout, launch failure, profile binding drift, concurrent execute, cancellation, restart from verified, and legacy STARTED recovery.
- [ ] Run the focused tests and verify RED because the coordinator is absent.
- [ ] Generalize trusted profile rebinding so both `TestApprovalBinding` and baseline execution plans validate version, digest, executable, argv, cwd, environment, and enabled state.
- [ ] Implement `BaselineExecutionCoordinator.ensure_created/execute/recover/get/cancel/context_item` using the shared core and parser.
- [ ] Map conclusive invalid outcomes to `BLOCKED`, uncertain outcomes to `INDETERMINATE`, and never automatically retry a STARTED record after reconstruction.
- [ ] Run focused coordinator and recovery tests and verify GREEN.

### Task 5: Protected Initial Context and Pre-Model Gate

**Files:**
- Modify: `src/agentforge/context/models.py`
- Modify: `src/agentforge/context/builder.py`
- Modify: `src/agentforge/runtime/engine.py`
- Modify: `src/agentforge/evaluation/harness.py`
- Modify: `src/agentforge/evaluation/models.py`
- Modify: `src/agentforge/evaluation/persistence.py`
- Test: `tests/integration/test_evaluator_owned_baseline_runtime.py`
- Test: `tests/unit/test_context_builder.py`

- [ ] Write failing tests proving the baseline item is protected, appears in the first request, survives reconstruction, and is absent when baseline is blocked.
- [ ] Write failing tests proving baseline execution leaves `test_runs_used`, `tool_call_count`, model-call count, and token usage at zero before Runtime starts.
- [ ] Add `EVALUATION_BASELINE_FAILURE` and allow `AgentRuntime.execute` to receive host-supplied prevalidated initial context without exposing a Tool.
- [ ] Gate `EvaluationHarness.execute` on a bound `BaselineExecutionCoordinator`; emit `EVALUATION_RUN_STARTED` before baseline, call Runtime only after verified binding checks, and include `baseline_execution_id` in the immutable evaluation result.
- [ ] Add an evaluator workflow terminal path that records pre-model setup/infrastructure failure without consuming a model budget.
- [ ] Run focused context and Runtime integration tests and verify GREEN.

### Task 6: Fresh Formal Fixture Pilot Preparation

**Files:**
- Create: `src/agentforge/evaluation/formal_fixtures.py`
- Modify: `src/agentforge/evaluation/workspace.py`
- Test: `tests/evaluation/test_formal_fixture_pilot.py`

- [ ] Write failing tests that load the B2.1 manifest, copy only the original buggy Fixture to a unique temporary workspace, bind visible/hidden profiles from trusted configuration, and reject symlink/reparse, modified immutable assets, or digest mismatch.
- [ ] Run the focused test and verify RED because formal Pilot preparation is absent.
- [ ] Implement strict formal manifest loading and a `FormalFixturePilotWorkspace` context manager that creates a fresh workspace and exposes trusted profile definitions without exposing hidden profile details to model context.
- [ ] Ensure hidden execution has no baseline-stage method and each Pilot has distinct root and baseline IDs.
- [ ] Run the focused tests and verify GREEN.

### Task 7: Bind Exact Failure Fingerprints to B2.1 Fixtures

**Files:**
- Modify: `evaluation/fixtures/fixture_schema.json`
- Modify: `evaluation/fixtures/tasks/bugsinpy-black-21/task_manifest.json`
- Modify: `evaluation/fixtures/tasks/quixbugs-shortest-path-length/task_manifest.json`
- Modify: `evaluation/fixtures/tasks/self-durable-double-consumption/task_manifest.json`
- Modify: `evaluation/fixtures/tasks/swebench-pytest-10051/task_manifest.json`
- Modify: `evaluation/fixtures/verify_fixtures.py`
- Test: `tests/evaluation/test_formal_fixture_assets.py`

- [ ] Extend fixture asset tests to require exact visible failed node IDs and reject hidden IDs or inconsistent verification evidence.
- [ ] Run the focused fixture tests and verify RED against the old schema.
- [ ] Add each previously verified visible failure node ID to its manifest and update schema validation.
- [ ] Update fixture verification to derive and compare the exact normalized failed-node set on every buggy visible run.
- [ ] Regenerate only deterministic fixture verification metadata if the verifier requires it; do not alter source, tests, or reference fixes.
- [ ] Run the fixture tests and three-repeat verifier and verify all buggy/reference expectations remain deterministic.

### Task 8: Crash Windows, Cancellation, Hidden Isolation, and Regression

**Files:**
- Test: `tests/integration/test_evaluator_owned_baseline_recovery.py`
- Test: `tests/process/test_evaluation_baseline_process_tree.py`
- Modify: `docs/repair_evaluation_protocol.md`
- Create: `docs/milestone_07b2_pilot_report.md`

- [ ] Add tests for crash after verified persistence/before model, crash after STARTED/before result persistence, cancellation/result races, no residual process tree, no hidden startup execution, and post-model development-test budget consumption.
- [ ] Verify audit serialization contains no complete output, absolute temporary path, environment value, credential marker, Fixture content, or hidden test detail.
- [ ] Run all B2.2 tests and the M6/M7 integration suites; fix only defects within the approved design.
- [ ] Update the evaluation protocol and milestone report with implemented behavior, test evidence, platform skips, and remaining limitations without claiming formal model results.
- [ ] Run final verification:

```text
uv run --frozen pytest -ra
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen python -m compileall src
git diff --check
```

- [ ] Review the final diff for secrets, hidden-fixture leakage, dependency changes, and scope creep; do not enter Milestone 8 or publish model scores.
