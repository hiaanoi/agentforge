# Milestone 7-B2.2 Evaluator-Owned Baseline Execution Design

## Status

Approved design for implementation planning. This document defines the evaluator-owned
baseline gate for formal repair pilots. It does not authorize a model run, publish evaluation
results, or change the formal fixture set.

## Objective

Every formal repair Pilot must prove that its fresh buggy workspace fails the visible
development profile for the expected reason before AgentForge sends the first model request.
The evaluator owns this check. The model cannot request, skip, configure, repeat, or cancel it.

The baseline execution must reuse the M6 managed test guarantees while remaining distinct from
the model-driven `run_tests` approval path. It is persisted and audited separately and consumes
none of the model's test, tool, or model-call budgets.

## Scope

This milestone implements:

- formal Fixture manifest support for an expected baseline failure fingerprint;
- a shared managed test execution core extracted from the existing M6 coordinator;
- a durable evaluator-owned baseline execution coordinator and repository;
- fail-closed baseline validation and crash recovery;
- a bounded, redacted baseline failure context item for the first model request;
- Pilot orchestration that runs the baseline before entering `AgentRuntime`;
- offline unit, integration, recovery, process-tree, and regression tests.

This milestone does not:

- expose baseline execution as a Tool or Runtime API available to the model;
- create synthetic ApprovalRequests for evaluator activity;
- run hidden verification at Pilot startup;
- change the model-driven development test budget;
- add shell, arbitrary command execution, dependency installation, network access, CLI,
  FastAPI, MCP, LangGraph, multi-agent behavior, or an OS sandbox;
- perform or publish formal model evaluation results.

## Design Decisions

### Shared execution core

`ManagedTestExecutionCore` owns the platform-sensitive mechanics shared by M6 and B2.2:

- execution of an already registered and rebound `TestProfile`;
- POSIX process-group or Windows Job Object supervision;
- timeout and cancellation of the complete process tree;
- bounded stdout and stderr capture;
- launch, exit, timeout, cancellation, and indeterminate outcome mapping;
- active-execution tracking keyed by execution origin and identifier.

It does not know about ApprovalRequest, RepairState, budgets, model calls, evaluation policy, or
baseline fingerprints. It accepts only a complete immutable `TestProfile`; it never resolves an
executable through `PATH` and never accepts model-supplied argv, cwd, or environment values.

The existing `TestExecutionCoordinator` retains all M6 approval, resume, budget, Run-state,
`ProcessExecutionRecord`, and ToolResult responsibilities. Its process launch and cancellation
delegate to the shared core without changing its public behavior.

`BaselineExecutionCoordinator` owns baseline-specific persistence, state transitions, profile
binding revalidation, fingerprint validation, safe-summary creation, recovery, and audit events.
It delegates only process execution to the shared core.

This split is preferred over synthetic evaluator approvals because an evaluator action is not an
approval fact. It is preferred over direct supervisor use because a second process execution path
would drift from M6 safety guarantees.

### Execution identity

The shared core uses a namespaced `ManagedExecutionKey`:

- `APPROVED_TOOL:<approval_id>` for M6;
- `EVALUATION_BASELINE:<baseline_execution_id>` for B2.2.

This key is an in-process cancellation and collision boundary. Durable identity remains in the
owning M6 or baseline record.

## Formal Fixture Contract

Each formal Fixture manifest adds this required object:

```json
{
  "expected_baseline_failure": {
    "runner": "pytest",
    "expected_exit_class": "NON_ZERO",
    "failed_node_ids": [
      "tests/visible/test_restart_dispatch.py::test_dispatch_after_restart_consumes_once"
    ],
    "match_mode": "EXACT_SET",
    "fingerprint_version": 1
  }
}
```

Constraints:

- `runner` is exactly `pytest` in B2.2;
- `expected_exit_class` is exactly `NON_ZERO`;
- `failed_node_ids` is non-empty, unique, bounded, and contains only visible-suite node IDs;
- paths use `/` regardless of host OS;
- parameterized test suffixes are part of the complete node ID;
- `match_mode` is exactly `EXACT_SET`;
- `fingerprint_version` is `1`.

The expected fingerprint digest is SHA-256 over canonical JSON containing the runner, exit
class, sorted normalized node IDs, match mode, and fingerprint version. It is not derived from
the prose failure summary.

