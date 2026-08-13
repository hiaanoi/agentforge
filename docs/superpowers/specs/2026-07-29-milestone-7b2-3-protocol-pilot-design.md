# Milestone 7-B2.3 Frozen Protocol and Durable Pilot Design

## Status

Approved for implementation on `feat/milestone-7b2.3-protocol`.

The B2.2 baseline is commit `e358c49` and tag
`milestone-7b2-baseline-complete`. B2.3 builds evaluation orchestration only.
It does not execute or publish a real-model benchmark.

## Goal

Turn the existing constrained repair primitives into one evaluator-owned,
reproducible, durable Pilot workflow. A caller supplies one immutable protocol
and one admitted formal Fixture; AgentForge owns workspace preparation,
protocol verification, Runtime construction, baseline execution, approvals,
hidden final verification, result persistence, replacement decisions, and
aggregate selection.

The external interface must remain small:

```python
campaign = pilot_runner.create_campaign(protocol)
result = await pilot_runner.run_campaign(campaign.campaign_id)
recovered = await pilot_runner.recover_campaign(campaign.campaign_id)
```

Callers must not assemble repositories, tools, coordinators, profiles, or
approval loops.

## Architecture Audit

### Existing strengths

- `AgentRuntime` already owns model/tool sequencing, checkpoints, durable
  approvals, mutation execution, test execution, and final verification.
- `RepairCoordinator` already enforces latest-source testing, full workspace
  diff validation, protected paths, and evaluator-triggered hidden tests.
- `BaselineExecutionCoordinator` already verifies the exact visible pytest
  failed-node set before the first model request.
- `FormalFixturePilotWorkspace` already separates the model workspace from
  reference fixes and hidden tests.
- `RepairEvaluationRun`, `TaskEvaluationSummary`, and deterministic report
  rendering already define the core result vocabulary.

### Blocking gaps

1. No immutable protocol object binds model configuration, ModelBudget,
   ContextPolicy, prompts, Tool schemas, task policy, Fixture assets, test
   profile templates, platform, repetitions, and replacement policy.
2. `EvaluationHarness` requires a fully assembled Runtime and cannot create a
   formal Pilot from a Fixture.
3. Default `ContextPolicy.system_instructions` describes a read-only agent and
   is unsuitable for repair execution.
4. Formal Fixture manifests have no frozen, leakage-reviewed user task prompt.
5. `RUN_CREATED` currently writes full task text into an Event, contrary to
   the evaluation protocol.
6. Repetition slots, attempt numbers, campaign state, and replacement
   decisions are not persisted.
7. `summarize_task` rejects duplicate repetition indexes but does not resolve
   replacement chains.
8. A process restart during evaluator orchestration has no campaign-level
   recovery fact.
9. There is no trusted OpenAI provider factory that derives runtime settings
   from the frozen protocol without persisting an API key.

## Non-Goals

- No real OpenAI repair run in B2.3.
- No CLI, FastAPI, frontend, MCP, LangGraph, shell, Git mutation, dependency
  installation, arbitrary environment, or container sandbox.
- No parallel campaign execution.
- No automatic re-execution of an attempt with an indeterminate side effect.
- No migration framework; B2.3 continues the existing fresh-database
  `create_all` development contract and records this debt.
- No public benchmark claim.

## Domain Model

### EvaluationProtocol

`EvaluationProtocol` is frozen and self-digesting. It contains no credential.

Required bindings:

- `schema_version`
- `protocol_name`
- `execution_mode`: `OFFLINE_TEST` or `REAL_MODEL`
- `task_id`
- `fixture_registry_digest` for the complete admitted Fixture batch
- `fixture_asset_digest` for the selected task tree
- `expected_baseline_fingerprint_digest`
- `task_policy_digest`
- `test_profile_template_digest`
- `provider_binding`
- `model_budget`
- `system_prompt`, `task_prompt`, their versions and digests
- `tool_schema_digest`
- `context_policy`
- `completion_correction_mode`
- `repetition_count`
- `replacement_policy`
- `platform_binding`
- computed `protocol_digest`

