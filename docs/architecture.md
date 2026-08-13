# Architecture

> 中文阅读入口：先阅读根目录 [README](../README.md) 了解当前能力地图；投递与面试准备请看
> [中文项目导读](portfolio-guide.zh-CN.md)。本文保留完整的英文技术设计与历史边界，适合需要
> 核对 Runtime、审批、mutation、测试验证和评测流程的读者。

## Scope

Milestones 0 through 7-B2.3 establish a durable single-agent core, policy-controlled Tool Runtime,
restart-safe approval/resume orchestration, a real OpenAI Responses adapter, deterministic context
reliability, approval-gated workspace mutation, verified test execution, and constrained repair
evaluation infrastructure. AgentForge supports one Runtime process, local SQLite, fake or OpenAI
model responses, local READ tools, the local WRITE tools `write_file` and `edit_file`, and one
DANGEROUS capability: approved execution of an immutable administrator-registered TestProfile.
Arbitrary commands remain denied.

## Components

```text
AgentRuntime
  |-- ModelProvider (MockModelProvider or OpenAIModelProvider)
  |-- ModelExecutor
  |     `-- ModelWorkflow (attempts, retries, usage, budgets)
  |-- ContextBuilder + ToolResultRenderer + LoopDetector
  |-- ToolExecutor
  |     |-- ToolRegistry + Pydantic input models
  |     |-- PolicyEngine
  |     |     |-- WorkspacePathResolver
  |     |     `-- SensitiveFilePolicy
  |     |-- Local read-only repository tools
  |     |-- write_file + edit_file + AtomicMutationWriter
  |     `-- run_tests(profile_id) managed execution
  |-- RunRepository
  |-- EventRepository
  |-- CheckpointRepository
  |-- ApprovalRepository
  |-- MutationApprovalBinding + MutationExecution repositories
  |-- TestApprovalBinding + ProcessExecution repositories
  |-- ModelRuntimeState + ModelAttempt repositories
  |-- ApprovalWorkflow (short conditional transactions)
  |-- MutationCoordinator + MutationWorkflow
  |-- TestExecutionCoordinator + platform ProcessTreeSupervisor
  `-- optional RepairCoordinator
        |-- RepairWorkflow + immutable RepairTaskPolicy
        |-- WorkspaceBaselineBuilder + WorkspaceDiffValidator
        `-- trusted final-verification scheduling
             |
          SQLite

EvaluationHarness
  |-- JSON task loader + temporary fixture workspace
  |-- constrained AutoApprovalHarness
  |-- existing AgentRuntime
  `-- RepairEvaluationRun repository + metrics/reporting

PilotRunner
  |-- immutable EvaluationProtocol repository
  |-- fixed Campaign -> Slot -> Attempt state machines
  |-- durable PilotWorkspaceLease manager
  |-- PilotRuntimeFactory
  |     `-- complete AgentRuntime + evaluation object graph
  |-- evaluator-owned baseline and replacement decisions
  |-- side-effect-aware recovery reconciliation
  `-- selected-run aggregation + redacted campaign report
```

The domain package owns state and validation. Persistence maps domain objects to database rows.
The Runtime owns logical model turns, context construction, loop state, and checkpointing.
`ModelExecutor` owns physical provider attempts and retry policy. `ToolExecutor` owns the complete
tool lifecycle: validation, policy, budget accounting, events, timeout, normalization, and output
limits. OpenAI SDK objects do not cross the provider boundary.

## Execution flow

1. Create a `Run` and append `RUN_CREATED`.
2. Transition it to `RUNNING` and append `RUN_STARTED`.
3. Build deterministic typed context and strict tool schemas.
4. Reserve a physical request attempt before network access, then ask `ModelProvider` for one
   `ModelResponse`. Retryable failures consume separate persisted attempts.
5. Map the untrusted response to either `ToolCall` or `FinalAnswer`; protocol and output failures
   are normalized at the provider boundary.
6. For a tool call, `ToolExecutor` validates arguments, evaluates policy, checks budget, audits
   the request, runs the tool, bounds its output, and emits a terminal tool event.
7. Render the tool result for model context, update deterministic loop state, compact context at
   complete call/result-pair boundaries, and save RuntimeSnapshot v4.
8. For a normal Run, a final answer transitions directly to `COMPLETED`. In Repair Mode it is a
   completion proposal: development-test freshness, full workspace diff, and trusted final
   verification must succeed first.