### Actual failure matching

The pytest parser extracts terminal `FAILED <node-id>` records from bounded visible-profile
output and normalizes only path separators. Matching requires all of the following:

1. the process launched and terminated conclusively;
2. it did not time out or receive cancellation;
3. its exit code is non-zero;
4. at least one failed node ID was parsed;
5. the actual normalized node-ID set exactly equals the manifest set;
6. no collection or import error makes the result ambiguous.

Unexpected success, missing failures, additional failures, parser ambiguity, collection errors,
and import errors all block the Pilot. Exact stdout bytes, traceback line numbers, temporary
paths, object addresses, and execution times are intentionally excluded from matching because
they are unstable across supported hosts.

## Domain Model

### Baseline execution status

`BaselineExecutionStatus` contains:

- `CREATED`: durable execution intent exists but no process was launched;
- `STARTED`: one caller atomically claimed the record and may have launched a process;
- `VERIFIED_EXPECTED_FAILURE`: the visible failure conclusively matches the manifest;
- `BLOCKED`: a conclusive result makes the Pilot ineligible to call the model;
- `INDETERMINATE`: the process outcome or termination cannot be proved.

Only `CREATED -> STARTED` is a claim transition. `STARTED` may transition to one terminal state.
Terminal records are immutable except for idempotent reads.

`BaselineFailureReason` contains stable categories including:

- `UNEXPECTED_PASS`
- `FAILURE_FINGERPRINT_MISMATCH`
- `FAILURE_OUTPUT_UNPARSABLE`
- `COLLECTION_OR_IMPORT_ERROR`
- `TIMEOUT`
- `LAUNCH_FAILURE`
- `PROFILE_BINDING_MISMATCH`
- `CANCELLED`
- `PROCESS_OUTCOME_INDETERMINATE`
- `PERSISTED_STATE_INVALID`

`VERIFIED_EXPECTED_FAILURE` has no failure reason. `BLOCKED` and `INDETERMINATE` require one.

### Safe baseline summary

`BaselineFailureSummary` is immutable, versioned, bounded, and safe for persistence and model
context. It contains:

- profile ID and version;
- exit code;
- normalized visible failed node IDs;
- failure count;
- bounded redacted failure headings, exception categories, and assertion summaries;
- stdout and stderr digests;
- truncation state;
- an instruction that this was the evaluator-owned buggy baseline and that the model must choose
  its own read, edit, and retest sequence.

It excludes hidden profile identifiers, hidden test data, complete tracebacks, complete output,
source or reference-fix content, absolute temporary and interpreter paths, environment values,
credentials, and machine-specific identifiers. Redaction occurs before persistence. The
serialized safe summary has a fixed maximum size and its own digest.

## Durable State Machine

```text
CREATED
   |
   | CAS claim and profile binding revalidation
   v
STARTED
   |
   +--> VERIFIED_EXPECTED_FAILURE
   |
   +--> BLOCKED
   |
   +--> INDETERMINATE
```

The coordinator revalidates `profile_version`, `profile_digest`, `executable_path`,
`argv_digest`, `cwd`, and `environment_digest` immediately before claim and execution. Any
mismatch blocks execution before process launch.

### Recovery semantics

- `CREATED` may be claimed once with a conditional update on status and record version.
- A concurrent caller that loses the claim must not launch a process.
- `VERIFIED_EXPECTED_FAILURE` is reused after restart; baseline execution is not repeated.
- `BLOCKED` and `INDETERMINATE` return the persisted terminal result and never call the model.
- A `STARTED` record discovered after coordinator reconstruction becomes `INDETERMINATE`.
- A crash after process launch but before durable result persistence is therefore fail-closed.
- A crash after verified persistence but before the first model request reconstructs the same
  protected context item and proceeds without rerunning baseline.
- Missing, corrupt, cross-Run, cross-workspace, or digest-inconsistent state never causes a silent
  baseline restart.
- A fresh attempt requires a new Pilot, Run, baseline record, and Fixture workspace copied from
  the original immutable Fixture.

## Persistence

Add `evaluation_baseline_executions` with one row per Run and a uniqueness constraint on
`run_id`. The row stores:

