# Milestone 6 TDD Plan: Verified Test Execution Runtime

> Production behavior is added only after its focused test has failed for the expected missing
> capability. This milestone creates no Git commit.

## File map

New production modules:

- `src/agentforge/domain/test_execution.py`: profile, plan, binding, record, and TestResult models.
- `src/agentforge/tools/testing/profiles.py`: trusted registration and binding validation.
- `src/agentforge/tools/testing/run_tests.py`: profile_id-only approval tool.
- `src/agentforge/process/base.py`: platform-neutral supervisor contracts and outcomes.
- `src/agentforge/process/streaming.py`: bounded stream hashing, capture, and redaction.
- `src/agentforge/process/posix.py`: process-group supervisor.
- `src/agentforge/process/windows_job.py`: real Windows Job Object supervisor and launcher.
- `src/agentforge/process/runner.py`: platform selection and active execution handle.
- `src/agentforge/persistence/test_executions.py`: bindings and execution repositories.
- `src/agentforge/persistence/test_execution_workflow.py`: attempts, CAS claims, and terminal facts.
- `src/agentforge/runtime/test_execution.py`: coordinator, recovery, cancellation, and ToolResult.

Modified modules:

- `domain/enums.py`, `domain/models.py`, `domain/errors.py`
- `persistence/tables.py`, `persistence/approval_workflow.py`
- `policy/engine.py`, `tools/executor.py`
- `runtime/snapshots.py`, `runtime/engine.py`

## TDD sequence

### 1. TestProfile domain and digest

- Add `tests/unit/test_test_profile_domain.py` covering immutability, positive versions, absolute
  executable/cwd, argv identity, deterministic digests, record versions, failure-kind validation,
  and forbidden raw-output fields.
- Run the file and verify import/enum failures.
- Add the minimal enums and domain models; rerun the file, Ruff, and mypy.

### 2. Trusted registry and security

- Add `tests/security/test_test_profile_security.py` covering trusted-only registration API,
  duplicate IDs, missing/disabled profiles, executable one-time resolution, missing executables,
  workspace cwd containment, symlink/reparse cwd rejection, argv NUL/empty rejection, fixed env,
  sensitive environment names, no host inheritance, and all six resume binding comparisons.
- Verify RED before implementing `TestProfileRegistry` and TestExecutionPlan creation.
- Run focused tests, Ruff, and mypy.

### 3. Persistence and attempts

- Add `tests/unit/test_process_execution_persistence.py` for binding/record round trips, unique
  approval, unique `(run_id, attempt_number)`, tool_call_digest linkage, terminal metadata,
  record_version, restart durability, ordering, and Run isolation.
- Verify missing tables/repositories fail, then implement rows and repositories.
- Run focused tests, Ruff, and mypy.

### 4. Bounded streaming

- Add `tests/unit/test_bounded_process_streaming.py` using chunked BytesIO sources to prove output
  is consumed incrementally, retained bytes never exceed the per-stream limit, complete digests and
  sizes include discarded bytes, invalid UTF-8/control text is normalized, and token/private-key
  samples are redacted before a result is constructed.
- Implement streaming capture without `communicate()` or unbounded reads.
- Run focused tests, Ruff, and mypy.

### 5. POSIX process-group supervisor

- Add platform-gated `tests/process/test_posix_process_supervisor.py` with real Python parent/child
  fixtures. Verify fixed argv/cwd/env, shell=false behavior, normal/nonzero exit, timeout TERM/KILL,
  cancellation, child non-escape, no residual group, and uncertain termination mapping.
- Implement only the POSIX backend after RED. Run it on POSIX when available; on Windows verify the
  platform skip and test platform-neutral contracts separately.

### 6. Windows Job Object supervisor

- Add Windows-only `tests/process/test_windows_job_supervisor.py` using real Win32 APIs and a real
  Python launcher/child fixture. Verify launcher waits until Job assignment, no breakaway, root PID
  reporting, timeout/cancel whole-Job termination, zero active processes, KILL_ON_JOB_CLOSE, and no
  background process residue. Non-Windows must skip; no mocked Windows API.
- Implement ctypes-based Job Object ownership and controlled launcher, then run focused tests,
  Ruff, and mypy on Windows.

### 7. run_tests and Policy

- Add `tests/unit/test_run_tests_tool.py` and extend `tests/unit/test_policy.py` for a schema that
  accepts only profile_id, rejects extra command/argv/cwd/env fields, returns TestApprovalRequired,
  identifies LOCAL/DANGEROUS/TEST_PROFILE_EXECUTION/requires_approval, and fails closed through
  ordinary execute.
- Prove every other DANGEROUS capability remains denied before adding ToolCapability and managed
  policy handling.
- Run focused tests, Ruff, and mypy.

### 8. Workflow and cancellation CAS

- Add `tests/unit/test_test_execution_workflow.py` for idempotent CREATED allocation, monotonic
  per-Run attempt numbers, no budget use before STARTED, atomic Approval/Run/Execution claim,
  STARTED budget increment, record_version CAS, all terminal facts, cancellation before start,
  natural-completion-versus-cancel winner semantics, and INDETERMINATE fail-closed behavior.
- Implement TestExecutionWorkflow and extend ApprovalWorkflow cancellation atomically.
- Run focused tests, Ruff, and mypy.

### 9. Coordinator and three crash windows

- Add `tests/integration/test_test_execution_recovery.py` for approved CREATED restart execution,
  stale STARTED restart to INDETERMINATE, terminal-record restart result reuse, consumed checkpoint
  continuation, missing/corrupt records, profile drift across every bound field, repeated resume,
  and external coroutine cancellation with no child process left.
- Implement TestExecutionCoordinator and managed ToolExecutor execution after RED.
- Run focused M3/M5/M6 recovery regressions, Ruff, and mypy.

### 10. RuntimeSnapshot v3

- Add `tests/unit/test_snapshot_v3.py` for exact v3 parsing, v2/unversioned migration, pending test
  identity, bounded TestResult persistence, forbidden argv/env/raw-output fields, corrupt identity,
  and unknown future version rejection.
- Implement v3 and update Runtime checkpoint construction/read paths.
- Run snapshot and approval recovery suites, Ruff, and mypy.

### 11. Full Runtime loop

- Add `tests/integration/test_test_execution_runtime.py` with MockModelProvider flows for
  edit_file approval/commit, run_tests approval/execution, passing and nonzero results, model
  continuation, FinalAnswer, timeout result, rejection, cancellation, repeated test attempts,
  Tool Budget timing, multiple Runs/Approvals/Executions, and absence of output/secrets from events
  and checkpoints.
- Wire AgentRuntime APIs and result context only after the integration tests fail correctly.
- Run all M6 integration tests and focused M0-M5 regression groups.

### 12. Documentation and acceptance

- Add `docs/milestone_06_report.md` with actual evidence only.
- Update README, architecture, security model, and implementation plan without claiming shell,
  sandbox, automatic repair, dependency installation, or network isolation.
- Run the complete acceptance commands:

```powershell
uv run --frozen pytest -ra
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen python -m compileall src
git diff --check
```

- Confirm M0-M5 regression, unchanged unrelated dependencies and uv.lock, no secret-bearing files
  in Git status, no M6 commit, and no Milestone 7 work.