9. Invalid output, missing tools, failed tools, exhausted budgets, detected loops, or exhausted
   steps transition the run to
   `FAILED` with an auditable reason.

## Model request flow

```text
logical MODEL_REQUESTED
  -> reserve model_runtime_states.model_request_count
  -> persist model_attempts STARTED + MODEL_ATTEMPT_STARTED
  -> OpenAI Responses request (store=false, parallel_tool_calls=false)
  -> normalized ModelResponse + provider-reported usage
  -> MODEL_RESPONDED

retryable failure
  -> MODEL_ATTEMPT_FAILED
  -> MODEL_RETRY_SCHEDULED
  -> reserve a new physical attempt
```

SDK retries are disabled so persisted physical attempts remain the source of retry truth. AgentForge
does not use `previous_response_id`; local checkpoints are the recovery source.

The Runtime remains single-action per logical step. Multiple calls are a Provider Contract
Deviation:

- STRICT rejects the complete response inside the Provider boundary and enters persisted physical
  request retries.
- SEQUENTIAL_READ_ONLY is allowed only when every call is a registered local READ tool without
  approval and the response stays within the configured maximum.

Only the selected call crosses the Provider boundary. Runtime records deviation/normalization
events, executes the selected call, and persists count-only context telling the model that discarded
calls were not executed. Discarded arguments and call IDs never enter Runtime state.

## Tool data flow

```text
model tool_call
  -> TOOL_REQUESTED (sanitized arguments)
  -> ToolRegistry lookup + Pydantic validation
  -> PolicyEngine decision
  -> Run tool-call budget increment
  -> TOOL_STARTED
  -> sync/async tool execution with timeout
  -> ToolResult normalization + output bound
  -> TOOL_COMPLETED or TOOL_FAILED
```

Policy denial emits `TOOL_REQUESTED` and `TOOL_FAILED` but never `TOOL_STARTED`.

Synchronous READ tools run in worker threads. Approved Mutation tools execute in the Runtime's
current execution context so a timeout cannot return while a detached thread continues modifying
the workspace. Their configured timeout is therefore not preemptive; bounded local file size and
explicit mutation recovery contain the operation until process isolation is added.

## Approval and resume flow

```text
REQUIRE_APPROVAL
  -> AWAITING_APPROVAL RuntimeSnapshot + ApprovalRequest
  -> WAITING_APPROVAL + RUN_PAUSED
  -> APPROVED or REJECTED decision
  -> PAUSED
  -> conditional NOT_STARTED -> CLAIMED resume
  -> approved tool execution or structured rejection result
  -> READY_FOR_MODEL RuntimeSnapshot + CLAIMED -> CONSUMED
  -> continue model from persisted history
```

The request digest binds validated raw arguments, tool name, checkpoint ID, and step. Approval rows
store only sanitized arguments and bounded result summaries; complete ToolResult data is stored in
the READY_FOR_MODEL checkpoint. Approval and consumed-result checkpoints use RuntimeSnapshot v4;
unversioned, v1, v2, and v3 checkpoints migrate on read. Context, model counters, provider
metadata, loop state, and safe repair summaries survive approval recovery. No transaction remains
open while a tool or model runs.

## Mutation flow

```text
validated WRITE request
  -> read-only preflight + MutationPlan
  -> checkpoint + ApprovalRequest + immutable binding (PREPARED)
  -> approve + conditional resume claim (WRITING)
  -> repeat path/content/hash validation
  -> fsynced temporary file
  -> no-clobber hard-link create or hash-checked os.replace
  -> COMMITTED or FAILED execution record
  -> result checkpoint + approval consumption
```

`CREATE_ONLY` never overwrites an existing path. Replacing or editing an existing file requires
the caller's `expected_sha256` to match both at preflight and immediately before publication.
`edit_file` performs one exact replacement only. Runtime restart reuses a verified COMMITTED
result without writing again; a stale WRITING record becomes INDETERMINATE and is never replayed.

## Test execution flow

```text
run_tests(profile_id)
  -> trusted TestProfileRegistry preflight
  -> DANGEROUS / TEST_PROFILE_EXECUTION policy
  -> checkpoint + ApprovalRequest + immutable TestApprovalBinding
  -> approve creates ProcessExecutionRecord CREATED
  -> conditional STARTED claim + Tool Budget increment
  -> POSIX process group or Windows Job Object supervisor
  -> bounded/redacted TestResult + terminal execution fact
  -> consumed RuntimeSnapshot v4 + model continuation
```

