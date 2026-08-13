# Implementation Plan

## Milestone 0: complete

- Create a Python `src` layout managed by uv.
- Configure pytest, pytest-asyncio, Ruff, and strict mypy.
- Add project metadata, ignored files, environment template, and architecture documentation.

## Milestone 1: complete

- Add typed domain objects and validated run-state transitions.
- Add SQLite tables and repositories for runs, events, and checkpoints.
- Add a deterministic mock model provider.
- Add a safe in-process tool registry and executor.
- Add the minimal single-agent runtime and deterministic tests.

## Milestone 2: complete (PASS)

- Add a typed Tool protocol, deterministic registry ordering, schema export, and duplicate
  registration errors.
- Add a Windows-aware workspace resolver and centralized sensitive-file rules.
- Add structured policy decisions for tool existence, arguments, risk, approval, paths,
  cancellation, and tool-call budgets.
- Move the complete audited tool lifecycle into `ToolExecutor`.
- Add bounded read-only repository tools for listing, reading, searching, and Git diff.

## Milestone 2 verification

- `uv sync --frozen`: passed against the official PyPI lock file.
- Python 3.14.3.
- Full suite: 80 collected, 78 passed, 2 skipped, 0 failed.
- Both skips are real symlink tests blocked by Windows `WinError 1314`.
- Git/Diff focused suite: 5 passed, including all three real Git subprocess tests.
- Ruff: all checks passed.
- strict mypy: 30 source files, no issues.
- compileall: passed.

## Milestone 2 deferred scope

Human approval execution, write tools, test execution tools, FastAPI, real model providers,
recovery APIs, MCP, LangGraph, evaluation suites, Docker, and multi-agent execution were
intentionally deferred at the end of M2.

Milestone 2 permits local `READ` tools only. `WRITE`, `DANGEROUS`, and approval-required tools are
policy-denied rather than executed. Symlink containment is implemented but was not exercised on
this Windows account; junction and broader reparse-point coverage remains future hardening work.

Current durability is limited to persisted runs, audit events, and checkpoint snapshots. Public
pause/resume behavior, atomic run-event-checkpoint transactions, and concurrent event writers
belong to later milestones.

## Milestone 3: implementation complete

- Add versioned RuntimeSnapshot recovery contexts.
- Persist ApprovalRequest facts independently from complete tool results.
- Add deterministic digest binding over validated arguments, checkpoint, and step.
- Add short conditional transactions for pause, decision, claim, consumption, and cancellation.
- Add Runtime methods for pending approvals, approve, reject, resume, and cancel.
- Resume approved tools once, feed rejected calls back as structured results, and recover after
  process recreation without restarting from empty history.
- Mark stale approved CLAIMED calls INDETERMINATE instead of retrying unknown side effects.

Milestone 3 remains a core Runtime feature. FastAPI, CLI, real models, write/shell tools, MCP,
LangGraph, evaluation, distributed workers, and a frontend remain deferred.

### Milestone 3 verification

- Full suite: 106 collected, 104 passed, 2 existing Windows symlink skips, 0 failed.
- Ruff: all checks passed.
- strict mypy: 32 source files, no issues.
- compileall: passed.

## Milestone 4: complete (PASS)

### M4A: real model foundation

- Add provider-neutral `ModelResponse`, usage, budget, configuration, and stable model errors.
- Add an asynchronous OpenAI Responses adapter behind the `ModelProvider` boundary.
- Convert ToolSpecs to strict function schemas and preserve provider-neutral call IDs.
- Disable SDK retries; persist every physical attempt before network access and own retry/backoff
  in `ModelExecutor`.
- Persist request counts and provider-reported usage in one model runtime state per Run.
- Add RuntimeSnapshot v2 with strict unversioned/v1 migration and unknown-version rejection.

### M4B: context reliability

- Add typed ContextItems, deterministic context construction, and pair-boundary compaction.
- Add deterministic repository ToolResult rendering with size, truncation, count, and SHA-256
  metadata.
- Add exact repeated action/result/error loop warning and terminal thresholds.
- Persist context and loop state across normal checkpoints and durable approval consumption.
- Emit model-attempt, retry, budget, compaction, and loop audit events without prompt/result data.
- Add fake-client offline tests and an explicit opt-in live OpenAI fixture-repository test.

