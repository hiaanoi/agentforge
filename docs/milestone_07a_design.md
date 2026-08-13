# Milestone 7-A Design: Constrained Repair Evaluation Infrastructure

## Status and scope

This document defines Milestone 7-A only. M7-A builds Evaluation Mode infrastructure on top of
the existing durable Runtime, approval/resume, workspace mutation, and managed test execution.
It does not create the formal benchmark task set, run a GPT-5.4 evaluation, implement interactive
budget extension, or complete Milestone 7.

M7-A adds no arbitrary shell, dependency installation, Git write operation, network tool,
container, OS sandbox, MCP, LangGraph, multi-agent behavior, FastAPI, or CLI.

## Existing architecture audit

AgentRuntime is the only model loop and owns context, checkpoints, approval continuation, and Run
control flow. MutationExecutionRecord and ProcessExecutionRecord are the durable facts for
side effects. ModelAttempt rows are the durable facts for physical Provider requests. M7-A must
aggregate these facts instead of duplicating their execution or recovery logic.

Three existing behaviors require explicit integration:

1. AgentRuntime currently accepts FinalAnswer as immediate Run completion. Repair mode must
   intercept it before the terminal transition.
2. PolicyEngine has global Tool policy but no immutable per-Run repair policy. Repair preflight
   must run after argument validation and before an ApprovalRequest can be created.
3. Run.tool_call_count is a generic Tool Budget and cannot represent separate read, edit, test,
   correction, and policy-violation limits.

## Architecture decision

AgentRuntime gains one optional RepairLifecycle interface. When absent, M0-M6 behavior is
unchanged. When present, Runtime calls the lifecycle at deterministic points:

- before a physical model request;
- after Tool argument validation but before ordinary policy can request approval;
- after a Tool result or durable side-effect result is available;
- when a FinalAnswer arrives;
- before and after trusted final verification;
- during resume reconciliation and cancellation.

RepairCoordinator implements this interface. It does not call ModelProvider, execute mutations,
launch processes, manage process trees, or create a second agent loop. WorkspaceDiffValidator is
an independent module used by the coordinator. EvaluationHarness constructs an isolated Runtime
stack and drives existing approval and resume methods.

```text
EvaluationHarness
    |-- RepairCoordinator
    |     |-- RepairWorkflow / repositories
    |     |-- WorkspaceBaselineBuilder
    |     `-- WorkspaceDiffValidator
    `-- AgentRuntime
          |-- ModelExecutor / ModelWorkflow
          |-- ToolExecutor / PolicyEngine
          |-- MutationCoordinator
          `-- TestExecutionCoordinator
```

## Domain language

RepairTaskPolicy is the immutable task contract. BudgetProfile is the trusted label used to
construct fixed limits; the model sees remaining numeric limits, not the label or difficulty.
RepairState is durable control and aggregate state. It is not an execution fact source.
WorkspaceBaseline is the immutable metadata manifest of the entire fixture workspace.
DiffValidationResult is a deterministic comparison fact between a baseline and a workspace scan.
RepairEvaluationRun is the report record for one task repetition.

## RepairTaskPolicy

RepairTaskPolicy is a frozen Pydantic model with normalized relative POSIX-style path patterns and
a deterministic SHA-256 policy_digest. It stores task identity, policy version, difficulty,
budget profile, path rules, profile rules, change-size rules, and all fixed limits.

Path matching is case-folded on Windows and case-sensitive on POSIX. Absolute paths, parent
traversal, empty paths, NUL, and drive-relative paths are invalid. Rule priority is always:

```text
forbidden or protected > allowed
```

The same policy digest is bound to RepairState, RuntimeSnapshot, Approval preflight, baseline,
diff validation, final verification, and RepairEvaluationRun. A mismatch fails closed.

Evaluation Mode supports exactly three fixed profiles:

| Profile | model | read | edit | test | correction | violation | wall seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| BASIC | 6 | 20 | 2 | 3 | 1 | 2 | 300 |
| ENGINEERING | 10 | 35 | 4 | 5 | 1 | 2 | 600 |
| CHALLENGE | 14 | 50 | 6 | 7 | 1 | 2 | 900 |

Interactive budget extension is deliberately absent.

## Budget semantics

- Repair model usage increments immediately before each Runtime logical Provider decision.
  ModelWorkflow separately persists every physical Provider attempt and retry. Exact cross-process
  reconciliation between those two accounting layers is deferred.
- Read usage increments when a local READ tool enters TOOL_STARTED. Reused resume results do not
  increment it.
- Edit usage increments when MutationExecutionRecord enters WRITING. Preflight denial, rejected
  approval, and hash mismatch before WRITING do not consume an edit attempt.
- Test usage increments when ProcessExecutionRecord enters STARTED. Every launched terminal result
  consumes one test run, including non-zero exit, timeout, and cancellation.
- Completion correction increments atomically when deterministic contract feedback is persisted.
- Policy violations use a separate counter. Limit exhaustion is terminal; configured severe
  violations may terminate immediately.

RepairWorkflow deduplicates counters with unique `(run_id, budget_kind, fact_id)` facts and uses
state-version conditions for explicit completion and terminal transitions. The current Evaluation
Harness serializes one Run; distributed concurrent aggregate writers are outside M7-A. Duplicate
resume reconciles execution IDs and does not count the same fact twice.

## Repair state machine

RepairCompletionStatus values are RUNNING, VERIFIED_SUCCESS, TESTS_FAILED,
FINAL_VERIFICATION_FAILED, UNVERIFIED_FINAL, BUDGET_EXHAUSTED, POLICY_BLOCKED,
DIFF_POLICY_VIOLATION, LOOP_DETECTED, MODEL_PROTOCOL_ERROR, RUNTIME_FAILURE, INDETERMINATE,
and CANCELLED.

```text
RUNNING
  -> WAITING_APPROVAL -> RUNNING
  -> COMPLETION_CHECK
       -> COMPLETION_CORRECTION -> RUNNING
       -> DIFF_VALIDATION
            -> FINAL_VERIFICATION_PENDING
                 -> VERIFIED_SUCCESS
                 -> FINAL_VERIFICATION_FAILED
       -> terminal policy/budget/unverified status
