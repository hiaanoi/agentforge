# Milestone 6 Design: Verified Test Execution Runtime

## Scope

Milestone 6 adds one controlled process capability: an Agent may select an administrator-
registered TestProfile and, after durable approval, execute that exact profile. It does not add
arbitrary shell, model-supplied commands or arguments, dependency installation, network tools,
Git mutation, containers, automatic rollback, automatic repair, or multi-agent execution.

TestProfile is a command allowlist, not an operating-system sandbox. M6 owns the complete process
tree and bounds persisted output, but the selected test process still has the permissions of the
AgentForge operating-system account.

## Architecture audit

- ToolExecutor already owns validation, policy, budgets, and generic tool audit events. M6 adds a
  managed execution path so run_tests can reuse that lifecycle without placing process code in the
  Tool object.
- ApprovalWorkflow already persists checkpoint, approval, Run pause, and optional mutation binding
  atomically. It will accept one mutually exclusive test binding.
- MutationCoordinator establishes the M5 pattern for a side-effect-specific coordinator and
  conditional workflow. TestExecutionCoordinator follows that pattern without reusing mutation
  records.
- RuntimeSnapshot v2 has no typed test recovery state. M6 introduces v3 and strict v2 migration.
- Policy currently denies every DANGEROUS tool. M6 permits only a local, approval-required tool
  carrying the TEST_PROFILE_EXECUTION capability; every other DANGEROUS tool remains denied.

## Component boundaries

```text
run_tests(profile_id)
  -> TestProfileRegistry preflight
  -> ToolExecutor validation and PolicyEngine
  -> TestApprovalRequired
  -> ApprovalWorkflow + TestApprovalBinding + RuntimeSnapshot v3
  -> TestExecutionCoordinator
       |-- TestExecutionWorkflow
       |-- ProcessExecutionRepository
       `-- ProcessTreeSupervisor
            |-- PosixProcessGroupSupervisor
            `-- WindowsJobObjectSupervisor
  -> bounded, redacted TestResult
  -> consumed checkpoint
  -> model continuation
```

run_tests is intentionally lightweight. Its ordinary execute method fails closed. Approved
execution is possible only through ToolExecutor's managed lifecycle and TestExecutionCoordinator.
Runtime and ToolExecutor do not branch on operating-system APIs.

## TestProfile and registry

TestProfileRegistry is constructed and populated only by trusted application startup code. There
is no Runtime or Tool API for registration, replacement, disabling, or environment modification.

At registration, a TestProfile is canonicalized and frozen with:

- profile_id, name, and description;
- an absolute resolved executable path;
- argv whose first item is that exact executable path;
- an absolute existing workspace-contained cwd;
- a complete fixed allowed_env mapping;
- timeout_seconds, max_output_bytes, enabled, and positive profile_version;
- deterministic executable, argv, cwd, environment, and complete profile SHA-256 digests.

The registry resolves a bare executable exactly once using the trusted startup PATH. Execution
uses the absolute result and never performs PATH search. argv cannot contain NUL or empty values.
cwd cannot escape the workspace or traverse a symlink/reparse component.

allowed_env is the complete child environment. It does not inherit the host environment. Variable
names associated with API keys, tokens, secrets, passwords, credentials, SSH, cloud credentials,
or provider credentials are rejected. Environment values are included in environment_digest and
profile_digest but are never persisted or audited.

Before resume, the registry compares the current profile against TestApprovalBinding:
profile_version, profile_digest, executable_path, argv_digest, cwd, and environment_digest must all
match. Missing, disabled, or changed profiles fail before process creation.

## Domain model

TestExecutionPlan is immutable safe approval metadata. TestApprovalBinding extends it with
approval_id, run_id, checkpoint_id, and tool_call_digest. It stores no argv or environment values.

ProcessExecutionRecord is the execution fact source. Run status is only control-flow state. The
record includes:

- execution_id, run_id, approval_id, and tool_call_digest;
- attempt_number, record_version, result_schema_version;
- profile identity and all binding digests;
- cwd and executable path as protected database metadata;
- root_pid, POSIX process_group_id, or Windows job_id;
- status and optional failure_kind;
- exit_code, stdout/stderr digests, actual sizes, bounded redacted summaries, and truncation flags;
- duration_ms, termination_reason, termination_result, and timestamps.

One approval has at most one execution. `(run_id, attempt_number)` is unique. Each new model
run_tests request creates a new approval and, after approval, the next attempt number. Repeated
approve or resume never creates another attempt.

ProcessExecutionStatus values are CREATED, STARTED, COMPLETED, FAILED, TIMEOUT, CANCELLED, and
INDETERMINATE. ProcessFailureKind distinguishes TEST_FAILURE, LAUNCH_FAILURE, TIMEOUT, CANCELLED,
PROFILE_MISMATCH, SUPERVISOR_FAILURE, and INDETERMINATE.