### Current verification evidence

- Python: 3.14.3.
- Full suite: 157 collected, 154 passed, 3 skipped, 0 failed.
- Skips: one opt-in live OpenAI test and two Windows symlink tests blocked by `WinError 1314`.
- All repository/Git tests passed, including the three real Git subprocess tests.
- Ruff: all checks passed.
- strict mypy: 44 source files, no issues.
- compileall: passed.
- `git diff --check`: passed with line-ending conversion warnings only.
- M4.1 multi-tool policy: STRICT and conditional SEQUENTIAL_READ_ONLY implemented.
- Three live read-only fixture runs: two completed, one exhausted its timeout retry before any
  Provider response or tool execution.

## Milestone 5: complete (PASS)

- Add local approval-required `write_file` with CREATE_ONLY and EXPECTED_HASH_REPLACE modes.
- Add local approval-required `edit_file` with one exact replacement and expected-hash binding.
- Validate mutation paths, sensitive names/content, UTF-8 text, byte limits, and file state before
  approval and again before execution.
- Publish via fsynced temporary files, no-clobber hard-link creation, or hash-checked `os.replace`.
- Persist immutable mutation approval bindings and PREPARED/WRITING/COMMITTED/FAILED/
  INDETERMINATE execution records.
- Atomically claim Run, Approval, and mutation state; reuse verified committed results and never
  replay uncertain WRITING side effects.
- Expose Runtime mutation execution query APIs and safe mutation audit events.
- Execute Mutation tools inline to prevent a timed-out detached worker thread from continuing a
  workspace write; the timeout is non-preemptive until process isolation exists.

### Milestone 5 verification

- Python: 3.14.3.
- Full offline suite: 217 collected, 213 passed, 4 skipped, 0 failed.
- Skips: one explicit opt-in OpenAI live test and three real symlink tests blocked by Windows
  `WinError 1314`; simulated reparse-point protection passed.
- Focused Tool Runtime and mutation regression: 35 passed.
- Ruff: all checks passed.
- strict mypy: 54 source files, no issues.
- compileall and `git diff --check`: passed.

## Milestone 6: complete (PASS)

- Add immutable administrator-registered TestProfiles with one-time absolute executable resolution,
  fixed argv/cwd/environment, versions, and deterministic binding digests.
- Add profile-id-only `run_tests` as the sole approval-capable DANGEROUS capability.
- Persist TestApprovalBinding and versioned ProcessExecutionRecord facts with monotonic per-Run
  attempts and conditional state transitions.
- Add bounded streaming stdout/stderr capture, complete stream digests, redacted summaries, and
  structured TestResult context.
- Supervise full process trees with POSIX sessions/groups or real Windows Job Objects. Timeout and
  cancel terminate the tree; uncertain termination becomes INDETERMINATE and is not retried.
- Add Runtime profile/execution query APIs, managed execution, restart recovery, cancel concurrency,
  RuntimeSnapshot v3, and MockModel edit-then-test integration.

### Milestone 6 verification

- Python: 3.14.3.
- Full suite: 268 collected, 262 passed, 6 skipped, 0 failed.
- Skips: one opt-in OpenAI Live test, one POSIX-only process-group test on Windows, and four real
  symlink tests blocked by Windows `WinError 1314`.
- Real Windows Job Object timeout, cancel, child-tree, fixed-environment, and bounded-output tests
  passed with no residual child process.
- Ruff: all checks passed.
- strict mypy: 69 source files, no issues.
- compileall and `git diff --check`: passed.

## Milestone 7-A: complete (PASS)

- Add immutable RepairTaskPolicy contracts with deterministic digests, protected/forbidden path
  precedence, allowed profile bindings, and fixed BASIC/ENGINEERING/CHALLENGE budgets.
- Persist RepairState, budget-consumption facts, full-workspace metadata baselines, diff validation
  summaries, and RepairEvaluationRun records in SQLite.
- Add RuntimeSnapshot v4 with migration from unversioned/v1/v2/v3 snapshots and bounded repair
  recovery state.
- Add RepairCoordinator completion checks, one objective default correction, strict zero-correction
  mode, development-test freshness, terminal precedence, and indeterminate fail-closed behavior.
