# Milestone 7-B2.4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this
> plan task by task. Track every checkbox and keep commits scoped to one task.

**Goal:** Execute and publish a durable, explicitly authorized, four-task x three-repetition
real-model Portfolio Pilot without weakening AgentForge safety or hiding model-quality failures.

**Architecture:** Reuse the existing per-task `EvaluationProtocol`, `EvaluationCampaign`,
`PilotRunner`, and Runtime execution path. Add a thin persisted Study layer above four Campaigns,
typed outcome classification, immutable run telemetry, explicit real-model authorization, and a
deny-by-default public report. All normal tests remain offline.

**Tech Stack:** Python 3.11+ domain target, Python 3.14.3 acceptance environment, Pydantic v2,
SQLAlchemy 2, SQLite, OpenAI Responses API, pytest, Ruff, strict mypy, `uv`.

## Current Status

- Tasks 0-15: complete. Offline implementation and regression acceptance passed.
- Task 16: complete. Read-only OpenAI smoke passed with the exact returned model identity, complete
  usage, bounded requests/tools, and sanitized multi-tool normalization evidence.
- Task 17: pending clean committed source, operator review, and formal Study authorization.
- Task 18: offline truth-boundary documentation complete; real-result closure remains pending.
- Focused B2.4 evidence: 131 passed (99 unit/persistence and 32 integration/security).
- Full offline evidence: 636 collected, 627 passed, 9 skipped, 0 failed; 4 fixtures x 3 repetitions
  verified; Ruff, strict mypy for 117 source files, compileall, `git diff --check`, and unchanged
  `uv.lock` passed. Pytest emitted one existing collection warning.
- Smoke evidence: `gpt-5.6-luna` and the post-fix `gpt-5.4-mini` alias-resolution run both completed
  with 4 model requests, 3 tool calls, 3 discarded calls, complete usage, and a final answer.
  The mini run resolved to `gpt-5.4-mini-2026-03-17` in 31.25 seconds. Formal 12-slot Study remains
  unexecuted.
- Provider identity hardening separates the requested alias from the exact response Snapshot. The
  relation is validated during preparation and exact response equality is enforced during formal
  execution.

The unchecked boxes below preserve the original execution checklist. This status block is the
authoritative progress record until the real Provider phases are executed.

---

## File Map

### New source files

- `src/agentforge/evaluation/outcomes.py`
  - Classifies terminal Repair facts as scored, infrastructure-invalid, configuration abort, or
    indeterminate.
- `src/agentforge/evaluation/telemetry_models.py`
  - Immutable sanitized run telemetry and pricing models.
- `src/agentforge/evaluation/telemetry.py`
  - Collects telemetry from Model attempts, Events, approvals, mutations, tests, and evaluation
    results.
- `src/agentforge/evaluation/telemetry_persistence.py`
  - One-to-one immutable telemetry Repository.
- `src/agentforge/evaluation/costs.py`
  - Decimal cost estimation from a frozen pricing snapshot.
- `src/agentforge/evaluation/study_models.py`
  - Study definition, authorization, runtime state, Campaign binding, and aggregate summary.
- `src/agentforge/evaluation/study_persistence.py`
  - Study Repository, CAS state transitions, Campaign bindings, and ordered Study Events.
- `src/agentforge/evaluation/study_builder.py`
  - Builds four real-model Protocols and one immutable Study definition from admitted Fixtures.
- `src/agentforge/evaluation/study_runner.py`
  - Sequentially creates/runs/recovers four existing Campaigns.
- `src/agentforge/evaluation/study_reports.py`
  - Private aggregate model and public allowlisted JSON/Markdown renderers.
- `src/agentforge/evaluation/real_model_gate.py`
  - Validates environment opt-in, secret presence, authorization, confirmation digest, and source
    drift.
- `src/agentforge/evaluation/source_provenance.py`
  - Collects clean Git/source/dependency digests using fixed read-only commands.
- `src/agentforge/evaluation/environment.py`
  - Builds the fixed TestProfile environment without inheriting host secrets.
- `src/agentforge/evaluation/public_artifacts.py`
  - Forbidden-content scanner for publishable artifacts.
- `evaluation/run_real_model_pilot.py`
  - Evaluator-only `prepare`, `run`, `recover`, and `report` entry point.

### Modified source files

- `src/agentforge/domain/enums.py`
  - Add Study and outcome enums plus Study Event types.
- `src/agentforge/models/domain.py`
  - Add a sanitized persisted `ModelAttemptRecord`.
- `src/agentforge/persistence/model_workflow.py`
  - Add ordered read access to physical model-attempt facts.