- identity: baseline execution ID, Run ID, task ID, workspace baseline ID;
- workspace binding: initial workspace digest;
- control: status, failure reason, record version, result schema version;
- profile binding: profile ID/version/digest, executable path, argv digest, cwd, environment
  digest;
- expectation: expected fingerprint digest;
- result: actual fingerprint digest, normalized failed node IDs, exit code, failure count;
- process facts: root PID, process-group ID or Job ID, duration, termination reason and result;
- output metadata: stdout/stderr digests and sizes, truncation flag;
- safe failure summary JSON and digest;
- created, started, and completed timestamps.

The table does not store complete stdout/stderr, complete environment mappings, source files,
reference fixes, hidden tests, prompts, or credentials. Baseline records are separate from M6
`ProcessExecutionRecord` because they are evaluator facts and have no ApprovalRequest.

Repository operations use conditional updates for claim and terminal transitions. Terminal
updates bind the expected record version and `STARTED` status. Conflicting terminal writes fail
without overwriting the first persisted execution fact.

## Audit Events

Add:

- `EVALUATION_BASELINE_CREATED`
- `EVALUATION_BASELINE_STARTED`
- `EVALUATION_BASELINE_VERIFIED`
- `EVALUATION_BASELINE_BLOCKED`
- `EVALUATION_BASELINE_INDETERMINATE`
- `EVALUATION_BASELINE_CONTEXT_INJECTED`

Payloads contain only IDs, status, stable reason, profile ID/version/digest, workspace digest,
expected and actual fingerprint digests, failure count, output digests/sizes, truncation state,
duration, and termination metadata. They do not contain complete output, context payloads,
environment values, or Fixture content.

Baseline events do not masquerade as `TOOL_REQUESTED`, `TOOL_STARTED`, Approval, or model events.

## Context Integration

Add protected `ContextItemKind.EVALUATION_BASELINE_FAILURE`. `ContextBuilder` must not compact it
away before the first model request.

`AgentRuntime.execute` gains an evaluator-facing way to accept prevalidated initial context items.
This is a host API, not a model Tool. The Evaluation layer supplies exactly one baseline context
item reconstructed from the verified durable record. Runtime snapshots then persist it using the
existing context-item mechanism.

Immediately before calling Runtime, the Harness rechecks:

- the baseline status is `VERIFIED_EXPECTED_FAILURE`;
- the baseline Run, task, profile, workspace baseline, and initial digest bindings match;
- the Run remains `CREATED` and has not been cancelled;
- model request count and repair model-call count remain zero;
- no hidden test execution exists for the Run.

Only then may Runtime transition the Run to `RUNNING` and emit `MODEL_REQUESTED`.

## Pilot Orchestration

`EvaluationPilotRunner` is the evaluator-owned top-level workflow:

1. load and strictly validate the formal Fixture manifest;
2. verify the original Fixture and copy it to a new temporary workspace;
3. build and persist the complete WorkspaceBaseline;
4. register immutable visible and hidden TestProfiles from trusted host configuration;
5. create the Run and bind RepairState and policy;
6. create and execute the evaluator-owned visible baseline;
7. verify and persist the expected failure and safe context summary;
8. invoke `AgentRuntime` with the protected baseline context;
9. allow model-driven development tests through the unchanged M6 Approval/Resume path;
10. after FinalAnswer, require existing diff and compliance gates before hidden final
    verification.

The baseline does not consume `RepairState.test_runs_used`, Run `tool_call_count`, model request
budget, or model token budget. All development tests after the first model request continue to
consume their existing budgets at M6 `STARTED` transition time.

Hidden final verification has no baseline-stage entry point and is not registered as a model-
selectable development profile.

## Cancellation and Races

The shared execution core tracks active supervisors by `ManagedExecutionKey`. Baseline cancel
requests terminate the complete process tree through the same POSIX or Windows mechanism as M6.

- Confirmed cancellation produces `BLOCKED/CANCELLED` and transitions the Run to `CANCELLED`.
- Unconfirmed process-tree termination produces `INDETERMINATE` and an infrastructure failure.
- A persisted terminal process result is never overwritten by cancellation.
- If verification wins the persistence race but cancellation is observed before Runtime starts,
  the Run remains cancelled and the model is not called.
- If Runtime has already started, existing Runtime cancellation semantics apply.
- Repeated cancel and execute calls are idempotent and cannot launch a second process.