- Add Git-independent full workspace diff validation, protected file hashing, change-size/count
  limits, and heuristic suspicious test-bypass detection.
- Integrate Repair preflight before mutation/test approvals and recheck it during approved
  execution. Model selection of the hidden final profile is denied.
- Route trusted final verification through the existing M6 ApprovalWorkflow,
  TestExecutionCoordinator, process-tree supervisor, and ProcessExecutionRecord chain.
- Add constrained AutoApproval for one temporary evaluation workspace and bound policy digest.
- Add JSON task loading, repeatable fixture copying, prompt/schema digests, evaluation metrics,
  safe JSON reporting, and persisted repetition/replacement models.
- Add a MockModel synthetic E2E covering read, approved edit, approved development test, full diff,
  hidden final verification, and verified completion.

### Milestone 7-A verification

- Python: 3.14.3.
- Full suite: 337 collected, 328 passed, 9 skipped, 0 failed.
- Skips: one opt-in OpenAI Live test, one POSIX process-group test, one POSIX executable-bit test,
  and six real symlink tests blocked by Windows `WinError 1314`.
- Synthetic constrained-repair E2E: passed.
- Real Windows Job Object tests and all M0-M6 regressions: passed.
- Ruff: all checks passed.
- strict mypy: 85 source files, no issues.
- compileall and `git diff --check`: passed.

M7-A is Evaluation Mode infrastructure only. It is not the complete Milestone 7 benchmark result
and does not establish GPT-5.4 repair quality.

## Milestone 7-B1 / 7-B2.0: complete (PASS)

- Audited ten accepted candidates across QuixBugs, BugsInPy, SWE-bench, and original designs.
- Recorded source, license, crop, dependency, Windows/Python 3.14, offline, difficulty, and hidden
  oracle evidence with explicit VERIFIED/INFERRED/UNVERIFIED classification.
- Executed candidate preflight and admitted six GO plus four CONDITIONAL candidates.
- Human-approved the first fixture batch and retained `self-durable-double-consumption` as the main
  demo with `self-terminal-state-cas` as its design-level backup.

## Milestone 7-B2.1: complete (PASS)

- Built four formal offline fixtures with editable buggy workspaces, evaluator-owned visible and
  hidden tests, immutable reference overlays, attribution, and versioned manifests.
- Added fail-closed registry/manifest/path/hash/overlay validation and fresh-copy pytest execution
  with a secret-free fixed environment.
- Repeated every buggy/reference and visible/hidden combination three times: 48 executions,
  24 expected buggy failures, 24 reference passes, 0 timeouts, and 0 unexpected outcomes.
- Full suite: 389 collected, 380 passed, 9 skipped, 0 failed.
- Ruff, strict mypy for 85 source files, compileall, and `git diff --check`: passed.
- No model evaluation was performed or authorized.

## Milestone 7-B2.2 baseline gate: complete (PASS)

- Added exact visible pytest failure fingerprints to all four formal Fixture manifests and the
  three-repeat verifier.
- Added a shared ManagedTestExecutionCore while preserving M6 approval, budget, persistence, and
  cancellation semantics.
- Added independent SQLite baseline records, conditional claims, profile rebinding, terminal
  immutability, and fail-closed STARTED recovery.
- Added evaluator-owned baseline orchestration before AgentRuntime and protected bounded/redacted
  initial context.
- Baseline execution consumes no model development-test, Tool, model-call, or token budget.
- Formal Pilot workspaces are fresh copies; reference fixes and hidden tests are not placed in the
  model workspace. Hidden verification remains final-only.
- Fixture verification: 48 executions, all expected outcomes and visible failure fingerprints
  matched, with 0 timeouts.
- Full suite: 414 collected, 405 passed, 9 skipped, 0 failed.
- Ruff, strict mypy for 91 source files, compileall, and `git diff --check`: passed.
- No real model repair Pilot, repeated scoring run, or published benchmark result was performed.

## Milestone 7-B2.3 frozen protocol and durable Pilot: complete (PASS)

- Added immutable, canonical EvaluationProtocol records binding all Fixture, provider/model,
  prompt, Tool schema, ContextPolicy, RepairTaskPolicy, TestProfile, executable, platform,
  repetition, completion, and replacement facts.
- Added immutable protocol registration, fixed Campaign slots, monotonic Attempt records, CAS state
  transitions, redacted campaign events, and durable workspace leases.