`ProviderBinding` uses explicit fields rather than a free-form dictionary:

- provider name
- exact model ID
- timeout
- retry count
- `store=false`
- maximum output tokens
- multi-tool response policy
- maximum function calls per response

`PlatformBinding` fixes:

- operating-system family
- Python implementation and version
- resolved executable path
- executable digest

The protocol validator rejects:

- an API key, token, secret, or arbitrary environment mapping;
- a REAL_MODEL protocol without explicit authorization;
- mismatched nested digests;
- unsupported providers or execution modes;
- zero repetitions;
- replacement categories outside the infrastructure allowlist.

The full prompt text may be stored in the protocol SQLite row. Events,
checkpoints, reports, and campaign audit rows contain only prompt digests.

### ReplacementPolicy

The only B2.3 automatic policy is `INFRASTRUCTURE_ONLY`.

It fixes:

- maximum replacement attempts per repetition;
- an explicit allowlist of replaceable failure categories;
- whether provider transport/timeout failures are replaceable.

Normal model failures, test failures, policy blocks, loop detection, budget
exhaustion, and hidden verification failures are never replaced.

`INDETERMINATE`, a claimed mutation without a terminal execution fact, or a
started test process without confirmed tree termination is never
automatically replaced.

### EvaluationCampaign

A campaign binds exactly one protocol digest and contains a fixed number of
repetition slots.

Campaign states:

```text
CREATED -> RUNNING -> COMPLETED
                   -> COMPLETED_WITH_INVALID
                   -> BLOCKED
                   -> INDETERMINATE
```

The slot count is immutable after creation. Campaign transitions use a
versioned conditional update.

### EvaluationSlot

Each slot represents one predeclared repetition index.

```text
PENDING -> CLAIMED -> RUNNING -> ACCEPTED
                              -> REPLACEMENT_PENDING
                              -> INVALID
                              -> INDETERMINATE
```

Only a successful conditional claim may prepare a workspace. A slot records
the selected terminal attempt and selected `evaluation_run_id`.

### PilotAttempt

An attempt is a durable orchestration fact, separate from
`RepairEvaluationRun`.

It records:

- campaign, slot, protocol, task, attempt number;
- replacement predecessor IDs;
- workspace lease ID and safe workspace-root digest;
- Runtime `run_id`;
- baseline and evaluation result IDs;
- state, failure category, infrastructure-failure flag;
- version and timestamps.

The actual temporary workspace path is stored only in the sensitive SQLite
attempt row, never in an Event or report.

Attempt states:

```text
CREATED -> WORKSPACE_READY -> RUNTIME_READY -> RUNNING -> COMPLETED
                                                  |----> INVALID
                                                  |----> INDETERMINATE
```

## Formal Task Prompt

Every admitted formal Fixture manifest gains a `repair_prompt` object:

- `title`
- `description`
- bounded `success_conditions`
- prompt schema version

The description exposes only the user-visible symptom and permitted success
criteria. It must not expose the reference fix, root-cause category, hidden
assertions, or evaluator-only crash/concurrency matrix.

`FormalFixtureLoader` validates this object. The task prompt is constructed
deterministically from the manifest and the normalized RepairTaskPolicy.

## Fixture and Profile Binding

Before campaign creation:

1. Load and hash the complete formal Fixture tree.
2. Verify the global admitted Fixture registry digest and the selected task
   asset digest independently.
3. Validate every declared immutable evaluator asset.
4. Derive the RepairTaskPolicy from exact editable paths, protected model
   paths, difficulty, and fixed budget.
5. Build deterministic visible and hidden TestProfile templates.
6. Hash the executable, argv templates, environment template, timeout, output
   limit, and profile version.
7. Build the exact Tool registry and hash exported schemas.
8. Build the exact repair prompts and ContextPolicy.
9. Refuse protocol registration if any derived digest differs.

Per-run concrete TestPlan digests may differ because temporary absolute paths
differ. The protocol binds the path-independent profile template; each Run
still persists and revalidates the concrete M6 TestPlan.

## Durable Workspace Lease

