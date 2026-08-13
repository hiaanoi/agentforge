# Milestone 7-B2.3 Frozen Protocol and Durable Pilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an evaluator-owned, protocol-bound, durable formal repair Pilot that creates and selects repeated evaluation results without exposing evaluator assets or weakening existing side-effect guarantees.

**Architecture:** An immutable `EvaluationProtocol` is registered before a campaign creates fixed repetition slots. `PilotRunner` owns durable workspace leases, runtime assembly, baseline execution, approvals, final verification, replacement decisions, recovery reconciliation, and safe aggregation through a small campaign interface.

**Tech Stack:** Python 3.14, Pydantic v2, SQLAlchemy 2, SQLite, asyncio, pytest/pytest-asyncio, Ruff, mypy strict, existing AgentForge Runtime/M3-M7 coordinators.

**Completion:** Implemented and verified on 2026-07-29. Full suite: 488 collected,
479 passed, 9 skipped. No real-model Pilot was executed.

---

## File Map

### New production modules

- `src/agentforge/evaluation/protocol.py`
  - Immutable protocol, provider/platform/profile/replacement bindings, canonical digests.
- `src/agentforge/evaluation/protocol_persistence.py`
  - Immutable SQLite protocol registration and lookup.
- `src/agentforge/evaluation/campaign_models.py`
  - Campaign, slot, attempt, state enums, and terminal result models.
- `src/agentforge/evaluation/campaign_persistence.py`
  - Campaign/slot/attempt repositories, CAS transitions, and safe campaign audit events.
- `src/agentforge/evaluation/pilot_workspace.py`
  - Durable temporary workspace lease creation, reopen, validation, and cleanup.
- `src/agentforge/evaluation/provider_factory.py`
  - Trusted provider binding interface and OpenAI construction without credential persistence.
- `src/agentforge/evaluation/pilot_factory.py`
  - Full Runtime/Profile/Tool/Coordinator assembly from verified protocol and lease.
- `src/agentforge/evaluation/pilot_runner.py`
  - Deep campaign interface, sequential scheduling, replacement, and recovery.
- `src/agentforge/evaluation/selection.py`
  - Replacement-chain validation and effective-run selection.

### Modified production modules

- `src/agentforge/evaluation/formal_fixtures.py`
  - Frozen repair prompt, complete task asset digest, path-independent profile template digest.
- `src/agentforge/evaluation/models.py`
  - Bind protocol/campaign/slot/attempt facts to `RepairEvaluationRun`.
- `src/agentforge/evaluation/persistence.py`
  - Persist new result bindings and query by campaign.
- `src/agentforge/evaluation/metrics.py`
  - Aggregate selected runs and expose invalid/replacement counts.
- `src/agentforge/evaluation/reports.py`
  - Safe campaign report with protocol digest and no prompt/output/path data.
- `src/agentforge/evaluation/prompts.py`
  - Build formal prompt from leakage-reviewed manifest data.
- `src/agentforge/evaluation/harness.py`
  - Accept protocol-bound result identity; retain execution responsibility only.
- `src/agentforge/runtime/engine.py`
  - Emit digest-only `RUN_CREATED` payload.
- `src/agentforge/domain/enums.py`
  - Campaign event types.
- `src/agentforge/persistence/tables.py`
  - Protocol, campaign, slot, attempt, and campaign-event rows.
- `evaluation/fixtures/fixture_schema.json`
  - Required `repair_prompt` schema.
- `evaluation/fixtures/tasks/*/task_manifest.json`
  - Leakage-reviewed prompt for all four admitted tasks.
- `evaluation/fixtures/verify_fixtures.py`
  - Prompt validation and refreshed Fixture digest.

### New tests

- `tests/unit/test_evaluation_protocol.py`
- `tests/unit/test_evaluation_protocol_persistence.py`
- `tests/unit/test_formal_repair_prompt.py`
- `tests/unit/test_pilot_workspace.py`
- `tests/unit/test_evaluation_campaign_persistence.py`
- `tests/unit/test_evaluation_selection.py`
- `tests/unit/test_evaluation_provider_factory.py`
- `tests/integration/test_pilot_runtime_factory.py`
- `tests/integration/test_pilot_campaign_runner.py`
- `tests/integration/test_pilot_campaign_recovery.py`
- `tests/integration/test_pilot_security.py`