- `src/agentforge/persistence/tables.py`
  - Add Study, Study-Campaign, Study-Event, and telemetry tables; add outcome class to evaluation
    results.
- `src/agentforge/evaluation/models.py`
  - Add outcome class and validation to `RepairEvaluationRun`; add richer task summary fields.
- `src/agentforge/evaluation/harness.py`
  - Classify outcomes and persist telemetry after durable result creation.
- `src/agentforge/evaluation/pilot_runner.py`
  - Select scored failures, replace only infrastructure failures, and stop on indeterminate facts.
- `src/agentforge/evaluation/metrics.py`
  - Use explicit planned/scored/success denominators and telemetry aggregates.
- `src/agentforge/evaluation/reports.py`
  - Preserve existing Campaign reports while adding outcome/telemetry-safe fields.
- `src/agentforge/evaluation/provider_factory.py`
  - Require a validated real-model execution gate for the real Provider.
- `src/agentforge/evaluation/protocol.py`
  - Validate the physical request budget against task-level Study construction and preserve
    explicit real-model authorization.
- `.gitignore`
  - Ignore private `.agentforge/` Study databases, workspaces, and raw artifacts.
- `README.md`
  - Record B2.4 only after real execution; do not preclaim success.
- `docs/repair_evaluation_protocol.md`
  - Add real-model Study semantics and outcome taxonomy.
- `docs/implementation_plan.md`
  - Mark B2.4 planned, then update final status after acceptance.

### New tests

- `tests/unit/test_evaluation_outcome_classification.py`
- `tests/unit/test_model_attempt_queries.py`
- `tests/unit/test_evaluation_telemetry.py`
- `tests/unit/test_evaluation_telemetry_persistence.py`
- `tests/unit/test_evaluation_costs.py`
- `tests/unit/test_evaluation_study_models.py`
- `tests/unit/test_real_model_authorization.py`
- `tests/unit/test_evaluation_study_persistence.py`
- `tests/unit/test_evaluation_study_reports.py`
- `tests/integration/test_real_model_study_builder.py`
- `tests/integration/test_real_model_study_runner.py`
- `tests/integration/test_real_model_study_replacement.py`
- `tests/integration/test_real_model_study_recovery.py`
- `tests/security/test_real_model_study_security.py`

## Task 0: Protect The Existing Baseline

**Files:** No source changes.

- [ ] Record `git status --short`, branch, HEAD, and tags.
- [ ] Confirm the worktree contains only approved planning documents before implementation.
- [ ] Run the B2.3 focused suite:

```powershell
uv run --frozen pytest `
  tests/unit/test_evaluation_protocol.py `
  tests/unit/test_evaluation_protocol_persistence.py `
  tests/unit/test_evaluation_campaign_persistence.py `
  tests/unit/test_evaluation_provider_factory.py `
  tests/unit/test_evaluation_selection.py `
  tests/integration/test_pilot_runtime_factory.py `
  tests/integration/test_pilot_campaign_runner.py `
  tests/integration/test_pilot_campaign_recovery.py `
  tests/integration/test_formal_pilot_campaign_e2e.py `
  -ra
```

Expected: all focused tests pass with no network access.

- [ ] Run the complete suite and record the exact baseline count.
- [ ] Create a dedicated B2.4 feature branch only after the baseline is clean.
- [ ] Do not modify dependencies or `uv.lock`.

## Task 1: Add Explicit Evaluation Outcome Classes

**Files:**

- Create: `src/agentforge/evaluation/outcomes.py`
- Modify: `src/agentforge/domain/enums.py`
- Modify: `src/agentforge/evaluation/models.py`
- Test: `tests/unit/test_evaluation_outcome_classification.py`

- [ ] Write failing tests for every scored, infrastructure, configuration, and indeterminate
  category listed in the design.
- [ ] Add these enums:

```python
class EvaluationOutcomeClass(StrEnum):
    SCORED = "SCORED"
    INFRASTRUCTURE_INVALID = "INFRASTRUCTURE_INVALID"
    INDETERMINATE = "INDETERMINATE"


class EvaluationFailureClass(StrEnum):
    NONE = "NONE"
    MODEL_QUALITY = "MODEL_QUALITY"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    CONFIGURATION = "CONFIGURATION"
    SAFETY_INDETERMINATE = "SAFETY_INDETERMINATE"


class EvaluationOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome_class: EvaluationOutcomeClass
    failure_class: EvaluationFailureClass
    infrastructure_failure: bool
    replaceable: bool
    abort_study: bool
```

- [ ] Implement one pure classifier:

```python
_REPLACEABLE_INFRASTRUCTURE = {
    RepairTerminationReason.MODEL_RATE_LIMITED,
    RepairTerminationReason.MODEL_TIMEOUT,
    RepairTerminationReason.MODEL_TRANSPORT_ERROR,
    RepairTerminationReason.MODEL_PROVIDER_ERROR,
}
_CONFIGURATION_FAILURES = {
    RepairTerminationReason.MODEL_AUTH_ERROR,
    RepairTerminationReason.MODEL_BAD_REQUEST,
}


def classify_evaluation_outcome(
    *,
    status: RepairCompletionStatus,
    failure_reason: RepairTerminationReason | None,
) -> EvaluationOutcome:
    if status is RepairCompletionStatus.VERIFIED_SUCCESS:
        return EvaluationOutcome(
            outcome_class=EvaluationOutcomeClass.SCORED,
            failure_class=EvaluationFailureClass.NONE,
            infrastructure_failure=False,
            replaceable=False,
            abort_study=False,
        )
    if status is RepairCompletionStatus.INDETERMINATE:
        return EvaluationOutcome(
            outcome_class=EvaluationOutcomeClass.INDETERMINATE,
            failure_class=EvaluationFailureClass.SAFETY_INDETERMINATE,
            infrastructure_failure=False,
            replaceable=False,
            abort_study=True,
        )
    if failure_reason in _CONFIGURATION_FAILURES:
        return EvaluationOutcome(
            outcome_class=EvaluationOutcomeClass.INFRASTRUCTURE_INVALID,
            failure_class=EvaluationFailureClass.CONFIGURATION,
            infrastructure_failure=True,
            replaceable=False,
            abort_study=True,
        )
    if failure_reason in _REPLACEABLE_INFRASTRUCTURE:
        return EvaluationOutcome(
            outcome_class=EvaluationOutcomeClass.INFRASTRUCTURE_INVALID,
            failure_class=EvaluationFailureClass.INFRASTRUCTURE,
            infrastructure_failure=True,
            replaceable=True,
            abort_study=False,
        )
    if status is RepairCompletionStatus.RUNTIME_FAILURE:
        return EvaluationOutcome(
            outcome_class=EvaluationOutcomeClass.INFRASTRUCTURE_INVALID,
            failure_class=EvaluationFailureClass.INFRASTRUCTURE,
            infrastructure_failure=True,
            replaceable=False,
            abort_study=False,
        )
    return EvaluationOutcome(
        outcome_class=EvaluationOutcomeClass.SCORED,
        failure_class=EvaluationFailureClass.MODEL_QUALITY,
        infrastructure_failure=False,
        replaceable=False,
        abort_study=False,
    )
```

- [ ] Extend `RepairEvaluationRun` with `outcome_class` and `failure_class`.
- [ ] Validate:
  - verified success is scored with failure class `NONE`;
  - scored failure is not infrastructure;
  - infrastructure-invalid result has `infrastructure_failure=true`;
  - indeterminate result cannot be verified or infrastructure-replaceable.
- [ ] Run:

```powershell
uv run --frozen pytest tests/unit/test_evaluation_outcome_classification.py -ra
```

Expected: pass.

- [ ] Run existing evaluation model/selection tests.
- [ ] Commit:

```text
feat: classify evaluation outcomes explicitly
```

## Task 2: Expose Sanitized Physical Model-Attempt Facts

**Files:**

- Modify: `src/agentforge/models/domain.py`
- Modify: `src/agentforge/persistence/model_workflow.py`
- Test: `tests/unit/test_model_attempt_queries.py`

- [ ] Write failing tests for ordering, success usage, failed codes, retryability, and elapsed
  duration.
- [ ] Add:

```python
class ModelAttemptRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: UUID
    run_id: UUID
    logical_call_id: UUID
    attempt_number: int = Field(gt=0)
    status: Literal["STARTED", "COMPLETED", "FAILED"]
    error_type: ModelErrorCode | None = None
    retryable: bool | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    usage: ModelUsage | None = None
    created_at: UtcDatetime
    completed_at: UtcDatetime | None = None
```

- [ ] Add `ModelWorkflow.list_attempts(run_id) -> list[ModelAttemptRecord]`.
- [ ] Calculate a failed attempt's elapsed duration from persisted timestamps when
  `duration_ms` is absent; do not invent Provider latency.
- [ ] Ensure the record has no request content, response content, API key, or Provider request ID.
- [ ] Run the new test and existing model workflow/executor tests.
- [ ] Commit:

```text
feat: expose sanitized model attempt facts
```

## Task 3: Build Immutable Evaluation Telemetry

**Files:**