The model cannot provide command, argv, cwd, environment, or additional arguments. Registration
resolves argv[0] to an absolute executable once. The child receives only the Profile's complete
fixed environment. stdout/stderr are drained incrementally; complete streams are hashed and sized,
while only bounded redacted prefixes persist.

ProcessExecutionRecord is the execution fact source; Run status is control flow. A non-zero exit is
a determinate test failure returned to the model. A stale STARTED record becomes INDETERMINATE and
is not retried. A persisted terminal record is reused after restart without launching another
process. Attempts are monotonic per Run.

## Repair evaluation flow

```text
EvaluationTaskDefinition
  -> temporary isolated fixture copy
  -> full WorkspaceBaseline metadata manifest
  -> immutable RepairTaskPolicy + fixed budget binding
  -> existing AgentRuntime and approval/resume loop
  -> mutation/test facts aggregated into RepairState
  -> FinalAnswer completion check
  -> full Git-independent WorkspaceDiffValidator scan
  -> trusted hidden TestProfile through the M6 execution chain
  -> RepairEvaluationRun + TaskEvaluationSummary + safe JSON report
```

RepairCoordinator is an optional aggregation and scheduling layer, not a second Agent Loop. It
does not execute mutations, launch subprocesses, or call a Provider. MutationExecutionRecord and
ProcessExecutionRecord remain the side-effect facts. RepairState tracks counters, freshness,
completion status, baseline/diff bindings, and final-verification identity.

Evaluation Mode uses fixed BASIC, ENGINEERING, or CHALLENGE limits. Task prompt templates and the
protected dynamic Runtime state omit trusted difficulty and budget-profile labels while exposing
remaining numeric budgets and objective test/freshness facts. AutoApproval is restricted to a
bound temporary workspace and one RepairTaskPolicy digest; it still calls the normal approve/resume
APIs.

## Formal fixture admission

M7-B2.1 stores four admitted repair tasks under `evaluation/fixtures/tasks/`. Each task separates
the editable buggy workspace from evaluator-owned visible tests, hidden tests, attribution, and a
private fixed-file reference overlay. A versioned manifest binds fixed pytest argv, path ownership,
offline requirements, limits, provenance, and SHA-256 digests.

The fixture verifier validates the exact approved registry, manifest shape, path containment,
editable/protected coverage, symlink absence, immutable hashes, and overlay targets before running
anything. Every attempt uses a fresh temporary copy, an absolute interpreter, no PATH lookup, a
minimal secret-free environment, disabled pytest plugin autoload, and evaluator tests outside the
editable workspace. Buggy and reference states execute the same visible and hidden suites.

This verifier is benchmark construction infrastructure, not part of AgentRuntime and not an OS
sandbox. It executes only repository-owned, human-reviewed fixtures; its AST checks catch accidental
policy violations but cannot contain hostile Python. Formal fixture admission does not authorize
model execution.

## Evaluator-owned baseline gate

Before a formal Pilot can enter AgentRuntime, `BaselineExecutionCoordinator` runs the visible
development profile against a fresh buggy workspace. It reuses `ManagedTestExecutionCore`, the
same process-tree supervision and bounded capture core used by M6, but does not create an
ApprovalRequest or Tool event. Its independent SQLite record uses conditional `CREATED -> STARTED`
claiming and immutable terminal facts.

Only a conclusive non-zero pytest result whose normalized failed node-ID set exactly matches the
Fixture manifest reaches `VERIFIED_EXPECTED_FAILURE`. Unexpected pass, mismatch, parser ambiguity,
timeout, profile drift, and launch failure block before the Provider is called. A recovered
`STARTED` record becomes `INDETERMINATE` and is never automatically rerun. Verified records are
reused after restart.

The resulting `EVALUATION_BASELINE_FAILURE` ContextItem contains only a bounded redacted visible
failure summary and is protected from normal pair compaction. Baseline execution does not consume
Repair test budget, Run Tool budget, model-request budget, or token budget. Model-driven tests after
startup still use the ordinary M6 Approval/Resume chain. Hidden verification remains final-only.

## Protocol-bound formal Pilot

B2.3 freezes every reproducibility and safety input in one immutable `EvaluationProtocol`: task
and registry asset digests, expected baseline fingerprint, provider/model configuration, model
budget, system and task prompts, Tool schema, ContextPolicy, RepairTaskPolicy, TestProfile
template, executable and platform facts, repetition count, completion mode, and replacement
policy. Registration is immutable; a protocol name cannot be rebound to different facts.

