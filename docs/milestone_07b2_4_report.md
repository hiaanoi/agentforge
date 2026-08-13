# Milestone 7-B2.4 Report

## Status

**Offline implementation: PASS**

**Real-model smoke: PASS; formal 12-slot Study: NOT RUN**

This is an implementation and offline-verification report. It is not a real-model benchmark
result and does not claim repair quality, a Provider invoice, or an official SWE-bench score.

## Implemented

- Explicit `SCORED`, `INFRASTRUCTURE_INVALID`, and `INDETERMINATE` outcomes with separate failure
  classes for model quality, infrastructure, configuration, and uncertain side effects.
- Sanitized physical model-attempt queries and immutable per-run telemetry.
- Frozen decimal pricing snapshots and usage-completeness-aware cost estimates.
- A durable four-Campaign Study with CAS transitions, ordered events, explicit authorization, and
  fixed 4 x 3 slot bindings.
- Clean Git/source/dependency provenance and fixed TestProfile environment construction.
- Four frozen REAL_MODEL Protocols with BASIC/ENGINEERING physical request and token envelopes.
- A real-model gate requiring exact opt-in, runtime secret presence, operator confirmation,
  source integrity, and ordered Protocol identity. Authorization persists only after the complete
  gate succeeds.
- Sequential Study execution, explicit recovery, configuration abort, infrastructure gaps,
  indeterminate fail-closed behavior, and terminal zero-side-effect replay.
- Deterministic Provider/Protocol/source/gate binding failures abort without infrastructure
  replacement.
- Requested Provider aliases and exact response Snapshot IDs are separately frozen; formal
  execution rejects response identity drift.
- Evaluator-owned fixed Python/pytest environments that do not inherit host secrets, user site
  packages, or ambient pytest plugins.
- Denominator-correct, typed public reports that include all 12 planned slots and scan for
  secrets, Prompts, unsafe URLs, absolute paths, hidden data, and reference data.
- Evaluator-only `prepare`, `canary`, `run`, `recover`, and `report` commands. `canary` executes
  only one recoverable slot from the first Campaign and leaves the Study non-terminal. This is
  not a product CLI.

## Real-Model Smoke Evidence

The explicit local smoke ran against a generated temporary read-only repository and did not access
the AgentForge workspace or formal Fixtures. The requested and returned model was
`gpt-5.6-luna`.

- Result: `1 passed` in `24.30s`.
- Model requests: `4` of the limit `5`.
- Tool calls: `3` of the limit `5`.
- Provider returned multiple calls: `true`; maximum returned calls: `3`.
- Selected calls: `2`; discarded calls: `3`; normalization and Provider deviation events: present.
- Final answer completed and cited both temporary fixture paths.
- Completed-request usage and all-physical-request usage: complete.

The alias-resolution fix was re-verified with `gpt-5.4-mini` on 2026-07-30:

- Result: `1 passed` in `31.25s`.
- Requested model alias: `gpt-5.4-mini`; resolved response model: `gpt-5.4-mini-2026-03-17`.
- Model requests: `4`; tool calls: `3`; returned-call maximum: `3`.
- Selected calls: `2`; discarded calls: `3`; normalization and Provider deviation events: present.
- Final answer, temporary fixture citations, and complete usage metadata: present.

## Offline Verification

- Python: 3.14.3.
- Focused unit/persistence suite: 99 passed.
- Focused integration/security suite: 32 passed.
- Focused B2.4 aggregate: 131 passed.
- Full suite: 636 collected, 627 passed, 9 skipped, 0 failed.
- Fixture verifier: 4 fixtures x 3 repetitions, PASS.
- Ruff: all checks passed.
- strict mypy: 117 source files, no issues.
- compileall: passed.
- `git diff --check`: passed with Windows line-ending conversion warnings only.
- `uv.lock`: Git blob identical to the B2.3 baseline.

The nine skips are one explicit OpenAI live opt-in test, one POSIX process-group test, six real
symlink tests unavailable under the current Windows account (`WinError 1314`), and one
POSIX-specific executable-bit test. Pytest also emitted one existing collection warning for the
domain model named `TestExecutionPlan`; it did not affect test execution.

## Truth Boundary

The OpenAI smoke has passed, but no formal Study has been prepared from the clean committed B2.4
source tree, authorized, or executed. Therefore there are no real 12-slot success counts, aggregate
tokens, replacement counts, or cost results to publish yet. Smoke metrics are not Study metrics.

Provider-side requests cannot be exactly-once across a crash. TestProfile process controls are not
an OS sandbox. Windows junction/reparse coverage remains incomplete, POSIX process-group acceptance
still requires a POSIX host, and SQLite production migrations/distributed workers are absent.