- Create: `src/agentforge/evaluation/telemetry_models.py`
- Create: `src/agentforge/evaluation/telemetry.py`
- Test: `tests/unit/test_evaluation_telemetry.py`

- [ ] Write failing aggregation and redaction tests.
- [ ] Define `EvaluationRunTelemetry` with:

```python
class EvaluationRunTelemetry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    evaluation_run_id: UUID
    run_id: UUID
    campaign_id: UUID
    attempt_id: UUID
    protocol_digest: str
    model_id: str
    logical_model_calls: int
    physical_model_requests: int
    completed_model_requests: int
    failed_model_requests: int
    retry_count: int
    usage_complete: bool
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    successful_provider_duration_ms: int
    maximum_provider_duration_ms: int
    model_attempt_elapsed_ms: int
    provider_deviation_count: int
    normalized_multi_tool_response_count: int
    returned_function_call_count: int
    discarded_function_call_count: int
    model_protocol_failure_count: int
    tool_requested_count: int
    tool_completed_count: int
    tool_failed_count: int
    read_call_count: int
    mutation_requested_count: int
    mutation_committed_count: int
    mutation_failed_count: int
    managed_test_requested_count: int
    managed_test_completed_count: int
    managed_test_failed_count: int
    managed_test_timeout_count: int
    approval_requested_count: int
    approval_granted_count: int
    approval_rejected_count: int
    context_compaction_count: int
    completion_correction_count: int
    policy_violation_count: int
    telemetry_digest: str = ""
```

- [ ] Implement `EvaluationTelemetryCollector.collect(record)`.
- [ ] Derive metrics only from persisted facts and safe Event payload counts.
- [ ] Reject cross-run or cross-Protocol facts.
- [ ] Compute a deterministic canonical digest.
- [ ] Run new telemetry tests plus event serialization tests.
- [ ] Commit:

```text
feat: collect durable evaluation telemetry
```

## Task 4: Persist Telemetry Idempotently

**Files:**

- Create: `src/agentforge/evaluation/telemetry_persistence.py`
- Modify: `src/agentforge/persistence/tables.py`
- Test: `tests/unit/test_evaluation_telemetry_persistence.py`

- [ ] Write failing save/get/reopen/conflict tests.
- [ ] Add one table:

```text
evaluation_run_telemetry
  evaluation_run_id PK/FK
  run_id UNIQUE
  campaign_id
  attempt_id UNIQUE
  protocol_digest
  telemetry_data JSON
  telemetry_digest
  schema_version
  created_at
```

- [ ] Enforce one-to-one identity and exact duplicate idempotency.
- [ ] Add `save`, `get`, `find`, and `list_for_campaign`.
- [ ] Verify `Database.create_schema()` creates the new table without modifying old result rows.
- [ ] Run persistence tests.
- [ ] Commit:

```text
feat: persist evaluation telemetry
```

## Task 5: Correct Result Selection Semantics

**Files:**

- Modify: `src/agentforge/evaluation/harness.py`
- Modify: `src/agentforge/evaluation/pilot_runner.py`
- Modify: `src/agentforge/evaluation/persistence.py`
- Modify: `src/agentforge/persistence/tables.py`
- Modify: `src/agentforge/evaluation/selection.py`
- Test: `tests/integration/test_real_model_study_replacement.py`
- Test: existing B2.3 Campaign tests

- [ ] Write RED tests proving an exhausted model protocol error is selected as a scored failure
  with no replacement.
- [ ] Write RED tests proving timeout receives at most one replacement.
- [ ] Write RED tests proving indeterminate result stops and retains the workspace.
- [ ] Persist outcome/failure class on `RepairEvaluationRunRow`.
- [ ] In `EvaluationHarness`, classify the terminal Repair state before creating the result.
- [ ] Save the result first, then collect/save telemetry. Make both operations idempotently
  recoverable.
- [ ] Change `_finish_result_attempt`:
  - `SCORED` -> completed Attempt and accepted Slot;
  - `INFRASTRUCTURE_INVALID` -> invalid Attempt and frozen replacement policy;
  - `INDETERMINATE` -> indeterminate Attempt/Slot and retained workspace.
- [ ] A `CONFIGURATION` failure must mark the current Campaign `BLOCKED` and stop its remaining
  repetitions before control returns to the Study Runner.
- [ ] Keep strict `resolve_effective_runs` behavior for complete Campaigns. Add
  `resolve_available_scored_runs` for `COMPLETED_WITH_INVALID` Campaigns so Study reporting can
  validate and summarize accepted slots without inventing results for invalid slots.
- [ ] Keep B2.3 Mock success and Provider timeout behavior passing.
- [ ] Add backward-safe result validation for newly created databases; do not claim production
  migration support.
