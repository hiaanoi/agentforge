# Milestone 7-B2.4 Test Plan

## Purpose

This plan verifies the Real-Model Portfolio Pilot without making network access part of the
default test suite. Offline tests use fake or deterministic Providers. Real OpenAI execution is a
separate, explicit acceptance phase whose result may be a model-quality failure.

## Test Layers

1. Unit tests for immutable models, digests, classification, telemetry, and report redaction.
2. Persistence tests for Study, authorization, telemetry, and conditional state transitions.
3. Integration tests for four-Campaign orchestration, replacement, restart, and terminal reuse.
4. Security tests for secrets, environment isolation, hidden assets, and public artifacts.
5. Existing complete regression suite.
6. Opt-in Provider smoke preflight.
7. Explicit formal 12-slot real-model Study.

## Offline Network Rule

The normal `pytest` invocation must continue to block or avoid external network access. Tests under
`tests/live/` remain skipped unless the exact opt-in variable, API key, and model ID are present.
No offline test may instantiate a real `AsyncOpenAI` client without an injected fake transport.

## Unit Test Matrix

### Study definition and digest

File: `tests/unit/test_evaluation_study_models.py`

1. The same normalized task/protocol order produces the same definition digest.
2. Changing task order changes the digest.
3. Changing any Protocol digest changes the Study digest.
4. Duplicate task IDs are rejected.
5. Duplicate Protocol digests are rejected.
6. Fewer or more than four B2.4 task IDs are rejected by the B2.4 builder.
7. Repetition count other than three is rejected by the B2.4 builder.
8. Planned slots must equal task count times repetitions.
9. Mixed model IDs are rejected.
10. Mixed Provider configuration digests are rejected.
11. Mixed platform bindings are rejected.
12. Dirty worktree provenance is rejected.
13. Drifted source, `pyproject.toml`, or `uv.lock` digest is rejected.
14. Unknown fields, secret fields, and environment maps are rejected.

### Authorization

File: `tests/unit/test_real_model_authorization.py`

1. Authorization binds the exact Study digest and ordered Protocol digests.
2. Authorization with a missing Protocol is rejected.
3. Authorization with an extra Protocol is rejected.
4. Authorization with a different model ID is rejected.
5. Authorization with more than four Campaigns or 12 planned slots is rejected.
6. Authorization with more than one replacement per slot is rejected.
7. `RUN_REAL_MODEL_PILOT` missing or not equal to `1` fails closed.
8. Missing API key fails closed without printing its variable value.
9. Incorrect operator confirmation digest fails closed.
10. Correct confirmation passes without serializing the API key.
11. Authorization cannot be reused for a different Study.
12. Authorization is immutable after persistence.

### Outcome classification

File: `tests/unit/test_evaluation_outcome_classification.py`

1. `VERIFIED_SUCCESS` is `SCORED`.
2. Visible test failure is `SCORED`.
3. Hidden final verification failure is `SCORED`.
4. Budget exhaustion is `SCORED`.
5. Loop detection is `SCORED`.
6. Policy or diff violation is `SCORED`.
7. Exhausted `MODEL_PROTOCOL_ERROR` is `SCORED`.
8. Invalid structured model output is `SCORED`.
9. Rate limit, timeout, transport, and retryable Provider error are
   `INFRASTRUCTURE_INVALID`.
10. Authentication and bad request are configuration aborts.
11. Approval, mutation, process, or baseline uncertainty is `INDETERMINATE`.
12. A scored failure cannot carry `infrastructure_failure=true`.
13. A verified success cannot be infrastructure-invalid or indeterminate.
14. An indeterminate result cannot be selected or replaced.

### Model-attempt facts and telemetry

Files:

- `tests/unit/test_model_attempt_queries.py`
- `tests/unit/test_evaluation_telemetry.py`

Cases:

1. Physical attempts are returned in logical-call and attempt-number order.
2. Successful attempt usage and duration are preserved.
3. Failed attempt code, retryability, and elapsed duration are preserved.
4. Retry count equals physical attempts minus logical calls where applicable.
5. Input, output, total, cached-input, and reasoning tokens aggregate correctly.
6. Provider deviation and normalization Event counts aggregate correctly.
7. Returned and discarded function-call counts aggregate correctly.
8. Tool, approval, mutation, test, context-compaction, correction, and policy counts aggregate
   correctly.
9. Telemetry digest is deterministic.
10. Re-collecting unchanged durable facts returns the same telemetry.
11. Conflicting telemetry for the same evaluation run raises an identity conflict.
12. Telemetry excludes Tool arguments, Tool output, model text, prompts, file paths, and Provider
    request IDs.

