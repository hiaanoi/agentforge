# Milestone 7-B2.3 Report

## Decision

Milestone 7-B2.3, Frozen Protocol and Durable Pilot, is **PASS** for offline deterministic
evaluation. It does not authorize or report a real-model repair benchmark.

## Delivered

- Immutable `EvaluationProtocol` with canonical digests for Fixture, provider/model, prompts,
  Tool schema, ContextPolicy, RepairTaskPolicy, TestProfile template, executable/platform,
  repetitions, completion mode, and replacement policy.
- Immutable SQLite protocol registration and durable Campaign, Slot, Attempt, Event, workspace
  lease, baseline, side-effect, and result facts.
- Sequential fixed-slot scheduling with conditional claims and explicit recovery.
- Fresh workspace per Attempt; hidden tests and reference fixes remain outside the model workspace.
- Evaluator-owned baseline before the first model request and hidden verification only after
  development-test freshness and diff compliance.
- Infrastructure-only replacement with validated Attempt and evaluation-result chains.
- Model/provider failures terminalize Repair state before `RepairEvaluationRun` persistence; stable
  request categories such as `MODEL_TIMEOUT` remain available to the frozen replacement policy.
- Complete frozen result-binding validation and selected-run aggregation.
- Fail-closed recovery for ambiguous baseline, mutation, process, or approval side effects.
- Idempotent terminal result reuse and terminal workspace cleanup, including a lease created before
  its database ID was persisted.
- Redacted campaign audit and report payloads without prompts, source, output, secrets, environment
  maps, or absolute workspace paths.

## Formal Offline Pilot

The primary `self-durable-double-consumption` Fixture ran three independent repetitions through the
actual SQLite Runtime and coordinator chain:

```text
fresh workspace
-> expected visible buggy baseline failure
-> bounded baseline context
-> MockModel read
-> durable approval and exact edit
-> approved visible development test
-> full diff validation
-> evaluator-owned hidden verification
-> immutable result and selected slot
```

All three selected runs reached `VERIFIED_SUCCESS`. Each used four model requests, one read, one
edit, one model-controlled development test, and one trusted hidden final verification. Baseline
execution remained separate and did not consume the model-controlled test budget. A separate
unexpected-baseline-pass case reached `BLOCKED`/invalid before any model request. A separate
provider-timeout case persisted `RUNTIME_FAILURE` with `MODEL_TIMEOUT`, invalidated the Attempt,
used the frozen replacement allowance, and never admitted a `RUNNING` result as a selected run.

## Recovery Evidence

Tests cover Campaign/Slot/Attempt restart phases, persisted results before Attempt finalization,
post-result audit failure, pre-run workspace orphans, replacement decisions, and terminal cleanup.
They also cover the required ambiguous side-effect windows:

- baseline execution left `STARTED`;
- mutation execution left `WRITING`;
- test process left `STARTED`;
- approval left `CLAIMED` without a terminal execution fact.

These states become `INDETERMINATE`, retain the workspace for investigation, and are never
automatically retried or replaced. Ordinary `run_campaign` refuses to adopt a persisted claim;
only explicit `recover_campaign` may reconcile it.

## Verification

Environment: Windows, Python 3.14.3.

- Full pytest: **500 collected, 491 passed, 9 skipped, 0 failed**.
- Formal Pilot E2E: **3 passed**, including three successful repetitions, the
  zero-model-request unexpected-baseline-pass case, and provider-timeout replacement handling.
- Focused B2.3 protocol/persistence/factory/Runner/E2E suite: **76 passed**.
- Formal Fixture verifier: **4 fixtures x 3 repetitions, PASS**. This covers 48 fresh pytest
  executions across buggy/reference and visible/hidden combinations.
- Preflight assets: **10 passed**.
- Ruff: **All checks passed**.
- strict mypy: **100 source files, no issues**.
- compileall: **passed**.
- `git diff --check`: **passed**.

The nine skips are one opt-in OpenAI live test, one POSIX process-group test on Windows, one
POSIX executable-bit test, and six real symlink tests blocked by Windows `WinError 1314`.

## Boundaries

- B2.3 ran only deterministic MockModel responses. There is no real-model Pilot result or public
  benchmark score.
- OpenAI credentials, network execution, shell, dependency installation, Git mutation, and
  model-defined argv/cwd/environment are not part of the Pilot.
- TestProfile process control is not an OS sandbox. The formal verifier runs only reviewed
  repository-owned Python fixtures.
- Windows Job Object behavior is exercised; POSIX process-group acceptance remains platform-gated.
- Windows junction and general reparse-point coverage is incomplete.
- SQLite schema creation exists, but production schema migrations and distributed campaign leases
  do not.
- A provider request transmitted immediately before a process crash has no provider-side
  exactly-once guarantee.

## Next Scope

Milestone 7-B2.4 should authorize one reviewed real-model protocol, run a small repeated Pilot
without changing the frozen task or safety contract, classify infrastructure failures separately,
and publish raw protocol digests plus selected-run metrics. It should not add a user-facing repair
product, arbitrary shell, or weaker approval semantics.