- [ ] Run focused Campaign, recovery, selection, and formal E2E tests.
- [ ] Commit:

```text
fix: keep model quality failures in evaluation scores
```

## Task 6: Add Pricing Snapshot And Cost Estimation

**Files:**

- Create: `src/agentforge/evaluation/costs.py`
- Modify: `src/agentforge/evaluation/telemetry_models.py`
- Test: `tests/unit/test_evaluation_costs.py`

- [ ] Write RED tests for cached, uncached, output, incomplete usage, and decimal stability.
- [ ] Define `PricingSnapshot` using decimal strings, exact model ID, effective date, source URL,
  and canonical digest.
- [ ] Implement:

```python
def estimate_cost(
    telemetry: EvaluationRunTelemetry,
    pricing: PricingSnapshot,
) -> EvaluationCost:
    if telemetry.model_id != pricing.model_id or not telemetry.usage_complete:
        return EvaluationCost(complete=False, currency=pricing.currency)
    if telemetry.cached_input_tokens > 0 and pricing.cached_input_per_million is None:
        return EvaluationCost(complete=False, currency=pricing.currency)
    uncached = max(0, telemetry.input_tokens - telemetry.cached_input_tokens)
    cached_rate = pricing.cached_input_per_million or Decimal("0")
    amount = (
        Decimal(uncached) * pricing.input_per_million
        + Decimal(telemetry.cached_input_tokens) * cached_rate
        + Decimal(telemetry.output_tokens) * pricing.output_per_million
    ) / Decimal(1_000_000)
    return EvaluationCost(
        complete=True,
        currency=pricing.currency,
        amount=amount.quantize(Decimal("0.000001")),
    )
```

- [ ] Never use binary floating point for currency.
- [ ] Mark incomplete cost instead of substituting zero or an assumed rate.
- [ ] Run tests.
- [ ] Commit:

```text
feat: bind evaluation pricing snapshots
```

## Task 7: Define Study And Authorization Models

**Files:**

- Create: `src/agentforge/evaluation/study_models.py`
- Modify: `src/agentforge/domain/enums.py`
- Test: `tests/unit/test_evaluation_study_models.py`
- Test: `tests/unit/test_real_model_authorization.py`

- [ ] Write RED tests for deterministic digests and every cross-binding rejection.
- [ ] Add Study statuses from the design.
- [ ] Define:

```python
class EvaluationStudyDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    study_name: str
    task_ids: tuple[str, str, str, str]
    protocol_digests: tuple[str, str, str, str]
    provider_configuration_digest: str
    model_id: str
    repetitions_per_task: Literal[3] = 3
    planned_scoring_slots: Literal[12] = 12
    runtime_source_digest: str
    git_commit_sha: str
    git_worktree_clean: Literal[True] = True
    pyproject_sha256: str
    uv_lock_sha256: str
    fixture_registry_digest: str
    platform_binding_digest: str
    pricing_snapshot_digest: str
    definition_digest: str = ""


class RealModelAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    study_definition_digest: str
    protocol_digests: tuple[str, str, str, str]
    model_id: str
    maximum_campaigns: Literal[4] = 4
    maximum_planned_slots: Literal[12] = 12
    maximum_replacements_per_slot: Literal[1] = 1
    network_access_acknowledged: Literal[True] = True
    authorization_digest: str = ""


class EvaluationStudy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    study_id: UUID = Field(default_factory=uuid4)
    definition_digest: str
    authorization_digest: str | None = None
    status: EvaluationStudyStatus = EvaluationStudyStatus.DRAFT
    record_version: int = Field(default=1, gt=0)
    created_at: UtcDatetime = Field(default_factory=utc_now)
    updated_at: UtcDatetime = Field(default_factory=utc_now)
    completed_at: UtcDatetime | None = None


class EvaluationStudyCampaignBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    study_id: UUID
    task_id: str
    task_order: int = Field(ge=0, lt=4)
    protocol_digest: str
    campaign_id: UUID


class EvaluationStudySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    planned_slots: Literal[12] = 12
    scored_slots: int = Field(ge=0, le=12)
    successful_slots: int = Field(ge=0, le=12)
    infrastructure_invalid_slots: int = Field(ge=0, le=12)
    indeterminate_slots: int = Field(ge=0, le=12)
    scoring_coverage: float = Field(ge=0, le=1)
    scored_success_rate: float | None = Field(default=None, ge=0, le=1)
    planned_slot_success_rate: float = Field(ge=0, le=1)
    task_any_success_count: int = Field(ge=0, le=4)
    task_stable_success_count: int = Field(ge=0, le=4)
```