```

Terminal precedence is INDETERMINATE, CANCELLED, runtime/model protocol failure, policy/diff
violation, wall-time/budget exhaustion, final verification failure, unverified final, development
test failure, then verified success. A later control-flow event cannot overwrite a higher-priority
terminal fact. Completed Mutation and ProcessExecution records are never rewritten by cancellation.

## Completion correction

FinalAnswer is only a proposal to complete. Before accepting it, RepairCoordinator requires at
least one allowed development test, the latest development result success=true, and that result to
complete after the last committed mutation. There must also be no pending approval, execution, or
indeterminate side effect.

If the source is unverified and correction budget remains, Coordinator atomically persists one
objective Runtime Contract Feedback item and increments completion_corrections_used. The feedback
states only the failed invariant. It does not name a target file, prescribe a tool, explain a root
cause, reveal hidden tests, or provide repair steps. Default mode permits one correction; strict
mode permits zero. Exhaustion produces UNVERIFIED_FINAL.

## Workspace baseline and diff

WorkspaceBaselineBuilder scans the complete fixture workspace through WorkspacePathResolver. It
stores metadata only: normalized relative path, SHA-256, size, file kind, executable bit,
symlink/reparse state, and optional text/binary classification. Source content is not duplicated
in SQLite. A symlink or reparse point fails closed.

WorkspaceDiffValidator compares the baseline with a fresh safe scan. It does not depend on Git.
It reports modified, created, deleted, rename-like, and type-changed files; deterministic digests;
size/count totals; violations; and suspicious findings. Deleted plus created files with the same
content digest are rename-like. Ambiguous matches remain suspicious and fail under the no-rename
policy.

Preflight validates a proposed mutation before Approval. A hash-bound MutationExecutionRecord
updates source freshness after execution. Completion always performs a full workspace scan.
Forbidden and protected matches override allowed paths, and protected file hashes must match
exactly. Incremental post-mutation workspace scanning remains future hardening.

SuspiciousChangeAnalyzer is intentionally heuristic. It detects common test bypass patterns such
as skip/xfail, pytest environment checks, sys.path manipulation, site customization, dynamic test
imports, and obvious fixture-specific branches. Findings do not prove semantic incorrectness and
may contain false positives or false negatives.

## Trusted final verification

Development profiles are model-selectable only when explicitly allowed by RepairTaskPolicy. The
final verification profile is selected only by RepairCoordinator and rejected when requested by a
model ToolCall.

Final verification constructs a trusted run_tests request but still follows the complete M6 path:

```text
Tool validation -> Repair preflight -> ApprovalRequest -> AutoApprovalHarness
-> approve -> resume -> TestExecutionCoordinator -> ProcessExecutionRecord
```

No harness subprocess shortcut is permitted. The model and report receive only passed/failed,
execution ID, duration, digest, and a generic status. Hidden names, inputs, assertions, full stderr,
and fixture content never enter model context, Event payloads, or RuntimeSnapshot.

## AutoApprovalHarness

AutoApprovalHarness is available only to EvaluationHarness with a marked temporary fixture
workspace. Every approval still passes policy binding, digest binding, checkpoint binding, budget,
and indeterminate checks. It approves only permitted mutation tools, allowed development profiles,
and the coordinator-selected final profile. Every grant or denial is audited. It has no
approve-all switch and is never enabled for an arbitrary user workspace.

## Persistence and recovery

New tables are repair_task_policies, repair_states, workspace_baselines,
workspace_baseline_files, diff_validation_results, and repair_evaluation_runs. Large source,
complete diffs, prompts, hidden tests, and complete process output are not stored.

RuntimeSnapshot V4 extends V3 with safe repair identity and summary fields. Unversioned and
v1/v2/v3 data migrate with repair mode absent. Unknown versions fail closed.

Recovery reconciles snapshot and RepairState with durable facts:

1. A task created before its first model call reuses the same policy, baseline, and counters.
2. A committed mutation missing from RepairState is adopted by execution ID and never replayed.
3. A terminal development test missing from RepairState is reconstructed and never rerun.
4. A persisted correction fact is adopted without consuming correction twice.
5. A persisted diff result is reused when its baseline and final workspace digests still match.
6. A terminal final verification result is reconstructed and never rerun.
7. Any WRITING or STARTED indeterminate side effect makes Repair immediately INDETERMINATE.

## Prompt infrastructure

Evaluation Mode provides deterministic fixed-system and task templates plus a protected dynamic
Runtime context item with remaining numeric budgets and objective test/freshness state. Each fixed
template and Tool schema has a SHA-256 digest.
Strict and default completion-correction modes use identical initial templates and Tool schemas;
formal prompt optimization and benchmark prompt freeze belong to M7-B.

The templates and dynamic context do not include difficulty, budget profile name, reference repair,
hidden tests, target file, root cause, or repair steps. They include task description, allowed
behavior, path rules, development profile identity, remaining numeric budgets, and objective
development-test/freshness state.

## Security boundaries

TestProfile and diff policy are allowlists, not an OS sandbox. Hidden tests reduce benchmark
overfitting but do not prove semantic correctness. Suspicious analysis is heuristic. M7-A has no
network egress isolation or container boundary. A trusted test profile runs with the Runtime
account's OS permissions. Arbitrary shell, dependency installation, Git writes, and model-defined
process configuration remain unavailable.