`PilotRunner` creates the complete fixed slot set before execution and runs slots sequentially.
Each Attempt receives a fresh durable workspace lease whose model-visible tree excludes reference
fixes and hidden tests. The Runtime factory revalidates all protocol, manifest, executable,
workspace, profile, prompt, schema, policy, and provider bindings before the baseline or model can
run.

```text
register frozen protocol
  -> create campaign and fixed repetition slots
  -> CAS claim slot and create monotonic attempt
  -> create and persist fresh workspace lease
  -> assemble bound Runtime and baseline record
  -> verify expected buggy baseline
  -> execute model/approval/mutation/development-test loop
  -> validate complete workspace diff
  -> run evaluator-owned hidden final profile
  -> persist immutable RepairEvaluationRun
  -> accept slot and clean terminal workspace
  -> select effective runs and render redacted report
```

Normal `run_campaign` may execute only a slot that it successfully claimed. Persisted active work
requires explicit `recover_campaign`, preventing another Runner from silently adopting stale
claims. Recovery reuses durable results, finalizes persisted terminal facts, discovers workspace
leases created before their database binding was saved, and cleans terminal workspaces
idempotently. A baseline left `STARTED`, mutation left `WRITING`, process left `STARTED`, or
approval left ambiguously `CLAIMED` makes the Attempt and Campaign `INDETERMINATE`; it is never
replayed or replaced.

Before `EvaluationHarness` persists a result, a model-output, Provider, or request failure
terminalizes the associated Repair state. Request failures retain a stable, classified reason
such as `MODEL_TIMEOUT`, `MODEL_RATE_LIMITED`, or `MODEL_TRANSPORT_ERROR` under
`RUNTIME_FAILURE`; protocol/output violations use `MODEL_PROTOCOL_ERROR`, and the internal model
request budget maps to `BUDGET_EXHAUSTED`. Therefore a `RUNNING` Repair state cannot be persisted
or selected as a completed Pilot result. Only a category explicitly allowlisted by the frozen
replacement policy may produce a replacement Attempt.

Only allowlisted infrastructure failures can consume the frozen per-slot replacement allowance.
Attempt facts are the replacement source of truth, including pre-model failures with no
`RepairEvaluationRun`. Selection validates both Attempt and result replacement graphs, requires a
result's predecessor evaluation-run ID to equal the Attempt's persisted predecessor, and excludes
superseded infrastructure-invalid results from model-quality metrics.

## Real-model Portfolio Study

B2.4 adds a thin persisted Study layer above four existing Campaigns. The Study definition binds
the fixed task/Protocol order, one requested model alias, one exact response model ID, and Provider
configuration, source commit and tree,
`pyproject.toml`, `uv.lock`, Fixture registry, platform, and pricing snapshot by deterministic
digests. Registration creates all four Campaigns and 12 slots before execution.

`RealModelExecutionGate` requires the exact opt-in value, runtime-only API key, operator-confirmed
Study digest, immutable authorization, clean matching source provenance, and the exact ordered
Protocol set before a real Provider delegate can be constructed. The API key is excluded from
Pydantic dumps and never enters SQLite, Events, checkpoints, TestProfile environments, reports, or
command arguments. The application validates a candidate authorization against the complete gate
before the authorization CAS is persisted, so rejected opt-in, confirmation, source, or secret
checks do not leave a Study in `AUTHORIZED`.

OpenAI may resolve a requested alias such as `gpt-5.4-mini` to a dated response Snapshot such as
`gpt-5.4-mini-2026-03-17`. Smoke discovery accepts only the exact alias or a strict dated Snapshot
from the same model family. Formal preparation persists both IDs in the Provider binding, Protocol,
Study definition, authorization, manifest, and report. Execution then requires the response ID to
equal the frozen Snapshot exactly; later Snapshot drift fails closed as configuration error.

Formal TestProfiles receive a fixed evaluator-owned Python/pytest environment plus only the
allowlisted Windows runtime roots needed to start the executable. They do not inherit the host
environment, user site packages, pytest plugins, or ambient Python configuration.

`EvaluationStudyRunner` executes Campaigns sequentially. Ordinary `run` refuses to adopt a RUNNING
Study or active Campaign; only explicit `recover` may reconcile them. Scored model failures consume
their slot, allowlisted infrastructure failures may receive one replacement, configuration
failures abort later work, and ambiguous side effects make the Study INDETERMINATE. Terminal replay
reconciles missing telemetry from durable facts but performs no Provider or Tool call. Deterministic
Provider, model, Protocol, source, or gate binding errors raised during preparation are classified
as configuration failures and cannot spend an infrastructure replacement.