- [ ] Fix B2.4 task order as a tuple constant and require exactly four Protocols x three
  repetitions.
- [ ] Bind source, dependency, Fixture, platform, Provider, pricing, and authorization digests.
- [ ] Reject secret-like and open-ended configuration fields through `extra="forbid"`.
- [ ] Run model/authorization tests.
- [ ] Commit:

```text
feat: define real model evaluation studies
```

## Task 8: Persist Study State With CAS

**Files:**

- Create: `src/agentforge/evaluation/study_persistence.py`
- Modify: `src/agentforge/persistence/tables.py`
- Modify: `src/agentforge/domain/enums.py`
- Test: `tests/unit/test_evaluation_study_persistence.py`

- [ ] Write RED tests for idempotent registration, atomic Campaign binding, transition conflicts,
  terminal immutability, and database reopen.
- [ ] Add tables:
  - `evaluation_studies`
  - `evaluation_study_campaigns`
  - `evaluation_study_events`
- [ ] Store immutable definition/authorization data as validated JSON with independent digests.
- [ ] Use `record_version` conditional updates for every Study transition.
- [ ] Add ordered safe Study Events:
  - `EVALUATION_STUDY_CREATED`
  - `EVALUATION_STUDY_AUTHORIZED`
  - `EVALUATION_STUDY_STARTED`
  - `EVALUATION_STUDY_CAMPAIGN_COMPLETED`
  - `EVALUATION_STUDY_COMPLETED`
  - `EVALUATION_STUDY_ABORTED`
  - `EVALUATION_STUDY_INDETERMINATE`
- [ ] Ensure Events contain no model text, prompt, source, output, paths, pricing URL query data, or
  secrets.
- [ ] Run tests.
- [ ] Commit:

```text
feat: persist durable evaluation studies
```

## Task 9: Bind Source Provenance And Test Environment

**Files:**

- Create: `src/agentforge/evaluation/source_provenance.py`
- Create: `src/agentforge/evaluation/environment.py`
- Test: `tests/security/test_real_model_study_security.py`
- Modify: `.gitignore`

- [ ] Write RED tests using temporary clean and dirty Git repositories.
- [ ] Collect:
  - exact commit SHA;
  - clean-worktree Boolean;
  - canonical `src/agentforge` tree digest;
  - `pyproject.toml` SHA-256;
  - `uv.lock` SHA-256.
- [ ] Use fixed read-only Git argv, `shell=False`, bounded output, and no inherited `GIT_*` control
  variables.
- [ ] Build TestProfile environment only from fixed values plus `SYSTEMROOT`/`WINDIR` on Windows.
- [ ] Assert `OPENAI_API_KEY`, `TOKEN`, `SECRET`, `SSH`, proxy credentials, and arbitrary parent
  variables never enter TestProfile environment.
- [ ] Ignore:

```gitignore
# Private real-model evaluation state
.agentforge/
```

- [ ] Run security tests.
- [ ] Commit:

```text
feat: bind study source provenance
```

## Task 10: Build Four Frozen Real-Model Protocols

**Files:**

- Create: `src/agentforge/evaluation/study_builder.py`
- Test: `tests/integration/test_real_model_study_builder.py`
- Modify: `src/agentforge/evaluation/protocol.py`

- [ ] Write RED tests using all four real manifests and a fake Provider implementation.
- [ ] Add `B2_4_TASK_ORDER` exactly as defined in the design.
- [ ] Build one Protocol per task from current `PilotRuntimeFactory.inspect_manifest` facts.
- [ ] Apply exact Provider, Context, retry, token, and repetition settings from the design.
- [ ] Derive physical request/token envelope from BASIC or ENGINEERING difficulty.
- [ ] Require one exact model ID across all Protocols.
- [ ] Build the immutable Study definition only after all Protocols validate.
- [ ] Ensure Protocol and Study serialization contains no API key.
- [ ] Ensure no reference or hidden asset enters prompt construction.
- [ ] Run builder and existing protocol tests.
- [ ] Commit:

```text
feat: build frozen real model study protocols
```

## Task 11: Enforce The Real-Model Gate

**Files:**

- Create: `src/agentforge/evaluation/real_model_gate.py`
- Modify: `src/agentforge/evaluation/provider_factory.py`
- Test: `tests/unit/test_real_model_authorization.py`
- Test: `tests/unit/test_evaluation_provider_factory.py`

- [ ] Write RED tests for every missing/mismatched gate input.
- [ ] Define `RealModelExecutionGate.validate`, accepting an `EvaluationStudy`, a
  `Sequence[EvaluationProtocol]`, and the operator confirmation digest. It returns `None` only
  after all gate checks pass and otherwise raises `RealModelAuthorizationError` before Provider
  construction.