## Task 1: Frozen protocol domain

**Files:**
- Create: `src/agentforge/evaluation/protocol.py`
- Create: `tests/unit/test_evaluation_protocol.py`

- [ ] **Step 1: Write failing deterministic-digest and validation tests**

Define test fixtures using explicit safe bindings:

```python
def protocol_payload() -> dict[str, object]:
    return {
        "protocol_name": "offline-self-durable-v1",
        "execution_mode": "OFFLINE_TEST",
        "task_id": "self-durable-double-consumption",
        "fixture_registry_digest": SHA_F,
        "fixture_asset_digest": SHA_A,
        "expected_baseline_fingerprint_digest": SHA_B,
        "task_policy_digest": SHA_C,
        "test_profile_template_digest": SHA_D,
        "provider_binding": {
            "provider": "mock",
            "model_id": "deterministic-repair-model",
            "timeout_seconds": 30,
            "max_retries": 1,
            "store": False,
            "max_output_tokens": 2000,
            "multi_tool_response_policy": "SEQUENTIAL_READ_ONLY",
            "max_function_calls_per_response": 8,
            "configuration_digest": "",
        },
        "model_budget": {"max_model_requests": 10, "max_retries": 1, "max_total_tokens": 50000},
        "system_prompt_version": 1,
        "system_prompt": "Repair only through AgentForge tools.",
        "task_prompt": "Repair the duplicate dispatch symptom.",
        "tool_schema_digest": SHA_E,
        "context_policy": {
            "max_items": 100,
            "max_characters": 20000,
            "max_utf8_bytes": 40000,
            "version": "1",
            "system_prompt_version": "1",
            "system_instructions": "Repair only through AgentForge tools.",
        },
        "completion_correction_mode": "DEFAULT",
        "repetition_count": 3,
        "replacement_policy": {
            "mode": "INFRASTRUCTURE_ONLY",
            "max_replacements_per_slot": 1,
            "replaceable_failure_categories": ["MODEL_TRANSPORT_ERROR", "MODEL_TIMEOUT"],
        },
        "platform_binding": platform_binding(),
        "real_model_authorized": False,
    }
```

Tests must assert:

- equivalent normalized input produces the same `protocol_digest`;
- global Fixture registry and selected task asset digests are independently
  bound;
- `provider_binding.configuration_digest`, prompt digests, ContextPolicy
  instructions, and top-level protocol digest are computed and verified;
- `model_dump()` excludes no required reproducibility fact;
- `api_key`, `token`, `secret`, arbitrary model kwargs, or environment maps
  are rejected as extra fields;
- `REAL_MODEL` requires `real_model_authorized=True`;
- `OFFLINE_TEST` rejects `real_model_authorized=True`;
- non-infrastructure replacement categories are rejected;
- the resolved executable path and executable SHA-256 are mandatory.

- [ ] **Step 2: Run RED**

```powershell
uv run --frozen pytest tests/unit/test_evaluation_protocol.py -q
```

Expected: import failure because `agentforge.evaluation.protocol` does not exist.

- [ ] **Step 3: Implement protocol models and canonical digest helper**

Create:

```python
class EvaluationExecutionMode(StrEnum):
    OFFLINE_TEST = "OFFLINE_TEST"
    REAL_MODEL = "REAL_MODEL"


class ReplacementMode(StrEnum):
    INFRASTRUCTURE_ONLY = "INFRASTRUCTURE_ONLY"


class ProviderBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["mock", "openai"]
    model_id: str
    timeout_seconds: float
    max_retries: int
    store: Literal[False] = False
    max_output_tokens: int | None
    multi_tool_response_policy: MultiToolResponsePolicy
    max_function_calls_per_response: int
    configuration_digest: str = ""


class ReplacementPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal[ReplacementMode.INFRASTRUCTURE_ONLY]
    max_replacements_per_slot: int = Field(ge=0, le=3)
    replaceable_failure_categories: tuple[str, ...]


class PlatformBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    os_family: Literal["WINDOWS", "POSIX"]
    python_implementation: str
    python_version: str
    executable_path: str
    executable_sha256: str


class EvaluationProtocol(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    # all fields from the design
    protocol_digest: str = ""
```

Every nested model computes and validates its own digest in an `after`
validator. Use one shared canonical JSON SHA-256 helper with sorted keys,
UTF-8, and compact separators.