Public reports are built from an explicit typed allowlist. Planned, scored, successful,
infrastructure-invalid, indeterminate, and unexecuted counts use separate denominators. Quality
metrics use selected scored runs; token and cost totals include every persisted execution attempt.
Every one of the 12 predeclared slots appears in JSON and Markdown, including pending, invalid,
indeterminate, and unexecuted slots, without exposing private UUIDs. SQLite remains the private
execution fact source. The scanner rejects secrets, Prompt substrings, Provider request IDs, Tool
arguments/output, hidden/reference markers, unsafe pricing URLs, and POSIX/Windows/UNC absolute
paths before JSON or Markdown is written.

## Context and loop state

RuntimeSnapshot v4 stores typed `ContextItem` values independently from the compatibility history
field. Repository tool outputs are rendered into deterministic bounded summaries before they enter
model context. When limits are exceeded, `ContextBuilder` removes the oldest complete tool-call /
tool-result pairs and emits `CONTEXT_COMPACTED`; approval results, runtime errors, and loop warnings
are protected items.

`LoopDetector` compares deterministic action, result, and stable-error digests. It warns at the
configured threshold and fails at the terminal threshold. This is exact-repeat protection, not a
semantic similarity detector.

## Current boundaries

- A step is one model response, whether it requests a tool or returns a final answer.
- SQLite operations are synchronous and short-lived; model and tool boundaries are async.
- Domain timestamps are normalized to timezone-aware UTC values after SQLite reads.
- Event sequence allocation is transactional for normal single-process use, but distributed
  concurrent writers are outside this milestone.
- RuntimeSnapshot v4 migration supports unversioned history and v1/v2/v3 snapshots. Unknown
  future versions fail explicitly.
- Repository traversal skips symlink entries. Resolver logic canonicalizes explicit paths and is
  designed to deny final targets outside the workspace. Real symlink cases could not run under
  the current Windows account because link creation failed with `WinError 1314`; junctions and
  other reparse-point variants remain incompletely verified.
- Synchronous READ tools run in a worker thread. Timeout stops waiting but cannot kill that
  thread. Mutation tools run inline to prevent detached writes, so their timeout is advisory and
  cannot preempt filesystem code.
- External cancellation is audited as `TOOL_FAILED` and then propagated to the caller. It is not
  converted into an ordinary tool result.
- Fixed Git subprocesses discard inherited `GIT_*` control variables before execution. Git output
  is bounded before entering model history, although the subprocess capture itself is currently
  in memory.
- A stale approved CLAIMED request has an unknown side-effect outcome. Recovery marks it
  INDETERMINATE and fails the Run instead of retrying the tool.
- Conditional SQLite updates and an in-memory active-Run guard prevent duplicate resume inside the
  supported single-process architecture; distributed leases are not implemented.
- Model request limits count physical attempts. Token totals use provider-reported values only;
  context limits use item, character, and UTF-8 byte counts because no tokenizer is included.
- A process crash after a provider request is transmitted but before its result is persisted can
  lead to a billed duplicate on retry. There is no provider-side exactly-once guarantee.
- Only `write_file` and `edit_file` provide WRITE capability, and both require approval. The only
  executable DANGEROUS capability is approval-bound TEST_PROFILE_EXECUTION. TestProfile is not an
  OS sandbox. Arbitrary shell, model-defined process configuration, dependency or Git mutation,
  FastAPI, product CLI, MCP, LangGraph, and additional providers are absent. B2.4 includes an
  evaluator-only runbook script, but its real OpenAI smoke and formal 12-slot Study have not been
  executed.
- Campaign ownership uses SQLite CAS plus a process-local active guard. There is no distributed
  worker lease or cross-host scheduler. A stale active claim requires an explicit recovery call.
- SQLite schema creation is supported, but versioned production migrations are not.
- Evaluation fixtures and policy checks do not provide filesystem, process, or network isolation.
  SuspiciousChangeAnalyzer is heuristic, and hidden tests reduce overfitting without proving
  semantic correctness.
- Windows Job Object process-tree timeout and cancellation were exercised locally. The POSIX
  process-group implementation is covered by a platform-gated test but was skipped on Windows.