- Added a trusted PilotRuntimeFactory that assembles the complete existing Runtime and coordinator
  graph and fails before execution when any frozen binding drifts.
- Added PilotRunner sequential scheduling, explicit recovery, terminal-result reuse, orphan lease
  cleanup, side-effect-aware indeterminate handling, and infrastructure-only replacements.
- Added complete result-binding validation, including exact Attempt/result replacement-predecessor
  agreement before a result can be selected.
- Added selected-run metrics and safe campaign reports that exclude invalid replacements, prompts,
  outputs, secrets, and local paths.
- Added a formal MockModel E2E with three fresh repetitions through baseline, durable approval,
  exact edit, visible tests, diff validation, and hidden final verification.
- Added a fail-closed unexpected-baseline-pass E2E proving zero model requests.
- Added a provider-timeout E2E proving `MODEL_TIMEOUT` reaches a durable terminal Repair state,
  creates an invalid result, and follows only the frozen replacement policy.
- Full suite: 500 collected, 491 passed, 9 skipped, 0 failed.
- Focused B2.3 protocol, persistence, factory, Runner, and E2E suite: 76 passed.
- Fixture verifier: 4 fixtures x 3 repetitions passed; preflight suite: 10 passed.
- Ruff, strict mypy for 100 source files, compileall, and `git diff --check`: passed.
- No real-model repair Pilot or public benchmark result was produced.

## Milestone 7-B2.4 real-model Portfolio Pilot: offline implementation and smoke complete

- Implemented scope: the four admitted formal Fixtures, three repetitions each, one exact OpenAI
  model/configuration, and 12 predeclared scoring slots.
- Each task retains an independent immutable EvaluationProtocol and durable Campaign. A new
  evaluator-owned Study layer binds authorization, sequential execution, recovery, aggregate
  metrics, and public reporting.
- The full execution gate is validated before authorization is persisted. Deterministic
  Provider/Protocol/source binding errors abort the Study without spending a replacement.
- Model-quality failures remain score-eligible and are never replaced. Only allowlisted
  infrastructure failures may use one replacement per slot; indeterminate side effects stop the
  Study fail-closed.
- Implemented telemetry includes physical/logical model request counts, retries, token classes,
  Provider deviations, Tool/approval/test behavior, latency, and pricing-snapshot-based cost
  estimates.
- The Study remains an AgentForge Portfolio Pilot over cropped Fixtures, not an official upstream
  benchmark.
- Public JSON and Markdown include all 12 planned slots without private UUIDs or raw execution
  content; private durable facts remain in SQLite.
- Focused B2.4 verification: 131 passed, comprising 99 unit/persistence and 32
  integration/security tests.
- Full offline verification: 636 collected, 627 passed, 9 skipped, 0 failed, with one existing
  pytest collection warning.
- Ruff, strict mypy for 117 source files, compileall, Fixture verification
  (`4 fixtures x 3 repetitions`), `git diff --check`, and `uv.lock` identity passed.
- The read-only real OpenAI smoke passed: 4 model requests, 3 tool calls, 3 discarded calls, complete
  usage, and a completed final answer. The formal 12-slot Study remains pending and no aggregate
  real-model repair result is reported.

Detailed documents:

- `docs/milestone_07b2_4_design.md`
- `docs/milestone_07b2_4_test_plan.md`
- `docs/milestone_07b2_4_implementation_plan.md`

## Deferred after Milestone 7-B2.4 offline implementation

- Explicitly authorized OpenAI smoke, formal 12-slot Study execution, and evidence-based final
  B2.4 status/report closure.
- Interactive mode budgets, BudgetExtensionRequest, and human-approved dynamic budget changes.
- A user-facing automatic bug-repair product loop.
- File deletion/move/rename, general patch parsing, dependency installation, and Git mutation.
- FastAPI, CLI product surface, frontend, MCP, and LangGraph.
- Additional model providers, tokenizer-aware limits, distributed workers, and schema migrations.
- Public evaluation datasets, production tracing, RAG, multi-agent orchestration, and Docker.
- Process-isolated mutation execution and comprehensive Windows junction/reparse validation.
- OS sandboxing/container isolation and POSIX process-group acceptance on a POSIX host.