## Failure Classification

Unexpected pass and failure-fingerprint mismatch are evaluation setup failures, not model repair
failures. Timeout, launch failure, profile mismatch, corrupt state, and indeterminate process
outcomes are infrastructure failures. All terminate before a model request and preserve zero
model usage.

The RepairState and Run are moved to compatible terminal states through a dedicated evaluator
workflow operation. The immutable final `RepairEvaluationRun` remains the reporting record and
must expose the baseline execution ID and stable pre-model failure category when a Pilot is
blocked before Runtime execution.

## Internal APIs

```python
class ManagedTestExecutionCore:
    async def execute(
        self,
        execution_key: ManagedExecutionKey,
        profile: TestProfile,
    ) -> ManagedExecutionOutcome: ...

    def cancel(
        self,
        execution_key: ManagedExecutionKey,
        reason: str,
    ) -> bool: ...
```

```python
class BaselineExecutionCoordinator:
    def ensure_created(
        self,
        *,
        run_id: UUID,
        task_id: str,
        workspace_baseline: WorkspaceBaseline,
        profile_id: str,
        expected_failure: ExpectedBaselineFailure,
    ) -> BaselineExecutionRecord: ...

    async def execute(self, run_id: UUID) -> BaselineExecutionRecord: ...
    def recover(self, run_id: UUID) -> BaselineExecutionRecord: ...
    def get(self, run_id: UUID) -> BaselineExecutionRecord: ...
    def cancel(self, run_id: UUID, reason: str) -> BaselineExecutionRecord: ...
    def context_item(self, run_id: UUID) -> ContextItem: ...
```

Neither API is added to the Tool Registry or exposed as a general AgentRuntime operation.

## TDD Verification Matrix

### Domain, manifest, parser, and redaction

1. Reject a missing or malformed expected baseline fingerprint.
2. Reject duplicate, empty, hidden, or unbounded expected node IDs.
3. Normalize Windows and POSIX separators to the same node ID.
4. Match actual and expected node-ID sets exactly.
5. Reject unexpected pass, missing failure, extra failure, collection error, and import error.
6. Redact absolute paths and credential-like content and enforce summary limits.
7. Reject invalid baseline state and failure-reason combinations.

### Persistence and recovery

8. `ensure_created` is idempotent and rejects identity conflicts.
9. Different Runs and workspaces remain isolated.
10. Only one caller wins the `CREATED -> STARTED` claim.
11. Verified baseline is reused after process and Runtime reconstruction.
12. Legacy `STARTED` recovery becomes `INDETERMINATE` without process restart.
13. Profile version, digest, executable, argv, cwd, or environment mismatch blocks launch.
14. Missing, corrupt, or cross-bound baseline state fails closed.
15. A crash after verified persistence but before model invocation resumes with identical safe
    context.

### Runtime, budget, audit, and security integration

16. Expected baseline failure completes before the first model request.
17. The first ModelRequest contains the protected safe baseline summary.
18. Baseline execution leaves test, Tool, model-call, and token budgets unchanged.
19. The model cannot trigger, skip, configure, or repeat baseline execution.
20. Unexpected pass, mismatch, timeout, launch failure, and unconfirmed outcome produce zero model
    requests.
21. Events contain the required digests and counts but no complete output, environment, source,
    hidden-test detail, or credential.
22. Every Pilot receives a distinct copy of the original Fixture.
23. Development tests after model start consume the normal M6 budget.
24. No hidden test executes during baseline preparation.
25. Hidden verification executes only after the final compliance gate.
26. Timeout and cancellation terminate the complete process tree with no residual child process.
27. Cancellation races preserve the first conclusive execution fact and never start the model
    after pre-model cancellation.
28. M6 approval-driven test execution behavior remains unchanged after core extraction.
29. All M0 through M7-B2.1 tests continue to pass.

Final verification commands:

```text
uv run --frozen pytest -ra
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen python -m compileall src
git diff --check
```

## Acceptance Criteria

B2.2 is complete only when every Pilot has a separately persisted, audited, conclusively verified
visible buggy baseline before its first model request; every invalid or uncertain baseline stops
with zero model usage; baseline work consumes no model development-test budget; post-model tests
retain M6 semantics; hidden verification remains final-only; and all regression and static checks
pass.
