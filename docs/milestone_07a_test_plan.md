# Milestone 7-A RED-GREEN Test Plan

## Method

Every production behavior starts with a focused failing test. The test must fail because the
behavior is absent, not because of setup or syntax. Minimal production code is then added, the
focused test is rerun, and related M0-M6 tests are executed before the next slice. No broad
exception swallowing or relaxation of existing security rules is allowed.

## M7A.1 Repair policy and fixed budgets

- RED: immutable policy, canonical path normalization, deterministic digest, invalid policy,
  forbidden/protected precedence, and exact BASIC/ENGINEERING/CHALLENGE limits.
- GREEN: `domain/repair.py` policy and budget models plus stable digest helpers.
- Verify: `tests/unit/test_repair_task_policy.py`.

## M7A.2 Repair persistence and CAS budgets

- RED: policy/state creation, digest binding, every counter timing, stale state_version rejection,
  terminal precedence, duplicate fact reconciliation, and limit exhaustion.
- GREEN: repair tables, repositories, and RepairWorkflow conditional updates.
- Verify: `tests/unit/test_repair_budget.py` and persistence-focused tests.

## M7A.3 RuntimeSnapshot V4

- RED: unversioned/v1/v2/v3 migration, V4 identity validation, unknown version failure, and absence
  of source, prompt, environment, hidden test, and complete output fields.
- GREEN: RuntimeSnapshotV4 and strict loader migration.
- Verify: `tests/unit/test_snapshot_v4.py` plus v2/v3 regression tests.

## M7A.4 RepairCoordinator and completion correction

- RED: mutation/test chronological reconciliation, latest_source_verified, FinalAnswer rejection,
  strict zero-correction behavior, one default correction, objective feedback, correction crash
  idempotency, and terminal state non-overwrite.
- GREEN: lifecycle dispositions, RepairCoordinator, RepairWorkflow correction fact, and protected
  context item.
- Verify: `tests/unit/test_repair_coordinator.py` and
  `tests/unit/test_completion_correction.py`.

## M7A.5 Workspace baseline

- RED: full manifest, deterministic root digest, normalization, binary/text classification,
  executable bit, path escape, symlink/reparse failure, and sensitive audit safety.
- GREEN: baseline scanner and metadata persistence without source content.
- Verify: `tests/unit/test_workspace_baseline.py` and platform-gated security cases.

## M7A.6 Workspace diff and suspicious analysis

- RED: modified/created/deleted/rename-like/type changes, protected hashes, path precedence,
  creation policy, file/byte limits, external changes, sensitive files, test infrastructure, and
  suspicious test-bypass patterns with benign boundaries.
- GREEN: independent WorkspaceDiffValidator and SuspiciousChangeAnalyzer.
- Verify: `tests/unit/test_workspace_diff_validator.py` and
  `tests/unit/test_suspicious_change_analyzer.py`.

## M7A.7 Runtime policy integration

- RED: forbidden edit and disallowed profile produce no ApprovalRequest; final profile is not
  model-selectable; creation rules and exhausted budgets fail before side effects; policy mismatch
  after restart fails closed.
- GREEN: optional Repair preflight seam in ToolExecutor/AgentRuntime.
- Verify: `tests/integration/test_repair_policy_integration.py` plus M2/M5/M6 regressions.

## M7A.8 Trusted final verification

- RED: only Coordinator can request final profile, complete M6 approval/binding/process chain is
  used, hidden output is not returned to model/Event/checkpoint, success/failure mapping is stable,
  and duplicate resume does not rerun.
- GREEN: trusted final-verification disposition and bounded result summary.
- Verify: `tests/integration/test_final_verification.py`.

## M7A.9 AutoApprovalHarness

- RED: arbitrary workspace denied, evaluation marker required, policy/binding mismatch denied,
  exhausted/indeterminate state denied, no bypass of ApprovalWorkflow, and audited decisions.
- GREEN: constrained AutoApprovalHarness in the evaluation package.
- Verify: `tests/unit/test_auto_approval_harness.py`.

## M7A.10 Evaluation models, metrics, and prompts

- RED: task schema validation, immutable RepairEvaluationRun, repetition/replacement relation,
  result schema version, success aggregates, mean/median rates, failure distribution,
  infrastructure separation, deterministic prompt/schema digests, and hidden metadata exclusion.
- GREEN: evaluation task/models/metrics/prompts/report modules.
- Verify: `tests/unit/test_repair_evaluation_models.py`,
  `tests/unit/test_repair_metrics.py`, and prompt safety tests.

## M7A.11 Scripted synthetic end-to-end

- RED: read -> edit -> approval -> development test -> approval -> FinalAnswer -> full diff ->
  final verification -> verified success. Focused tests separately cover premature FinalAnswer
  correction, diff violations, final hidden failure, budget exhaustion, and policy block.
- GREEN: EvaluationHarness construction, temporary fixture copy, profile registration, Runtime
  driving, AutoApproval, result persistence, and reporting.
- Verify: `tests/integration/test_repair_evaluation_e2e.py` plus
  `tests/integration/test_repair_runtime_integration.py`.

## M7A.12 Crash recovery

- RED: duplicate consumed resume, committed mutation/test result reuse, final-verification reuse,
  correction idempotency, policy digest mismatch, and mutation/test indeterminate fail-closed.
- GREEN: reconciliation from existing M3/M5/M6 execution facts and V4 snapshot summaries.
- Verify: `tests/integration/test_final_verification.py`,
  `tests/integration/test_mutation_runtime.py`,
  `tests/integration/test_test_execution_recovery.py`, and Repair coordinator tests.

## M7A.13 Documentation and complete regression

- Update README, architecture, security model, implementation plan, protocol, and M7-A report.
- Assert project contract and documentation do not claim M7-B, formal task-set, GPT-5.4 results,
  OS sandbox, network isolation, or interactive budget extension.
- Run:

```text
uv run --frozen pytest -ra
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen python -m compileall src
git diff --check
```

## Platform policy

Windows symlink/reparse tests may skip only when the OS denies link creation and must report the
reason. POSIX process-group tests remain platform-gated. Tests do not fake unreliable OS security
semantics. M7-A tests are offline and use temporary fixture workspaces only.