### Pricing and cost

File: `tests/unit/test_evaluation_costs.py`

1. Pricing digest is deterministic.
2. Cached tokens are subtracted from uncached input tokens.
3. Output tokens are priced exactly once.
4. Reasoning tokens are reported but not double-billed.
5. Decimal arithmetic produces stable serialized values.
6. Missing usage produces `cost_complete=false`.
7. Missing cached-input rate when cached usage is non-zero produces
   `cost_complete=false`.
8. Model ID mismatch between pricing and Study is rejected.
9. Negative rates and unsupported currencies are rejected.

### Reporting

File: `tests/unit/test_evaluation_study_reports.py`

1. Planned, score-eligible, successful, and infrastructure-invalid denominators are all present.
2. Micro success and planned-slot success use different correct denominators.
3. Per-task majority, stable, and any-success flags are correct.
4. A 0/12 completed Study renders a valid report.
5. A 12/12 completed Study renders a valid report.
6. Mixed model-quality failures render a complete failure distribution.
7. Replaced infrastructure attempts remain visible in raw counts but not model-quality results.
8. Incomplete usage marks cost incomplete.
9. Public JSON ordering and digests are deterministic.
10. Markdown and JSON agree on all counts.
11. Reports never contain prompt text, source, test output, Tool arguments, diff text, API key,
    Provider request ID, or absolute workspace path.
12. Unknown telemetry fields fail closed instead of being automatically published.

## Persistence Test Matrix

### Study repository

File: `tests/unit/test_evaluation_study_persistence.py`

1. Registering the same immutable definition is idempotent.
2. The same Study ID with different content conflicts.
3. Study creation atomically binds all four Campaign IDs.
4. Missing or duplicate Campaign binding is rejected.
5. `DRAFT -> AUTHORIZED` uses a conditional version update.
6. `AUTHORIZED -> RUNNING` uses a conditional version update.
7. Only one caller can claim Study execution.
8. Terminal state changes are rejected.
9. A completed Study reloads after database restart.
10. Study Events are ordered and contain only IDs, digests, states, and counts.

### Telemetry repository

File: `tests/unit/test_evaluation_telemetry_persistence.py`

1. Telemetry is one-to-one with an evaluation run.
2. Cross-run, cross-attempt, cross-Protocol, and cross-Campaign bindings are rejected.
3. Exact duplicate save is idempotent.
4. Same ID with different metrics conflicts.
5. Telemetry survives database reopen.
6. A result can be found before telemetry and reconciled later.

## Integration Test Matrix

### Study builder and four Protocols

File: `tests/integration/test_real_model_study_builder.py`

Use a fake OpenAI-compatible Provider builder and the real four formal manifests.

1. Builder emits four REAL_MODEL Protocols in fixed task order.
2. Each Protocol carries three repetitions and exact Fixture/task/profile/policy digests.
3. BASIC and ENGINEERING physical/token budgets match the design.
4. All Protocols share one Provider model/configuration.
5. Reference overlays and hidden-test roots do not enter prompts or model requests.
6. Provider secret does not enter any serialized object.

### Complete 12-slot scored Study

File: `tests/integration/test_real_model_study_runner.py`

Use scripted Providers with task-specific responses, but use the real Runtime, SQLite, approval,
mutation, visible tests, hidden verification, and Study Runner.

1. Four Campaigns and 12 slots are created before the first model request.
2. Campaigns execute sequentially in fixed order.
3. Every repetition receives a fresh workspace and unique Run.
4. All 12 successful slots produce `COMPLETED`.
5. Mixed success/model-quality failures still produce `COMPLETED` when all 12 slots are scored.
6. Model protocol failure consumes and selects its slot without replacement.
7. Hidden-test failure consumes and selects its slot without replacement.
8. Budget exhaustion consumes and selects its slot without replacement.
9. Telemetry exists for every selected run.
10. Public report counts all 12 slots correctly.

### Infrastructure replacement

File: `tests/integration/test_real_model_study_replacement.py`

1. One initial timeout and one successful replacement select only the replacement result.
2. Both attempts remain present in raw attempt metrics.
3. Two consecutive timeouts exhaust the one-replacement allowance.
4. A slot with exhausted infrastructure failures remains unscored.
5. The Study becomes `COMPLETED_WITH_INFRASTRUCTURE_GAPS`.
6. An auth error aborts the current Campaign before its remaining repetitions.
7. An auth error aborts before remaining Campaigns start.
8. A bad request aborts the current Campaign and Study in the same way.
9. Model-quality failure never consumes replacement allowance.