TestResult.success describes the tests, not infrastructure. Exit zero produces success=true.
Non-zero exit produces success=false with FAILED/TEST_FAILURE and is still a successful managed
tool operation whose structured result is returned to the model. Launch, binding, supervisor, and
indeterminate failures are Runtime failures. Confirmed timeout is a determinate structured result.

## Approval and budget flow

1. The model supplies only profile_id.
2. The registry preflights an enabled profile and creates TestExecutionPlan without starting a
   process.
3. Policy returns REQUIRE_APPROVAL for the sole TEST_PROFILE_EXECUTION capability.
4. Runtime persists RuntimeSnapshot v3, ApprovalRequest, TestApprovalBinding, TEST_REQUESTED,
   APPROVAL_REQUESTED, and RUN_PAUSED in one transaction.
5. Approval creates one CREATED ProcessExecutionRecord and allocates attempt_number.
6. Resume revalidates the complete binding.
7. A conditional transaction changes approval NOT_STARTED to CLAIMED, Run PAUSED to RUNNING, and
   execution CREATED to STARTED.
8. Only this STARTED transition increments Tool Budget and emits TOOL_STARTED/TEST_STARTED.
9. No database transaction remains open during process execution.

Pending approval and CREATED records do not consume Tool Budget.

## Process-tree supervision

Process execution always uses shell=false, the fixed absolute executable, fixed argv, fixed cwd,
and fixed complete environment.

On POSIX, the root process starts a new session. The supervisor records its process group ID and
sends SIGTERM then SIGKILL to the complete group on timeout or cancellation. A determinate result
requires confirmation that the group is gone.

On Windows, an internal trusted launcher waits before starting the profile process. The launcher
is first assigned to a Job Object configured with KILL_ON_JOB_CLOSE and without breakaway. Only
then may it start the profile. The profile and descendants inherit Job membership. Timeout or
cancellation terminates the entire Job and confirms zero active processes. Windows tests use the
real Job Object API and are skipped on non-Windows platforms; no fake Windows API is used.

stdout and stderr are drained concurrently in bounded chunks. Each stream keeps at most
max_output_bytes in memory while continuing to count bytes and compute the SHA-256 of the complete
stream. Excess bytes are discarded immediately. Captured bytes are decoded with replacement,
control-normalized, and secret-redacted before persistence or model context. Complete raw output
is never stored.

## Cancellation and concurrency

The platform supervisor runs in a dedicated worker thread and exposes a thread-safe active handle.
AgentRuntime.cancel keeps its synchronous API. For an active execution it requests whole-tree
termination and waits for the supervisor's bounded confirmation before final control-flow state is
written.

Execution completion, timeout, cancellation, and recovery use conditional updates over STARTED and
record_version. Exactly one terminal execution fact wins:

- if natural completion wins, later cancel may cancel the Run but cannot rewrite the execution;
- if cancellation wins, late process exit cannot overwrite CANCELLED;
- timeout and cancel use one locked termination cause;
- inability to confirm whole-tree termination becomes INDETERMINATE.

External cancellation of the resume coroutine synchronously terminates and confirms the process
tree before CancelledError propagates. No worker thread or process tree may remain detached.

## Crash recovery

- APPROVED plus CREATED: revalidate the profile, atomically claim STARTED, execute once.
- CLAIMED plus STARTED after process recreation: mark INDETERMINATE and fail the Run. Do not attach
  by PID/PGID or retry because identifiers may be reused and the prior side-effect state is unknown.
- Terminal execution before result checkpoint: reconstruct TestResult from the execution record,
  persist a consumed checkpoint, and never rerun.
- Consumed result checkpoint before model continuation: load RuntimeSnapshot v3 and continue the
  model with last_test_result.
- Missing/corrupt binding, profile drift, or inconsistent identities fail explicitly.

RuntimeSnapshot v3 stores only pending_test_execution safe identity/digests, last_test_result
bounded redacted fields, and test_execution_state in addition to v2 state. It stores no command,
argv, environment, complete output, or unredacted output. v2 migrates with empty test state;
unknown future versions fail.

## Audit

Events are TEST_REQUESTED, TEST_STARTED, TEST_COMPLETED, TEST_FAILED, TEST_TIMEOUT,
TEST_CANCELLED, and TEST_INDETERMINATE. Payloads may include IDs, profile ID/version/digest,
attempt, status/failure kind, exit code, duration, stream digest/size, truncation, and termination
status. They never include executable path, argv, cwd, environment, raw output, summaries, or
credentials.

## Non-goals and debt

- TestProfile is not an OS sandbox. Profiles can read or modify anything available to the runtime
  account unless the operator provides external OS isolation.
- M6 adds no arbitrary shell, command parameters, dependency install, network tool, Git write,
  rollback, repair loop, Docker, or container.
- SQLite and process execution do not form one transaction. Unknown STARTED outcomes fail closed.
- Profile configuration remains trusted in-process startup configuration rather than a managed
  persistent administration system.