- [ ] Require the exact opt-in environment value, authorization, confirmation digest, clean source
  provenance, and protocol set.
- [ ] Keep `SecretStr` outside every persisted or reportable model.
- [ ] Require the gate before `OpenAIEvaluationProviderFactory.create` may create a real delegate.
- [ ] Map a Provider/model response-identity mismatch to a non-retryable
  `ModelRequestError(MODEL_BAD_REQUEST)` so it follows the configuration-abort path instead of an
  untyped exception or infrastructure replacement.
- [ ] Preserve injected fake-client tests without network.
- [ ] Run Provider factory and authorization tests.
- [ ] Commit:

```text
feat: gate formal real model execution
```

## Task 12: Implement Sequential Study Execution And Recovery

**Files:**

- Create: `src/agentforge/evaluation/study_runner.py`
- Test: `tests/integration/test_real_model_study_runner.py`
- Test: `tests/integration/test_real_model_study_replacement.py`
- Test: `tests/integration/test_real_model_study_recovery.py`

- [ ] Write RED tests proving all four Campaigns and 12 slots exist before the first Provider call.
- [ ] Add `EvaluationStudyRunner.run(study_id) -> EvaluationStudyResult` and
  `EvaluationStudyRunner.recover(study_id) -> EvaluationStudyResult` as the only public execution
  methods. Both delegate to one private state-machine method with an explicit `recover` Boolean;
  ordinary `run` must never adopt an existing active Campaign.

- [ ] Create/register all Protocols and Campaigns before Study execution.
- [ ] Execute Campaigns sequentially in fixed order.
- [ ] Call `PilotRunner.recover_campaign` only during explicit Study recovery.
- [ ] Stop immediately on configuration abort or indeterminate state.
- [ ] Continue past scored model failures.
- [ ] Complete with infrastructure gaps when replacement allowance is exhausted.
- [ ] Regenerate missing telemetry from durable facts.
- [ ] Make terminal `run`/`recover` idempotent with zero Provider or Tool calls.
- [ ] Run all Study integration tests.
- [ ] Commit:

```text
feat: orchestrate durable real model studies
```

## Task 13: Generate Denominator-Correct Public Reports

**Files:**

- Create: `src/agentforge/evaluation/study_reports.py`
- Create: `src/agentforge/evaluation/public_artifacts.py`
- Modify: `src/agentforge/evaluation/metrics.py`
- Modify: `src/agentforge/evaluation/reports.py`
- Test: `tests/unit/test_evaluation_study_reports.py`
- Test: `tests/security/test_real_model_study_security.py`

- [ ] Write RED tests for 0/12, mixed outcomes, 12/12, infrastructure gaps, and incomplete cost.
- [ ] Compute planned, scored, successful, and infrastructure-invalid denominators independently.
- [ ] Include all per-task and whole-Study metrics from the design.
- [ ] Render deterministic JSON and Markdown from one typed public-report model.
- [ ] Use an explicit field allowlist; do not serialize arbitrary domain models directly.
- [ ] Scan for:
  - supplied secret values;
  - prompt substrings;
  - known reference and hidden-test markers;
  - Windows and POSIX absolute paths;
  - Tool argument/output keys;
  - Provider request IDs.
- [ ] Refuse publication on a scan finding.
- [ ] State the non-official, cropped-Fixture, small-sample limitations.
- [ ] Run report and security tests.
- [ ] Commit:

```text
feat: publish redacted real model study reports
```

## Task 14: Add The Evaluator-Only Entry Point

**Files:**

- Create: `evaluation/run_real_model_pilot.py`
- Test: `tests/integration/test_real_model_study_builder.py`
- Test: `tests/security/test_real_model_study_security.py`

- [ ] Write tests around a callable `main(argv: Sequence[str]) -> int`.
- [ ] Add `prepare`:
  - validate offline preconditions;
  - build four Protocols and Study definition;
  - write private safe manifest under `.agentforge/`;
  - print only the Study digest and safe paths.
- [ ] Add `run`:
  - validate the real-model gate;
  - persist authorization;
  - execute sequentially;
  - return zero only for a reconciled terminal Study, regardless of model-quality success rate.
- [ ] Add `recover`:
  - require the same gate and digest;
  - call explicit Study recovery;
  - never silently restart an indeterminate side effect.
- [ ] Add `report`:
  - require no API key;
  - perform zero Provider calls;
  - write only redacted public artifacts.