### Crash recovery

File: `tests/integration/test_real_model_study_recovery.py`

1. Restart after Study authorization but before execution starts.
2. Restart between completed Campaigns.
3. Restart after Campaign creation but before first slot claim.
4. Restart after result persistence but before Attempt finalization.
5. Restart after Attempt finalization but before telemetry persistence.
6. Restart after telemetry persistence but before Campaign report persistence.
7. Restart after all Campaigns complete but before Study report persistence.
8. Terminal Study re-execution performs zero Provider and Tool calls.
9. Provider request interrupted before any local side effect follows the existing
   infrastructure-recovery policy.
10. Approval `CLAIMED` without execution fact makes the Study indeterminate.
11. Mutation `WRITING` makes the Study indeterminate.
12. Process `STARTED` makes the Study indeterminate.
13. Baseline `STARTED` makes the Study indeterminate.
14. Indeterminate Study retains its investigative workspace.

## Security Test Matrix

File: `tests/security/test_real_model_study_security.py`

1. `OPENAI_API_KEY` is absent from all SQLite text/JSON fields.
2. API key is absent from Events, checkpoints, exceptions, reprs, JSON, and Markdown.
3. API key and other host secrets are absent from TestProfile environment.
4. Formal model workspaces contain no reference overlay.
5. Formal model workspaces contain no hidden tests.
6. Only the OpenAI Adapter performs network access.
7. No Tool can perform network, shell, dependency installation, or Git mutation.
8. Public artifact scanning rejects absolute paths from Windows and POSIX.
9. Public artifact scanning rejects hidden-test identifiers and known reference markers.
10. Public artifact scanning rejects prompt substrings and source snippets.
11. Unknown report fields fail closed.
12. Dirty worktree or changed `uv.lock` prevents authorization.

## Offline Acceptance Commands

Run in this order:

```powershell
uv sync --frozen
uv run --frozen pytest `
  tests/unit/test_evaluation_study_models.py `
  tests/unit/test_real_model_authorization.py `
  tests/unit/test_evaluation_outcome_classification.py `
  tests/unit/test_model_attempt_queries.py `
  tests/unit/test_evaluation_telemetry.py `
  tests/unit/test_evaluation_costs.py `
  tests/unit/test_evaluation_study_reports.py `
  tests/unit/test_evaluation_study_persistence.py `
  tests/unit/test_evaluation_telemetry_persistence.py `
  -ra
```

```powershell
uv run --frozen pytest `
  tests/integration/test_real_model_study_builder.py `
  tests/integration/test_real_model_study_runner.py `
  tests/integration/test_real_model_study_replacement.py `
  tests/integration/test_real_model_study_recovery.py `
  tests/integration/test_real_model_pilot_entrypoint.py `
  tests/security/test_real_model_study_security.py `
  -ra
```

```powershell
uv run --frozen python evaluation/fixtures/verify_fixtures.py `
  --repeat 3 `
  --output evaluation/fixtures/verification_report.json
```

```powershell
uv run --frozen pytest -ra
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen python -m compileall src
git diff --check
```

Expected offline properties:

- no failed test;
- the existing opt-in/platform skips remain explained;
- no new default network skip is used to hide an unimplemented path;
- no API key is required;
- no `uv.lock` change.

Exact total test counts must be recorded from the final run rather than predicted in advance.

## Real Provider Smoke Preflight

The smoke test uses only a generated temporary, read-only repository:

```powershell
$env:RUN_LIVE_TESTS = "1"
$env:OPENAI_API_KEY = "<set-locally>"
$env:AGENTFORGE_OPENAI_MODEL = "<exact-account-accessible-model-id>"
uv run --env-file .env --frozen pytest `
  tests/live/test_openai_live.py `
  -ra -s