`PilotWorkspaceManager` creates a unique evaluator-owned directory under the
system temporary root:

```text
agentforge-pilots/<campaign-id>/<slot>/<attempt>/
    lease.json
    model_workspace/
    evaluator_hidden/tests/hidden/
```

The lease file is outside the model workspace. It binds campaign, slot,
attempt, protocol digest, Fixture digest, and a random nonce.

Preparation rules:

- copy only the buggy workspace and visible tests into `model_workspace`;
- copy hidden tests into `evaluator_hidden`;
- never copy reference files;
- reject symlinks, reparse points, unsupported file types, and digest drift;
- use create-exclusive lease creation;
- reopen only when every lease binding matches;
- clean up only a terminal attempt owned by the matching lease.

A CREATED or WORKSPACE_READY orphan can be invalidated and replaced safely. A
RUNNING orphan is inspected for active mutation/test facts before any
replacement decision.

## Runtime Assembly

`PilotRuntimeFactory` is an internal deep module. Given a verified protocol,
formal manifest, attempt, and workspace lease, it builds:

- resolver and sensitive-file policy;
- administrator-only visible/hidden TestProfile registry;
- READ tools: `list_files`, `read_file`, `search_text`;
- WRITE tools: `edit_file`, and `write_file` only when the task permits file
  creation or hash-bound replacement;
- managed `run_tests`;
- Tool registry and schema revalidation;
- PolicyEngine and RepairPolicyEnforcer;
- mutation and test execution coordinators;
- ModelWorkflow, ModelExecutor, frozen ModelBudget, and provider;
- ContextBuilder with the frozen repair system prompt;
- RepairCoordinator and diff validator;
- BaselineExecutionCoordinator;
- EvaluationHarness.

The provider factory is trusted startup code. It receives an
`EvaluationProtocol`, obtains credentials externally, and must return a
provider bound to the same provider name, exact model ID, and safe
configuration digest. OpenAI construction never persists the API key.

`RUN_CREATED` audit payloads become digest-only. The full task prompt remains
available to the Runtime but is not emitted to Events.

## PilotRunner Flow

For each slot, sequentially:

1. CAS-claim the slot.
2. Create the attempt and durable workspace lease.
3. Revalidate Fixture, protocol, executable, prompts, policy, profiles, and
   Tool schemas.
4. Build and persist the workspace baseline.
5. Create the Runtime Run and RepairState.
6. Persist the attempt-to-Run binding.
7. Create the evaluator-owned baseline execution.
8. Mark attempt and slot RUNNING.
9. Execute `EvaluationHarness`.
10. Persist the immutable `RepairEvaluationRun`.
11. Finish the attempt from the real result.
12. Accept the slot for all valid model outcomes, including unsuccessful
    repairs.
13. For a replaceable infrastructure failure, create a fresh attempt and link
    both the attempt and evaluation-run predecessor.
14. Fail closed for an indeterminate side effect or exhausted replacement
    allowance.
15. Clean terminal workspace leases according to retention policy.

The model cannot select the repetition index, protocol, workspace, profiles,
environment, replacement decision, baseline, or hidden verification.

## Recovery

`recover_campaign` first reconciles persisted facts:

- terminal attempts are reused;
- a persisted `RepairEvaluationRun` finalizes an unfinished attempt
  idempotently;
- pre-Run preparation orphans become INVALID and may be replaced;
- a Run with a claimed/started mutation or test lacking a terminal fact makes
  the attempt and campaign INDETERMINATE;
- a nonterminal orphan with no possible side effect becomes an
  infrastructure-invalid attempt and follows the frozen replacement policy;
- no attempt restarts from the original Fixture under the same attempt ID;
- no replacement reuses a prior workspace;
- terminal accepted slots never execute again.

This is campaign-level fail-closed recovery. It does not weaken M3/M5/M6
side-effect recovery rules and does not pretend that an arbitrary interrupted
model request can always resume.

## Replacement Selection and Metrics

`resolve_effective_runs` validates all replacement chains before aggregation:

- same task, protocol digest, and repetition index;
- predecessor exists and is infrastructure-invalid;
- no cycles or branches;
- strictly increasing attempt numbers;
- at most one terminal selected run per repetition;
- every predeclared repetition has one selected valid run before a complete
  summary is produced.

Superseded infrastructure-invalid runs remain queryable but are excluded from
model-quality denominators. Reports include invalid/replacement counts and
safe failure categories, never full prompts, output, source, diffs, hidden
test content, credentials, environment mappings, or local paths.

## Audit Events

Add campaign audit facts:

- `EVALUATION_PROTOCOL_REGISTERED`
- `EVALUATION_CAMPAIGN_CREATED`
- `EVALUATION_CAMPAIGN_STARTED`
- `EVALUATION_SLOT_CLAIMED`
- `EVALUATION_ATTEMPT_STARTED`
- `EVALUATION_ATTEMPT_COMPLETED`
- `EVALUATION_ATTEMPT_INVALID`
- `EVALUATION_ATTEMPT_INDETERMINATE`
- `EVALUATION_REPLACEMENT_CREATED`
- `EVALUATION_SLOT_ACCEPTED`
- `EVALUATION_CAMPAIGN_COMPLETED`

Campaign audit payloads contain IDs, digests, versions, counts, safe status,
safe failure category, and bounded durations only.

## Concurrency

- One campaign runner is active per process.
- Campaign and slot ownership use SQLite conditional updates.
- Tool execution and model requests never hold a database transaction.
- A second caller observing an active claim returns a stable conflict.
- Stale claims are not stolen automatically. Recovery must classify the
  persisted attempt first.
- Slots execute sequentially in B2.3 to keep rate limits and attribution
  deterministic.

## Test Strategy

### Protocol

- deterministic digest under equivalent normalized input;
- reject secret fields, drifted digests, unauthorized REAL_MODEL mode, and
  unsupported replacement categories;
- round-trip immutable SQLite registration and conflict detection;
- verify exact provider, prompt, policy, profile, Fixture, executable, and
  Tool-schema bindings.

### Workspace

- fresh unique workspace for every attempt;
- no reference or hidden files in model workspace;
- hidden profile targets only evaluator-owned external tests;
- lease reopen is idempotent;
- mismatched/tampered lease fails closed;
- symlink/reparse tests remain platform-gated.

### Runtime assembly

- repair ContextPolicy replaces the read-only default;
- registry contains only approved tools;
- full task text is absent from Events;
- profile environment remains a complete allowlist;
- model cannot choose hidden profile;
- all protocol digests are revalidated before the first model request.

### Campaign and replacement

- fixed slots created once;
- concurrent claim has one winner;
- repeated run/recovery is idempotent;
- normal model failure is accepted and not replaced;
- allowlisted infrastructure failure creates exactly one fresh replacement;
- exhausted replacement allowance closes invalid;
- indeterminate mutation/test never replaces;
- replacement chains reject cycles, branches, cross-task links, and duplicate
  selected runs;
- metrics exclude superseded infrastructure-invalid attempts.

### End-to-end

- run a complete formal Fixture through baseline, Mock provider repair,
  development test, diff validation, hidden final verification, persistence,
  and aggregation;
- unexpected baseline pass makes zero model requests;
- restart after campaign/slot/attempt transitions preserves isolation and
  does not duplicate side effects;
- hidden output, prompt text, fixture source, environment, and local absolute
  paths do not appear in Events or reports;
- all M0-M7-B2.2 tests continue to pass.

## Documentation and Acceptance

B2.3 is PASS only when:

- the complete offline Pilot path is owned by `PilotRunner`;
- a formal Fixture E2E reaches a durable selected result;
- protocol and replacement semantics are persisted and tested;
- recovery tests cover every orchestration state;
- no real-model result is claimed;
- full pytest, Ruff, strict mypy, compileall, Fixture verification, preflight
  validation, and `git diff --check` pass.

Remaining after B2.3:

- create and approve an exact REAL_MODEL protocol instance;
- execute the three-run main Demo Pilot;
- classify live Provider failures under the frozen replacement policy;
- publish B2.4 results only after all three selected repetitions are valid.