- [ ] **Step 4: Run GREEN**

```powershell
uv run --frozen pytest tests/unit/test_evaluation_protocol.py -q
uv run --frozen ruff check src/agentforge/evaluation/protocol.py tests/unit/test_evaluation_protocol.py
uv run --frozen mypy src/agentforge/evaluation/protocol.py
```

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/evaluation/protocol.py tests/unit/test_evaluation_protocol.py
git commit -m "feat: add frozen evaluation protocol"
```

## Task 2: Immutable protocol persistence

**Files:**
- Create: `src/agentforge/evaluation/protocol_persistence.py`
- Modify: `src/agentforge/persistence/tables.py`
- Create: `tests/unit/test_evaluation_protocol_persistence.py`

- [ ] **Step 1: Write failing repository tests**

Test:

```python
persisted = repository.register(protocol)
assert repository.get(protocol.protocol_digest) == protocol
assert repository.get_by_name(protocol.protocol_name) == protocol
assert repository.register(protocol) == protocol
```

Then create a protocol with the same `protocol_name` and different digest and
assert `ProtocolConflictError`. Assert the row has no columns or serialized
values containing API key names. Assert registration writes one safe protocol
audit fact containing only name, task, provider, model, versions, and digests.

- [ ] **Step 2: Run RED**

```powershell
uv run --frozen pytest tests/unit/test_evaluation_protocol_persistence.py -q
```

- [ ] **Step 3: Add rows and repository**

Add:

```python
class EvaluationProtocolRow(Base):
    __tablename__ = "evaluation_protocols"
    protocol_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    protocol_name: Mapped[str] = mapped_column(String(200), unique=True)
    task_id: Mapped[str] = mapped_column(String(200), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    model_id: Mapped[str] = mapped_column(String(200))
    protocol_data: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
```

`EvaluationProtocolRepository.register()` must compare the complete validated
domain object when an existing digest is found and fail on name reuse with a
different digest.

- [ ] **Step 4: Run GREEN and static checks**

```powershell
uv run --frozen pytest tests/unit/test_evaluation_protocol_persistence.py -q
uv run --frozen ruff check src/agentforge/evaluation/protocol_persistence.py src/agentforge/persistence/tables.py tests/unit/test_evaluation_protocol_persistence.py
uv run --frozen mypy src
```

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/evaluation/protocol_persistence.py src/agentforge/persistence/tables.py tests/unit/test_evaluation_protocol_persistence.py
git commit -m "feat: persist immutable evaluation protocols"
```

## Task 3: Frozen formal prompts and Fixture binding

**Files:**
- Modify: `src/agentforge/evaluation/formal_fixtures.py`
- Modify: `src/agentforge/evaluation/prompts.py`
- Modify: `evaluation/fixtures/fixture_schema.json`
- Modify: `evaluation/fixtures/tasks/bugsinpy-black-21/task_manifest.json`
- Modify: `evaluation/fixtures/tasks/quixbugs-shortest-path-length/task_manifest.json`
- Modify: `evaluation/fixtures/tasks/self-durable-double-consumption/task_manifest.json`
- Modify: `evaluation/fixtures/tasks/swebench-pytest-10051/task_manifest.json`
- Modify: `evaluation/fixtures/verify_fixtures.py`
- Create: `tests/unit/test_formal_repair_prompt.py`
- Modify: `tests/evaluation/test_fixture_assets.py`

- [ ] **Step 1: Write failing prompt and digest tests**

Require:

```python
manifest = FormalFixtureLoader().load(task_root)
assert manifest.repair_prompt.title
assert manifest.repair_prompt.description
assert manifest.repair_prompt.success_conditions
assert manifest.asset_digest == FormalFixtureLoader().load(task_root).asset_digest
assert "hidden" not in manifest.repair_prompt.model_dump_json().casefold()
assert "reference/fixed_files" not in manifest.repair_prompt.model_dump_json()
```

For the primary Demo assert the prompt describes duplicate dispatch after a
restart but does not contain `idempotent`, `exactly once`, `double
consumption`, `compare-and-set`, table names, or the hidden concurrency
matrix.

Assert `build_formal_prompt_bundle()` is deterministic and uses the repair
system prompt in ContextPolicy.

- [ ] **Step 2: Run RED**

```powershell
uv run --frozen pytest tests/unit/test_formal_repair_prompt.py tests/evaluation/test_fixture_assets.py -q
```

- [ ] **Step 3: Implement `FormalRepairPrompt` and complete task digest**

Add:

```python
class FormalRepairPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    success_conditions: tuple[str, ...] = Field(min_length=1, max_length=20)
```

Compute each task asset digest over every file under that task root using
relative path plus file SHA-256. Add path-independent visible/hidden profile
template digest computation.

Add leakage-reviewed prompts to all four manifests. The task prompt must be a
deterministic composition of title, description, allowed paths, visible
profile ID, file-creation rule, and success conditions.

- [ ] **Step 4: Extend verifier and regenerate derived assets**

Validate prompt shape and banned evaluator-only phrases in
`verify_fixtures.py`. Then run:

```powershell
uv run --frozen python evaluation/fixtures/verify_fixtures.py --repeat 3
uv run --frozen python evaluation/preflight/build_assets.py
uv run --frozen pytest tests/evaluation/test_fixture_assets.py tests/evaluation/test_preflight_assets.py -q
```

Expected: 48 Fixture executions pass and preflight digest tests pass.

- [ ] **Step 5: Run GREEN and commit**

```powershell
uv run --frozen pytest tests/unit/test_formal_repair_prompt.py tests/evaluation/test_fixture_assets.py -q
git add src/agentforge/evaluation/formal_fixtures.py src/agentforge/evaluation/prompts.py evaluation/fixtures evaluation/preflight tests/unit/test_formal_repair_prompt.py tests/evaluation/test_fixture_assets.py
git commit -m "feat: freeze formal repair prompts"
```

## Task 4: Durable Pilot workspace leases

**Files:**
- Create: `src/agentforge/evaluation/pilot_workspace.py`
- Create: `tests/unit/test_pilot_workspace.py`
- Modify: `tests/evaluation/test_formal_fixture_pilot.py`

- [ ] **Step 1: Write failing lease tests**

Cover:

- create-exclusively under the system temp root;
- unique workspace per attempt;
- exact lease bindings and safe root digest;
- hidden/reference exclusion from model workspace;
- hidden test external location;
- idempotent reopen;
- mismatch in protocol, campaign, slot, attempt, nonce, or Fixture digest;
- tampered lease JSON;
- cleanup only after terminal ownership confirmation;
- symlink/reparse rejection with platform-gated tests.

Expected interface:

```python
lease = manager.create(manifest, binding)
same = manager.reopen(lease.lease_id, binding)
manager.cleanup(same, terminal=True)
```

- [ ] **Step 2: Run RED**

```powershell
uv run --frozen pytest tests/unit/test_pilot_workspace.py -q
```

- [ ] **Step 3: Implement lease domain and manager**

Use a frozen `PilotWorkspaceLease` with:

```python
lease_id: UUID
campaign_id: UUID
slot_id: UUID
attempt_id: UUID
protocol_digest: str
fixture_asset_digest: str
workspace_root: str
workspace_root_digest: str
hidden_test_root: str
nonce: UUID
created_at: UtcDatetime
```

The lease file stays in the attempt parent, not `model_workspace`. Validate
all resolved paths remain beneath the manager root and reject any filesystem
entry that is a symlink, reparse point, or unsupported type.

- [ ] **Step 4: Run GREEN and commit**

```powershell
uv run --frozen pytest tests/unit/test_pilot_workspace.py tests/evaluation/test_formal_fixture_pilot.py -q
uv run --frozen ruff check src/agentforge/evaluation/pilot_workspace.py tests/unit/test_pilot_workspace.py
uv run --frozen mypy src
git add src/agentforge/evaluation/pilot_workspace.py tests/unit/test_pilot_workspace.py tests/evaluation/test_formal_fixture_pilot.py
git commit -m "feat: add durable pilot workspace leases"
```

## Task 5: Campaign, slot, attempt, and audit persistence

**Files:**
- Create: `src/agentforge/evaluation/campaign_models.py`
- Create: `src/agentforge/evaluation/campaign_persistence.py`
- Modify: `src/agentforge/persistence/tables.py`
- Modify: `src/agentforge/domain/enums.py`
- Create: `tests/unit/test_evaluation_campaign_persistence.py`

- [ ] **Step 1: Write failing lifecycle and CAS tests**

Test:

- campaign creation atomically creates fixed indexes `0..repetition_count-1`;
- duplicate creation for the protocol is idempotent only for identical facts;
- one of two conditional slot claims wins;
- attempts have monotonic numbers and one active attempt;
- valid state transitions follow the design;
- terminal slots and attempts are immutable;
- campaign event sequence is monotonic and isolated;
- audit payload JSON has no prompt, output, environment, source, or local path;
- repeated transition calls return the persisted result or stable conflict.

- [ ] **Step 2: Run RED**

```powershell
uv run --frozen pytest tests/unit/test_evaluation_campaign_persistence.py -q
```

- [ ] **Step 3: Implement models, rows, repositories, and workflow**

Add rows:

```python
EvaluationCampaignRow
EvaluationSlotRow
EvaluationPilotAttemptRow
EvaluationCampaignEventRow
```

Required uniqueness:

- campaign identity;
- `(campaign_id, repetition_index)`;
- `(slot_id, attempt_number)`;
- `(campaign_id, sequence_number)`;
- one selected evaluation result per slot.

Every transition uses status plus `record_version` in the SQL `WHERE` clause.
Do not hold a transaction while copying files, running tests, or calling a
model.

- [ ] **Step 4: Run GREEN and commit**

```powershell
uv run --frozen pytest tests/unit/test_evaluation_campaign_persistence.py -q
uv run --frozen ruff check src/agentforge/evaluation/campaign_models.py src/agentforge/evaluation/campaign_persistence.py src/agentforge/persistence/tables.py tests/unit/test_evaluation_campaign_persistence.py
uv run --frozen mypy src
git add src/agentforge/evaluation/campaign_models.py src/agentforge/evaluation/campaign_persistence.py src/agentforge/persistence/tables.py src/agentforge/domain/enums.py tests/unit/test_evaluation_campaign_persistence.py
git commit -m "feat: persist durable evaluation campaigns"
```

## Task 6: Replacement chains, selected metrics, and reports

**Files:**
- Create: `src/agentforge/evaluation/selection.py`
- Modify: `src/agentforge/evaluation/models.py`
- Modify: `src/agentforge/evaluation/persistence.py`
- Modify: `src/agentforge/evaluation/metrics.py`
- Modify: `src/agentforge/evaluation/reports.py`
- Create: `tests/unit/test_evaluation_selection.py`
- Modify: `tests/unit/test_repair_evaluation_models.py`
- Modify: `tests/unit/test_repair_metrics.py`

- [ ] **Step 1: Write failing selection tests**

Add protocol/campaign/slot/attempt bindings to evaluation results. Test:

```python
selection = resolve_effective_runs(protocol, slots, attempts, runs)
assert selection.selected_runs == (replacement, second_slot)
assert selection.superseded_run_ids == (original.evaluation_run_id,)
assert selection.infrastructure_invalid_count == 1
assert selection.replacement_count == 1
```

Reject:

- replacement of a normal model failure;
- cross-task/protocol/repetition predecessor;
- missing predecessor;
- cycle;
- branch with two replacements from one predecessor;
- attempt-number regression;
- duplicate accepted result;
- incomplete slot set when a complete report is requested.

Assert metrics use selected valid runs only and reports expose safe counts plus
digests, not prompts or local paths.

- [ ] **Step 2: Run RED**

```powershell
uv run --frozen pytest tests/unit/test_evaluation_selection.py tests/unit/test_repair_evaluation_models.py tests/unit/test_repair_metrics.py -q
```

- [ ] **Step 3: Implement selection and result bindings**

Add to `RepairEvaluationRun` and row mapping:

```python
protocol_digest: str
campaign_id: UUID
slot_id: UUID
attempt_id: UUID
attempt_number: int
```

Replace direct `summarize_task(all_runs)` use in campaign reporting with:

```python
selection = resolve_effective_runs(...)
summary = summarize_task(list(selection.selected_runs))
```

Keep the existing low-level `summarize_task` strict about duplicate
repetition indexes.

- [ ] **Step 4: Run GREEN and commit**

```powershell
uv run --frozen pytest tests/unit/test_evaluation_selection.py tests/unit/test_repair_evaluation_models.py tests/unit/test_repair_metrics.py -q
uv run --frozen mypy src
git add src/agentforge/evaluation/selection.py src/agentforge/evaluation/models.py src/agentforge/evaluation/persistence.py src/agentforge/evaluation/metrics.py src/agentforge/evaluation/reports.py tests/unit/test_evaluation_selection.py tests/unit/test_repair_evaluation_models.py tests/unit/test_repair_metrics.py
git commit -m "feat: resolve evaluation replacement chains"
```

## Task 7: Trusted provider binding and task-event redaction

**Files:**
- Create: `src/agentforge/evaluation/provider_factory.py`
- Modify: `src/agentforge/runtime/engine.py`
- Create: `tests/unit/test_evaluation_provider_factory.py`
- Modify: `tests/integration/test_runtime.py`

- [ ] **Step 1: Write failing provider and audit tests**

Test:

- OFFLINE_TEST accepts only the exact mock binding;
- OpenAI factory maps every frozen provider field to
  `ModelProviderConfig`;
- factory gets the API key externally and protocol serialization cannot
  contain it;
- mismatched provider/model/configuration digest fails before generation;
- REAL_MODEL protocol requires explicit authorization;
- `RUN_CREATED` contains `task_digest` and not task text;
- no Event contains the full task prompt.

- [ ] **Step 2: Run RED**

```powershell
uv run --frozen pytest tests/unit/test_evaluation_provider_factory.py tests/integration/test_runtime.py -q
```

- [ ] **Step 3: Implement provider factory and redacted event**

Define:

```python
class EvaluationProviderFactory(Protocol):
    def create(self, protocol: EvaluationProtocol) -> ModelProvider: ...


class OpenAIEvaluationProviderFactory:
    def __init__(self, api_key: SecretStr) -> None: ...
    def create(self, protocol: EvaluationProtocol) -> ModelProvider: ...
```

Change `AgentRuntime.create_run` to append:

```python
{"task_digest": hashlib.sha256(task.encode("utf-8")).hexdigest()}
```

instead of full task text. Preserve Runtime behavior and Event ordering.

- [ ] **Step 4: Run GREEN and commit**

```powershell
uv run --frozen pytest tests/unit/test_evaluation_provider_factory.py tests/integration/test_runtime.py tests/live/test_openai_live.py -q
uv run --frozen ruff check src/agentforge/evaluation/provider_factory.py src/agentforge/runtime/engine.py tests/unit/test_evaluation_provider_factory.py
uv run --frozen mypy src
git add src/agentforge/evaluation/provider_factory.py src/agentforge/runtime/engine.py tests/unit/test_evaluation_provider_factory.py tests/integration/test_runtime.py
git commit -m "feat: bind evaluation providers safely"
```

## Task 8: Full Pilot Runtime factory

**Files:**
- Create: `src/agentforge/evaluation/pilot_factory.py`
- Modify: `src/agentforge/evaluation/harness.py`
- Modify: `src/agentforge/evaluation/persistence.py`
- Create: `tests/integration/test_pilot_runtime_factory.py`
- Create: `tests/integration/test_pilot_security.py`

- [ ] **Step 1: Write failing assembly tests**

Using a formal Fixture lease and deterministic provider, assert:

- factory returns a ready `PilotExecution` with Run, metadata, Harness, and
  cleanup bindings;
- registry has exactly `list_files`, `read_file`, `search_text`, `edit_file`,
  conditional `write_file`, and `run_tests`;
- no shell, network, Git mutation, hidden-test-reading tool, or profile
  registration interface exists;
- ContextPolicy system instructions equal the frozen repair prompt;
- exported Tool schema digest, task policy digest, profile template digest,
  Fixture digest, baseline fingerprint, executable digest, and prompts match
  the protocol;
- visible and hidden M6 concrete plans are persisted;
- hidden tests/reference fixes are unreadable from the model workspace;
- environment is the complete registered allowlist and excludes key/token/
  secret/SSH variables;
- a single drifted binding aborts before any provider request.

- [ ] **Step 2: Run RED**

```powershell
uv run --frozen pytest tests/integration/test_pilot_runtime_factory.py tests/integration/test_pilot_security.py -q
```

- [ ] **Step 3: Implement the deep assembly module**

Expose only:

```python
class PilotRuntimeFactory:
    def prepare(
        self,
        protocol: EvaluationProtocol,
        manifest: FormalFixtureManifest,
        attempt: PilotAttempt,
        lease: PilotWorkspaceLease,
    ) -> PilotExecution: ...
```

`PilotExecution` contains the objects needed by `PilotRunner`, but callers
cannot modify protocol/profile facts. The factory must build all repositories,
coordinators, profiles, tools, ContextBuilder, ModelExecutor, Runtime,
RepairState, workspace baseline, baseline execution, and Harness.

- [ ] **Step 4: Run GREEN and commit**

```powershell
uv run --frozen pytest tests/integration/test_pilot_runtime_factory.py tests/integration/test_pilot_security.py -q
uv run --frozen ruff check src/agentforge/evaluation/pilot_factory.py src/agentforge/evaluation/harness.py tests/integration/test_pilot_runtime_factory.py tests/integration/test_pilot_security.py
uv run --frozen mypy src
git add src/agentforge/evaluation/pilot_factory.py src/agentforge/evaluation/harness.py src/agentforge/evaluation/persistence.py tests/integration/test_pilot_runtime_factory.py tests/integration/test_pilot_security.py
git commit -m "feat: assemble protocol-bound pilot runtimes"
```

## Task 9: PilotRunner scheduling, replacement, and recovery

**Files:**
- Create: `src/agentforge/evaluation/pilot_runner.py`
- Create: `tests/integration/test_pilot_campaign_runner.py`
- Create: `tests/integration/test_pilot_campaign_recovery.py`

- [ ] **Step 1: Write failing campaign execution tests**

Test the public interface:

```python
campaign = runner.create_campaign(protocol)
result = await runner.run_campaign(campaign.campaign_id)
same = await runner.run_campaign(campaign.campaign_id)
assert same == result
```

Cover:

- fixed slots run sequentially;
- each slot has a fresh workspace and baseline;
- baseline does not consume model/tool/test budgets;
- all valid model outcomes are accepted without replacement;
- allowlisted infrastructure failure creates one fresh linked replacement;
- normal repair/test/policy/budget failure is not replaced;
- replacement allowance exhaustion closes the slot INVALID;
- repeated calls do not duplicate attempts, model calls, mutations, or tests;
- selected evaluation result is persisted per slot;
- campaign completes only after all slots are accepted;
- incomplete/invalid campaign has no publishable complete summary.

- [ ] **Step 2: Write failing recovery tests**

Recreate repositories, managers, factories, and runner from the same SQLite
database after each persisted phase:

- campaign CREATED;
- slot CLAIMED;
- attempt CREATED;
- WORKSPACE_READY;
- RUNTIME_READY;
- baseline completed before model;
- persisted `RepairEvaluationRun` before attempt finalization;
- RUNNING with no active side-effect fact;
- mutation WRITING/CLAIMED;
- test execution STARTED;
- terminal accepted slot.

Assert safe pre-Run or no-side-effect orphans become INVALID and follow the
frozen replacement policy. Assert active uncertain side effects become
INDETERMINATE and never replace. Assert persisted terminal facts reconcile
idempotently.

- [ ] **Step 3: Run RED**

```powershell
uv run --frozen pytest tests/integration/test_pilot_campaign_runner.py tests/integration/test_pilot_campaign_recovery.py -q
```

- [ ] **Step 4: Implement `PilotRunner`**

Public interface:

```python
class PilotRunner:
    def create_campaign(self, protocol: EvaluationProtocol) -> EvaluationCampaign: ...
    async def run_campaign(self, campaign_id: UUID) -> CampaignResult: ...
    async def recover_campaign(self, campaign_id: UUID) -> CampaignResult: ...
```

Use the campaign workflow for every state change. Keep workspace copying,
provider calls, Runtime execution, and cleanup outside transactions. Catch
only classified infrastructure errors; unexpected programming exceptions
must persist a safe INVALID category and re-raise after reconciliation.

- [ ] **Step 5: Run GREEN and commit**

```powershell
uv run --frozen pytest tests/integration/test_pilot_campaign_runner.py tests/integration/test_pilot_campaign_recovery.py -q
uv run --frozen ruff check src/agentforge/evaluation/pilot_runner.py tests/integration/test_pilot_campaign_runner.py tests/integration/test_pilot_campaign_recovery.py
uv run --frozen mypy src
git add src/agentforge/evaluation/pilot_runner.py tests/integration/test_pilot_campaign_runner.py tests/integration/test_pilot_campaign_recovery.py
git commit -m "feat: run durable evaluation campaigns"
```

## Task 10: Formal Fixture end-to-end and regression closeout

**Files:**
- Modify: `tests/integration/test_repair_evaluation_e2e.py`
- Create: `tests/integration/test_formal_pilot_campaign_e2e.py`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/implementation_plan.md`
- Modify: `docs/repair_evaluation_protocol.md`
- Create: `docs/milestone_07b2_3_report.md`

- [ ] **Step 1: Add full formal E2E**

Run the primary Demo through:

```text
protocol registration
-> campaign and slot creation
-> durable workspace lease
-> exact buggy baseline
-> deterministic Mock provider read/edit/test/final sequence
-> durable approval/resume
-> diff validation
-> evaluator-owned hidden verification
-> immutable RepairEvaluationRun
-> selected slot
-> TaskEvaluationSummary
-> safe campaign report
```

Assert:

- one verified selected result;
- visible baseline then model-driven development tests then hidden final;
- baseline is excluded from `test_runs`;
- reference fix and hidden output never enter model requests, Events, or
  reports;
- local absolute workspace paths and full prompt text never enter Events or
  reports;
- all persisted IDs and digests form one valid binding chain.

- [x] **Step 2: Run focused B2.3 suite**

```powershell
uv run --frozen pytest tests/unit/test_evaluation_protocol.py tests/unit/test_evaluation_protocol_persistence.py tests/unit/test_formal_repair_prompt.py tests/unit/test_pilot_workspace.py tests/unit/test_evaluation_campaign_persistence.py tests/unit/test_evaluation_selection.py tests/unit/test_evaluation_provider_factory.py tests/integration/test_pilot_runtime_factory.py tests/integration/test_pilot_security.py tests/integration/test_pilot_campaign_runner.py tests/integration/test_pilot_campaign_recovery.py tests/integration/test_formal_pilot_campaign_e2e.py -ra
```

- [x] **Step 3: Update docs without overstating capability**

Document:

- B2.3 owns offline durable campaign execution;
- no real-model repair result exists yet;
- no public benchmark score exists;
- real-model protocol authorization remains B2.4;
- SQLite migrations, POSIX acceptance, junction/reparse completeness, and OS
  sandboxing remain deferred.

- [x] **Step 4: Run complete verification**

```powershell
uv run --frozen pytest -ra
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen python -m compileall src
uv run --frozen python evaluation/fixtures/verify_fixtures.py --repeat 3
uv run --frozen pytest tests/evaluation/test_preflight_assets.py -q
git diff --check
```

Record exact collected/passed/skipped counts in
`docs/milestone_07b2_3_report.md`, then rerun `git diff --check`.

- [x] **Step 5: Commit implementation closeout**

```powershell
git add README.md docs src evaluation tests
git commit -m "feat: complete milestone 7-b2.3 pilot protocol"
```

Do not tag, merge, push, or execute a real-model Pilot in this task.

## Plan Self-Review

### Spec coverage

- Frozen protocol: Tasks 1-3 and 7.
- Deep evaluator-owned module: Tasks 8-9.
- Durable campaign facts and CAS: Task 5.
- Durable workspace isolation: Task 4.
- Repetition and replacement semantics: Tasks 5, 6, and 9.
- Recovery and side-effect fail-closed behavior: Task 9.
- Hidden-test and prompt secrecy: Tasks 3, 4, 7, 8, and 10.
- Formal E2E and complete regression: Task 10.

### Strong-module review

- `PilotRunner` does not forward to a prebuilt Harness; it creates and owns
  campaign lifecycle.
- `PilotRuntimeFactory` centralizes the full object graph and revalidates all
  protocol bindings.
- Callers cannot register profiles, choose environments, skip baseline, run
  hidden tests early, or choose replacements.
- Campaign recovery classifies persisted side-effect facts rather than
  blindly rerunning.

### Type and naming consistency

- `EvaluationProtocol.protocol_digest` is the cross-module identity.
- `EvaluationCampaign` contains `EvaluationSlot`; slots contain
  `PilotAttempt`.
- `RepairEvaluationRun` binds protocol/campaign/slot/attempt.
- `resolve_effective_runs` is the only campaign aggregation input selector.
- `PilotWorkspaceLease` is the only source of concrete Pilot paths.

### Explicit exclusions

No task adds a weak proxy, general command runner, model-controlled
configuration, live benchmark execution, UI, API server, or sandbox claim.