```

Required smoke evidence:

- authentication succeeds;
- requested and returned model identities are recorded, and the returned identity satisfies the
  frozen binding;
- at least one Tool call and final response complete;
- completed-request and all-physical-request usage completeness are recorded separately;
- usage is reported or explicitly marked unavailable;
- no unsafe Tool is exposed;
- multi-tool deviation events remain sanitized;
- no formal Fixture is accessed.
- the single resolved response model ID is printed; alias discovery accepts only the alias itself
  or its strict `alias-YYYY-MM-DD` Snapshot.

Actual smoke evidence recorded on 2026-07-30:

- `1 passed` in `24.30s`, requested and returned model `gpt-5.6-luna`;
- 4 model requests, 3 tool calls, within the configured 5/5 limits;
- 3 discarded calls from a maximum returned call count of 3, with normalization and deviation
  events present;
- final answer and both temporary fixture path citations present;
- completed-request and all-physical-request usage complete.

The post-fix alias-resolution smoke used `gpt-5.4-mini` and passed in `31.25s`. The Provider
returned the single exact response ID `gpt-5.4-mini-2026-03-17`; it completed 4 model requests and
3 tool calls, discarded 3 calls, emitted normalization and deviation events, completed the final
answer, cited the temporary fixture paths, and reported complete request usage.

This is smoke evidence only. It does not authorize or replace the formal 12-slot Study.

## Formal Study Commands

The implementation provides a non-product evaluator script with separate preparation and execution
steps:

```powershell
uv run --env-file .env --frozen python evaluation/run_real_model_pilot.py prepare `
  --state-dir .agentforge/pilots/m7-b2.4 `
  --model "<requested-model-alias>" `
  --response-model "<exact-model-id-from-smoke>" `
  --pricing-date "<YYYY-MM-DD>" `
  --pricing-source "<reviewed-pricing-url>" `
  --input-per-million "<decimal-rate>" `
  --cached-input-per-million "<decimal-rate>" `
  --output-per-million "<decimal-rate>"
```

The operator reviews the generated safe manifest and exact Study digest. Execution then requires:

For the lowest-cost real-model validation, use the bounded Canary first. It authorizes the same
gate, selects only the first Campaign's next unfinished Slot, and leaves durable state available
for recovery. It is not a complete Study result:

```powershell
$env:RUN_REAL_MODEL_PILOT = "1"
uv run --env-file .env --frozen python evaluation/run_real_model_pilot.py canary `
  --state-dir .agentforge/pilots/m7-b2.4-canary `
  --confirm "<exact-study-definition-digest>"
```

Only after the Canary is acceptable should the operator run the formal Study:

```powershell
$env:RUN_REAL_MODEL_PILOT = "1"
uv run --env-file .env --frozen python evaluation/run_real_model_pilot.py run `
  --state-dir .agentforge/pilots/m7-b2.4 `
  --confirm "<exact-study-definition-digest>"
```

Recovery uses:

```powershell
uv run --env-file .env --frozen python evaluation/run_real_model_pilot.py recover `
  --state-dir .agentforge/pilots/m7-b2.4 `
  --confirm "<exact-study-definition-digest>"
```

Report regeneration performs no model call:

```powershell
uv run --frozen python evaluation/run_real_model_pilot.py report `
  --state-dir .agentforge/pilots/m7-b2.4 `
  --output-dir evaluation/reports/m7-b2.4
```

`prepare` is offline and does not require an API key. `run` and `recover` require
`RUN_REAL_MODEL_PILOT=1`, `OPENAI_API_KEY`, and the exact confirmation digest. `report` reads only
durable local facts, requires no API key, and performs zero Provider calls.

## Real-Model Acceptance Matrix

For each of the 12 planned slots, record:

- task ID and repetition index;
- Protocol, Campaign, Slot, Attempt, Run, and evaluation-result IDs;
- score eligibility;
- final status and failure category;
- infrastructure replacement count;
- model logical calls, physical requests, and retries;
- READ, edit, and development-test counts;
- hidden verification outcome;
- input/output/total/cached/reasoning tokens;
- model latency and total wall time;
- Provider deviations and discarded function calls;
- estimated cost completeness and amount;
- final workspace and diff digests.

Do not require a minimum repair success rate for execution correctness. Require all model-quality
outcomes to remain visible and all infrastructure exclusions to be separately justified.

## Final Report Checklist

- [ ] Exact model ID and Provider settings are present.
- [ ] Requested model alias and frozen response model ID are both present and reconcile.
- [ ] Four Protocol digests and Study digest are present.
- [ ] Git commit, source, dependency, Fixture, Python, and platform bindings are present.
- [ ] Planned, scored, successful, invalid, and replacement counts reconcile.
- [ ] All 12 planned slots are represented, including pending, invalid, indeterminate, and
  unexecuted slots.
- [ ] Model-quality failures remain in the denominator.
- [ ] Infrastructure replacements remain visible.
- [ ] Token and cost completeness are explicit.
- [ ] Generated artifacts contain no private run, Campaign, Attempt, or evaluation UUIDs; SQLite
  remains the private execution fact source.
- [ ] No hidden-test detail is disclosed.
- [ ] No prompt, source, output, diff, secret, Provider request ID, or absolute path is disclosed.
- [ ] The report says this is a small Portfolio Pilot and not an official benchmark.
