# Milestone 7-B2.2 Evaluator-Owned Baseline Report

## Decision

**PASS for the evaluator-owned baseline gate.** AgentForge now proves that every configured formal
Pilot begins from a fresh buggy workspace with the expected visible failure before the first model
request. This report does not claim a real-model repair result or benchmark score.

## Implemented

- Exact, versioned pytest failed-node fingerprints in all four formal Fixture manifests.
- Three-repeat Fixture verification that rejects unexpected visible failure sets.
- A shared M6/B2.2 managed process execution core with full process-tree cancellation.
- Independent `evaluation_baseline_executions` SQLite facts with per-Run uniqueness, CAS claim,
  immutable terminal results, profile binding, process metadata, and safe-summary digests.
- Restart behavior that reuses verified facts and marks inherited STARTED execution
  INDETERMINATE without automatic replay.
- Evaluator-owned execution before AgentRuntime with zero development-test, Tool, model-call, and
  token budget consumption.
- Protected `EVALUATION_BASELINE_FAILURE` initial context containing bounded, redacted visible
  failure evidence.
- Fail-closed termination before Provider access for unexpected pass, mismatch, timeout, launch
  failure, profile drift, cancellation, or uncertain termination.
- Fresh formal Pilot workspace preparation that omits reference fixes and hidden tests from the
  model workspace. Hidden tests remain in an evaluator-owned external directory for final-only
  verification.

## Verification

- Formal Fixture verifier: 4 tasks x buggy/reference x visible/hidden x 3 repetitions = 48
  executions.
- Expected results: 24 buggy failures and 24 reference passes.
- Visible buggy failure fingerprints: 12 exact matches across three repetitions.
- Timeouts and unexpected outcomes: 0.
- Repository suite: 414 collected, 405 passed, 9 skipped, 0 failed.
- Skips: one opt-in OpenAI Live test, one POSIX process-group test, one POSIX executable-bit test,
  and six symlink tests unavailable under Windows `WinError 1314`.
- Ruff: all checks passed.
- mypy strict: 91 source files, no issues.
- compileall and `git diff --check`: passed.

## Safety Boundaries

- Baseline is not a Tool, ApprovalRequest, or model action.
- Only administrator-registered TestProfiles are accepted; executable, argv, cwd, environment,
  Profile version, and Profile digest are rebound before execution.
- Complete stdout/stderr and environment mappings are not written to baseline Events or records.
- A temporary directory is isolation hygiene, not an OS sandbox.
- Windows Job Object behavior is exercised on Windows; POSIX process groups remain platform-gated.
- SQLite CAS guarantees target the supported single-process architecture, not distributed workers.

## Deferred

- Real-model Pilot runs and repetition policy.
- Frozen model parameters and final prompt/schema digests for published evaluation.
- Aggregate scoring, replacement-run decisions, and benchmark publication.
- OS-level sandboxing, containers, distributed leases, and hostile-Fixture containment.