- [ ] Do not register a project-level product CLI entry point in `pyproject.toml`.
- [ ] Run entry-point tests with fake Provider factories.
- [ ] Commit:

```text
feat: add real model pilot runbook entry point
```

## Task 15: Offline Integration And Regression Closure

**Files:** Tests and implementation files above.

- [ ] Run the focused B2.4 unit suite from the test plan.
- [ ] Run the focused B2.4 integration/security suite.
- [ ] Run the existing B2.3 focused suite.
- [ ] Run Fixture verification:

```powershell
uv run --frozen python evaluation/fixtures/verify_fixtures.py `
  --repeat 3 `
  --output evaluation/fixtures/verification_report.json
```

- [ ] Run complete verification:

```powershell
uv run --frozen pytest -ra
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen python -m compileall src
git diff --check
```

- [ ] Confirm `uv.lock` is byte-identical to the baseline.
- [ ] Confirm default tests made no network request.
- [ ] Record exact test totals; do not predict them.
- [ ] Commit:

```text
test: close milestone 7-b2.4 offline acceptance
```

## Task 16: Real Provider Smoke Preflight

**Files:**

- Existing: `tests/live/test_openai_live.py`
- Modify only if new safe metrics require it.

- [x] Use a supported exact model ID available to the evaluator's account.
- [x] Run the existing read-only generated-Fixture Live test.
- [x] Confirm authentication, model identity, Tool schema, final response, usage completeness,
  Provider deviation handling, and budget bounds.
- [ ] If the smoke test fails:
  - classify auth/bad request as configuration;
  - classify timeout/rate limit/transport as infrastructure;
  - do not touch formal Fixture prompts or tests;
  - do not begin the formal Study.
- [x] Record the smoke result separately from Study metrics.

No commit is required solely for a local secret-bearing smoke execution.

## Task 17: Freeze And Execute The Formal Study

**Files:** Private `.agentforge/` state plus generated public report.

- [ ] Re-run source/dependency/Fixture digest validation immediately before `prepare`.
- [ ] Generate the Study definition and four Protocols.
- [ ] Review:
  - exact model ID;
  - Provider settings;
  - Prompt and Tool-schema digests;
  - task budgets;
  - 12 planned slots;
  - replacement policy;
  - pricing snapshot;
  - Git/source/dependency/platform bindings.
- [ ] Set `RUN_REAL_MODEL_PILOT=1`.
- [ ] Execute with the exact confirmation digest.
- [ ] Do not edit code, Prompt, Fixture, test, model setting, or budget while the Study is active.
- [ ] On process interruption, use `recover`; never create a replacement Study with the same
  public identity.
- [ ] Generate the public JSON/Markdown report.
- [ ] Verify terminal re-run causes zero new Provider/Tool calls.
- [ ] Run the public forbidden-content scanner a second time.

## Task 18: Documentation And Truth-Boundary Closure

**Files:**

- Create after execution: `docs/milestone_07b2_4_report.md`
- Modify: `README.md`
- Modify: `docs/repair_evaluation_protocol.md`
- Modify: `docs/implementation_plan.md`
- Modify: `docs/architecture.md`

- [ ] Report exact offline verification totals.
- [ ] Report exact Study state, 12-slot coverage, successes, model-quality failures,
  infrastructure-invalid slots, replacements, tokens, latency, and cost completeness.
- [ ] Link the four Protocol digests and Study digest.
- [ ] Record smoke preflight separately.
- [ ] State every remaining boundary from the design.
- [ ] Never write:
  - "production-ready";
  - "official SWE-bench score";
  - "exactly-once Provider calls";
  - "real-model benchmark passed" unless all required evidence exists;
  - any hidden-test or source detail.
- [ ] If Study is partial, blocked, or indeterminate, use that exact status in README and report.
- [ ] Run complete verification again after documentation.
- [ ] Commit only the source, tests, safe documents, and redacted public artifacts:

```text
docs: close milestone 7-b2.4 real model pilot
```

## Final Acceptance Gate

B2.4 is complete only when:

1. Offline implementation and regression checks pass.
2. Provider smoke preflight passes.
3. The exact Study is authorized before formal requests.
4. All four Campaigns are durably represented.
5. Every planned slot is represented in the final report.
6. Model-quality failures are scored without replacement.
7. Infrastructure exclusions and replacements are visible.
8. Indeterminate side effects fail closed.
9. Terminal replay performs no additional side effect.
10. Public artifacts pass the forbidden-content scan.

The final status must be one of `PASS`, `PARTIAL`, `BLOCKED`, or `INDETERMINATE` as defined in the
design. Implementation correctness and model repair quality are reported separately.
