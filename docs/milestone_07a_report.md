# Milestone 7-A Report

## Decision

Milestone 7-A is **PASS** for constrained repair evaluation infrastructure.

This is not a PASS for the complete Milestone 7. M7-B task-set construction, pilot runs, protocol
freeze, repeated GPT-5.4 evaluation, and result publication have not started.

## Implemented

- Immutable RepairTaskPolicy with deterministic digest, fixed evaluation budgets, path precedence,
  allowed development profiles, and one non-model-selectable final profile.
- SQLite RepairState and idempotent budget facts keyed by Run, budget kind, and fact ID.
- RuntimeSnapshot v4 with migration from unversioned/v1/v2/v3 checkpoints.
- RepairCoordinator aggregation for mutation/test facts, source-test freshness, completion
  correction, full diff validation, and final-verification scheduling.
- Metadata-only WorkspaceBaseline and Git-independent WorkspaceDiffValidator.
- Protected-path, file-count, changed-byte, creation/deletion/type, sensitive-file, and suspicious
  test-bypass checks.
- Trusted hidden final verification through ApprovalWorkflow, AutoApproval, resume,
  TestExecutionCoordinator, and ProcessExecutionRecord.
- Hidden final stdout/stderr removal before model context, Events, and checkpoints.
- EvaluationTaskDefinition JSON loading and repeatable temporary fixture copies.
- Constrained AutoApproval bound to one temporary workspace, Run, policy digest, allowed tool/profile,
  remaining side-effect budget, and nonterminal RepairState.
- Persisted RepairEvaluationRun records, repetition/replacement fields, task metrics, prompt/schema
  digests, and deterministic JSON reports.
- MockModel synthetic E2E: read, approved exact edit, approved development test, full diff, hidden
  final verification, and VERIFIED_SUCCESS.

## Verification

- Python: 3.14.3.
- Full suite: 337 collected, 328 passed, 9 skipped, 0 failed.
- Ruff: all checks passed.
- strict mypy: 85 source files, no issues.
- compileall: passed.
- `git diff --check`: passed.

The nine skips are:

- one explicitly opt-in OpenAI live test;
- one POSIX process-group test on Windows;
- one POSIX executable-bit test on Windows;
- six real symlink tests blocked by Windows `WinError 1314`.

Real Windows Job Object process-tree tests and all M0-M6 regressions passed.

## Completion Semantics

FinalAnswer is not repair success. VERIFIED_SUCCESS requires:

1. an allowed successful development test;
2. that test to be newer than the latest committed mutation;
3. a compliant full workspace diff against the immutable baseline;
4. unchanged protected files;
5. a successful coordinator-selected hidden final profile;
6. no indeterminate side effect or terminal budget/policy state.

Default mode permits one objective completion correction. Strict mode permits none.

## Recovery

MutationExecutionRecord and ProcessExecutionRecord remain execution facts. Duplicate resume reuses
persisted terminal results and does not rerun mutation, development test, or final verification.
A consumed final-verification approval with terminal RepairState completes the Run without another
model call. WRITING mutation and STARTED test crash recovery become INDETERMINATE and are never
automatically retried. Budget facts are idempotent by durable fact ID.

## Security Boundaries

- No arbitrary shell, model-defined argv/cwd/environment, dependency installation, Git write,
  network tool, MCP, LangGraph, FastAPI, CLI, container, or OS sandbox was added.
- TestProfile and diff policy are allowlists, not process or network isolation.
- Hidden tests reduce direct overfitting but do not prove semantic correctness.
- SuspiciousChangeAnalyzer is heuristic and can miss or misclassify behavior.
- A registered test runs with the AgentForge account's OS and network permissions.
- Symlink protection is implemented, but six real link tests could not execute under this Windows
  account. Junction and broader reparse-point validation remains incomplete.

## Technical Debt

- Repair logical model-call accounting and ModelWorkflow physical attempt accounting are separate;
  exact cross-process reconciliation after a transmitted Provider request remains unresolved.
- Evaluation Mode serializes one Run. Distributed RepairState writers and leases are unsupported.
- Incremental post-mutation workspace scanning is not implemented; mutation preflight and final
  full-workspace validation are implemented.
- SQLite schema migration tooling is still absent.
- No formal tasks or real-model repair-quality measurements exist yet.
