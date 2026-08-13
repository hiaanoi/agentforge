# AgentForge A1 Durable Product Kernel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Run creation, event append, source mutation, Provider dispatch, approval recovery, and cross-process ownership durable enough to expose through a product API.

**Architecture:** Add product persistence contracts around the existing Runtime rather than replacing it. A transaction-scoped `ApplicationUnitOfWork` owns atomic bundles; one scope-aware `EventLog` owns event ordering and authority checks; source revisions, leases, receipts, model attempts, and profile trust all use CAS and fail closed.

**Tech Stack:** Python 3.11+, SQLAlchemy 2, SQLite, Pydantic v2, asyncio, pytest with multiprocessing/subprocess integration tests.

---

## File Map

- Create `src/agentforge/application/__init__.py` and `src/agentforge/testing/__init__.py`: explicit package markers.
- Create `src/agentforge/application/contracts.py`: receipt, authority and lifecycle/outcome enums.
- Create `src/agentforge/application/kernel_errors.py`: kernel persistence/authority errors.
- Create `src/agentforge/persistence/product_tables.py`: schema version, receipt, lease, source, trust and control rows.
- Modify `src/agentforge/persistence/tables.py`: scope-aware EventRow, Run event sequence/version, model attempt states and verification source fields.
- Modify `src/agentforge/persistence/database.py`: product schema validation and explicit Session/UoW seam.
- Create `src/agentforge/persistence/event_log.py`: the only Event append/query implementation.
- Create `src/agentforge/persistence/application_uow.py`: atomic product transaction boundary.
- Create `src/agentforge/persistence/receipts.py`: idempotency state machine and request digest checks.
- Create `src/agentforge/persistence/run_leases.py`: CAS acquire/renew/release/fencing.
- Create `src/agentforge/persistence/source_revisions.py`: deterministic workspace digest and revision CAS.
- Create `src/agentforge/persistence/profile_trust.py`: safe Profile identity/trust records.
- Create `src/agentforge/application/run_creation.py`: atomic Run bundle workflow.
- Modify `src/agentforge/persistence/repositories.py`, `model_workflow.py`, `approval_workflow.py`, `mutation_workflow.py`, `test_execution_workflow.py`, and `repair_workflow.py`: accept transaction-scoped operations and EventLog.
- Modify `src/agentforge/runtime/engine.py`, `runtime/mutations.py`, `runtime/test_execution.py`, and `models/executor.py`: pass authority/fence and recovery facts.
- Modify `src/agentforge/tools/testing/profiles.py`: purpose and trusted identity reconstruction.
- Add focused unit and integration tests listed by task below.

### Task 1: Product schema version and persistence rows

**Files:**
- Create: `src/agentforge/application/contracts.py`
- Create: `src/agentforge/persistence/product_tables.py`
- Modify: `src/agentforge/persistence/tables.py`
- Modify: `src/agentforge/persistence/database.py`
- Create: `tests/unit/test_product_schema.py`

- [ ] **Step 1: Write failing fresh, missing and incompatible schema tests**

```python
def test_fresh_database_records_product_schema_version(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "product.sqlite3")
    database.create_schema()
    database.validate_product_schema()


def test_application_rejects_unversioned_or_wrong_schema(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "old.sqlite3")
    with pytest.raises(IncompatibleProductSchemaError):
        database.validate_product_schema()
    database.create_schema()
    set_schema_version(database, 999)
    with pytest.raises(IncompatibleProductSchemaError):
        database.validate_product_schema()
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/unit/test_product_schema.py -q`

Expected: FAIL because product schema contracts do not exist.

- [ ] **Step 3: Add closed enums, rows and validation**

```python
PRODUCT_SCHEMA_VERSION = 1


class ReceiptStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INDETERMINATE = "INDETERMINATE"


class LifecycleStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    TERMINAL = "TERMINAL"


class OutcomeStatus(StrEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
```

Create SQLAlchemy rows with explicit unique constraints:

```python
class ProductSchemaVersionRow(Base):
    __tablename__ = "product_schema_version"
    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class ApplicationCommandReceiptRow(Base):
    __tablename__ = "application_command_receipts"
    command_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    command_type: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_scope_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result_scope_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

Also define `RunLeaseRow`, `WorkspaceSourceBindingRow`, `TrustedProfileRow`, and
`RunControlRequestRow` with the exact fields from the approved design. Import `product_tables` in
`Database.create_schema()` before `Base.metadata.create_all`; insert version 1 only for a fresh
schema. `validate_product_schema()` refuses missing or unequal versions.

- [ ] **Step 4: Run GREEN and existing persistence regression**

Run: `uv run --frozen pytest tests/unit/test_product_schema.py tests/unit/test_persistence.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/application/__init__.py src/agentforge/testing/__init__.py src/agentforge/application/contracts.py src/agentforge/application/kernel_errors.py src/agentforge/persistence/product_tables.py src/agentforge/persistence/tables.py src/agentforge/persistence/database.py tests/unit/test_product_schema.py
git commit -m "feat: add versioned product persistence schema"
```

### Task 2: Scope-aware EventLog with closed authorities

**Files:**
- Create: `src/agentforge/persistence/event_log.py`
- Modify: `src/agentforge/domain/models.py`
- Modify: `src/agentforge/persistence/tables.py`
- Modify: `src/agentforge/persistence/repositories.py`
- Modify: `src/agentforge/persistence/model_workflow.py`
- Create: `tests/unit/test_event_log.py`

- [ ] **Step 1: Write authority, cursor, sequence and stale-fence tests**

```python
def test_event_log_orders_scopes_without_run_sequence_collision(kernel: KernelFixture) -> None:
    with kernel.database.session() as session:
        created = kernel.events.append(session, run_created_authority(kernel.run_id), EventType.RUN_CREATED, {})
        conversation = kernel.events.append(session, conversation_authority("c1", 1), "CONVERSATION_STARTED", {})
        resumed = kernel.events.append(session, run_lease_authority(kernel.lease), EventType.RUN_RESUMED, {})
    assert [created.global_cursor, conversation.global_cursor, resumed.global_cursor] == [1, 2, 3]
    assert created.run_sequence == 1
    assert conversation.run_sequence is None
    assert resumed.run_sequence == 2


def test_stale_run_authority_cannot_append(kernel: KernelFixture) -> None:
    stale = kernel.acquire_then_replace_lease()
    with kernel.database.session() as session, pytest.raises(StaleFenceError):
        kernel.events.append(session, RunLeaseAuthority(stale.fencing_token), EventType.RUN_FAILED, {})
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/unit/test_event_log.py -q`

Expected: FAIL because EventLog and authority types do not exist.

- [ ] **Step 3: Implement one append path**

```python
EventAuthority: TypeAlias = (
    RunCreationAuthority
    | RunLeaseAuthority
    | ConversationCommandAuthority
    | WorkspaceCommandAuthority
)


class EventLog:
    def append(
        self,
        session: Session,
        authority: EventAuthority,
        event_type: EventType | str,
        payload: Mapping[str, JsonValue],
    ) -> PersistedEvent:
        scope = self._authorize(session, authority)
        run_sequence = self._claim_run_sequence(session, scope.run_id)
        row = EventRow(
            event_id=str(uuid4()), scope_type=scope.type, scope_id=scope.id,
            run_id=scope.run_id, sequence_number=run_sequence,
            payload=dict(payload), event_type=str(event_type), created_at=utc_now(),
        )
        session.add(row)
        session.flush()
        return _to_domain(row)
```

Change `EventRow` to use `global_cursor: INTEGER PRIMARY KEY AUTOINCREMENT` and
`event_id: UUID UNIQUE NOT NULL`; keep `sequence_number` nullable and independent. The integer
primary key is the only global cursor mechanism. Do not derive cursor or Run sequence with
`max()+1`. Add `RunRow.next_event_sequence`
and claim it with a version-checked update. Convert `EventRepository` into a compatibility adapter
that delegates to EventLog. Replace `ModelWorkflow._append_event` with EventLog; no workflow may
construct `EventRow` directly.

Add a new `PersistedEvent` domain model with `event_id`, `global_cursor`, scope type/ID, nullable
`run_id`/`sequence_number`, event type string, payload and timestamp. EventLog returns
`PersistedEvent`; the legacy `EventRepository` adapter converts Run-scoped rows to the existing
strict `Event` model so current Runtime/evaluator callers remain compatible during A1.

- [ ] **Step 4: Run GREEN and event regression**

Run: `uv run --frozen pytest tests/unit/test_event_log.py tests/unit/test_model_attempt_queries.py tests/integration/test_runtime.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/persistence/event_log.py src/agentforge/domain/models.py src/agentforge/persistence/tables.py src/agentforge/persistence/repositories.py src/agentforge/persistence/model_workflow.py tests/unit/test_event_log.py
git commit -m "refactor: centralize durable event append"
```

### Task 3: Receipts, Unit of Work and atomic Run bundle

**Files:**
- Create: `src/agentforge/persistence/receipts.py`
- Create: `src/agentforge/persistence/application_uow.py`
- Create: `src/agentforge/application/run_creation.py`
- Modify: `src/agentforge/runtime/engine.py`
- Create: `tests/unit/test_application_receipts.py`
- Create: `tests/integration/test_atomic_run_creation.py`

- [ ] **Step 1: Write idempotency and crash-boundary tests**

```python
def test_same_command_returns_same_complete_run(kernel: ProductKernel) -> None:
    command = start_run_command(command_id=UUID(int=1))
    first = kernel.run_creation.create(command)
    repeated = kernel.run_creation.create(command)
    assert repeated.run_id == first.run_id
    assert kernel.count_complete_run_bundles() == 1


def test_same_id_with_changed_request_is_conflict(kernel: ProductKernel) -> None:
    kernel.run_creation.create(start_run_command(command_id=UUID(int=1), task="one"))
    with pytest.raises(IdempotencyConflictError):
        kernel.run_creation.create(start_run_command(command_id=UUID(int=1), task="two"))


@pytest.mark.parametrize("terminal", [ReceiptStatus.COMPLETED, ReceiptStatus.FAILED, ReceiptStatus.INDETERMINATE])
def test_receipt_terminal_transition_is_atomic(kernel: ProductKernel, terminal: ReceiptStatus) -> None:
    result = kernel.drive_command_to_terminal(start_run_command(), terminal)
    assert result.receipt.status is terminal
    assert result.receipt.updated_at == result.first_terminal_fact_at


@pytest.mark.parametrize("failpoint", RunCreationFailpoint)
def test_crash_leaves_zero_or_one_complete_bundle(kernel: ProductKernel, failpoint: RunCreationFailpoint) -> None:
    with pytest.raises(SimulatedProcessCrash):
        kernel.run_creation.create(start_run_command(), failpoint=failpoint)
    kernel = kernel.reopen()
    assert kernel.bundle_classification() in {"ABSENT", "COMPLETE"}
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/unit/test_application_receipts.py tests/integration/test_atomic_run_creation.py -q`

Expected: FAIL on missing receipt/UoW workflow.

- [ ] **Step 3: Implement transaction-scoped operations**

```python
class ApplicationUnitOfWork:
    def __enter__(self) -> Self:
        self.session = self._database.new_session()
        return self

    def commit(self) -> None:
        self.session.commit()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is not None:
            self.session.rollback()
        self.session.close()


class RunCreationWorkflow:
    def create(self, command: StartRun) -> RunCreationResult:
        with self._uow_factory() as uow:
            receipt = self._receipts.accept(uow.session, command)
            if receipt.result_scope_id is not None:
                return self._load_existing(uow.session, receipt)
            bundle = self._initialize_bundle(uow.session, command, receipt)
            self._events.append(uow.session, RunCreationAuthority(bundle.run_id), EventType.RUN_CREATED, {})
            self._receipts.mark_in_progress(uow.session, receipt.command_id, bundle.run_id)
            uow.commit()
            return bundle

    def initialize_bundle(
        self,
        session: Session,
        command: StartRun | SubmitMessageRun,
        receipt: ReceiptRecord,
    ) -> RunCreationResult:
        return self._initialize_bundle(session, command, receipt)
```

Expose `initialize_bundle(session, ...)` for B so Conversation and Run can share one outer UoW;
it never commits or opens another Session. The outer workflow accepts and binds the Receipt in that
same UoW, then `_initialize_bundle` performs session-bound inserts for Run, RepairState,
ModelRuntimeState, source/config/profile bindings and the initial Event. The method accepts both the standalone
`StartRun` and the persistence-facing `SubmitMessageRun`. It validates the supplied Receipt's
command ID, command type and canonical request digest before any insert. A standalone StartRun
claims a Run-scoped Receipt. `RunCreationWorkflow.initialize_bundle` is the unique owner of the
SubmitMessageRun `MESSAGE_ACCEPTED` persistence fact: for an authoritative ACCEPTED
Conversation-scoped Receipt it appends that Event and atomically claims the Receipt IN_PROGRESS;
for an authoritative IN_PROGRESS Receipt it verifies the unique matching Event and historical
claimed version before reattaching the original Run. It always returns the reloaded authoritative
IN_PROGRESS Receipt. B's outer UoW must not append or claim `MESSAGE_ACCEPTED` again.

`AgentRuntime.create_run()` is an evaluator-only legacy seam. Its existing argument list cannot
losslessly supply workspace/source/config/profile bindings, RepairTaskPolicy or baseline identity,
so it must not synthesize defaults and must not be used by a product entrypoint. The sole current
production caller is evaluator assembly in `pilot_factory`; A2 routes every product StartRun
through `RunCreationWorkflow` and then removes or further isolates this seam.

Implement Receipt primitives `complete`, `fail`, and `mark_indeterminate`, and wire only StartRun in
this task. Its Receipt transition shares the transaction that writes the Run status and Event.
Recovery maps IN_PROGRESS to reattach, safe re-drive, PAUSED or UNKNOWN; it never creates a second
Run. DecideApproval/ResumeRun wiring belongs to Task 5; TrustProfile wiring belongs to Task 7.

- [ ] **Step 4: Run GREEN and approval regression**

Run: `uv run --frozen pytest tests/unit/test_application_receipts.py tests/integration/test_atomic_run_creation.py tests/integration/test_approval_resume.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/persistence/receipts.py src/agentforge/persistence/application_uow.py src/agentforge/application/run_creation.py src/agentforge/runtime/engine.py tests/unit/test_application_receipts.py tests/integration/test_atomic_run_creation.py
git commit -m "feat: create product runs atomically"
```

### Task 4: Deterministic source revision chain and mutation recovery

**Files:**
- Create: `src/agentforge/persistence/source_revisions.py`
- Modify: `src/agentforge/persistence/mutations.py`
- Modify: `src/agentforge/persistence/mutation_workflow.py`
- Modify: `src/agentforge/runtime/mutations.py`
- Modify: `src/agentforge/persistence/tables.py`
- Create: `tests/unit/test_source_revisions.py`
- Create: `tests/integration/test_mutation_revision_recovery.py`

- [ ] **Step 1: Write digest and WRITING recovery tests**

```python
def test_digest_is_sorted_and_excludes_runtime_metadata(tmp_path: Path) -> None:
    write_tree(tmp_path, {"b.py": "b\n", "a.py": "a\n", ".agentforge/state.db": "ignored"})
    first = WorkspaceDigester().digest(tmp_path)
    write_tree(tmp_path, {".agentforge/cache": "ignored too"})
    assert WorkspaceDigester().digest(tmp_path) == first


@pytest.mark.parametrize(
    ("actual", "expected"),
    [("before", MutationRecoveryAction.RETRY), ("after", MutationRecoveryAction.FINALIZE),
     ("partial", MutationRecoveryAction.MARK_INDETERMINATE)],
)
def test_writing_recovery_uses_before_and_expected_after(actual: str, expected: MutationRecoveryAction) -> None:
    assert classify_writing_recovery(actual, before="before", expected_after="after") is expected
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/unit/test_source_revisions.py tests/integration/test_mutation_revision_recovery.py -q`

Expected: FAIL because revision bindings do not exist.

- [ ] **Step 3: Implement versioned digest and mutation journal integration**

```python
class SourceRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: UUID
    initial_source_digest: str
    expected_source_digest: str
    source_revision_number: int = Field(ge=0)


class MutationRecoveryAction(StrEnum):
    RETRY = "RETRY"
    FINALIZE = "FINALIZE"
    MARK_INDETERMINATE = "MARK_INDETERMINATE"
```

Version the digest algorithm and fix path normalization, sort order, `.git`/`.agentforge`/build
exclusions, symlink/reparse rejection, newline byte treatment, max file size and read-error failure.
Persist `before_workspace_digest` and `expected_after_workspace_digest` in MutationExecutionRow.
On commit, update mutation `COMMITTED` and source expected digest/revision in one DB transaction.
Implement the exact RETRY/FINALIZE/INDETERMINATE recovery classification from the design.

- [ ] **Step 4: Run GREEN and mutation regression**

Run: `uv run --frozen pytest tests/unit/test_source_revisions.py tests/integration/test_mutation_revision_recovery.py tests/unit/test_mutation_workflow.py tests/integration/test_mutation_runtime.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/persistence/source_revisions.py src/agentforge/persistence/mutations.py src/agentforge/persistence/mutation_workflow.py src/agentforge/runtime/mutations.py src/agentforge/persistence/tables.py tests/unit/test_source_revisions.py tests/integration/test_mutation_revision_recovery.py
git commit -m "feat: bind mutations to source revisions"
```

### Task 5: Run lease and fencing enforcement

**Files:**
- Create: `src/agentforge/persistence/run_leases.py`
- Modify: `src/agentforge/persistence/application_uow.py`
- Modify: `src/agentforge/persistence/event_log.py`
- Modify: `src/agentforge/runtime/engine.py`
- Modify: `src/agentforge/persistence/approval_workflow.py`
- Modify: `src/agentforge/persistence/mutation_workflow.py`
- Modify: `src/agentforge/persistence/test_execution_workflow.py`
- Modify: `src/agentforge/persistence/repair_workflow.py`
- Modify: `src/agentforge/persistence/model_workflow.py`
- Modify: `src/agentforge/persistence/repositories.py`
- Modify: `src/agentforge/persistence/source_revisions.py`
- Create: `src/agentforge/application/run_driver.py`
- Modify: `src/agentforge/runtime/mutations.py`
- Modify: `src/agentforge/runtime/test_execution.py`
- Modify: `src/agentforge/runtime/repair.py`
- Modify: `src/agentforge/models/executor.py`
- Create: `tests/unit/test_run_leases.py`
- Create: `tests/integration/test_run_fencing.py`

- [ ] **Step 1: Write CAS, expiry, command lease and stale-writer tests**

```python
def test_takeover_increments_fence_and_rejects_old_owner(kernel: ProductKernel) -> None:
    first = kernel.leases.acquire(kernel.run_id, owner_id="one", ttl=timedelta(seconds=1))
    kernel.clock.advance(seconds=2)
    second = kernel.leases.acquire(kernel.run_id, owner_id="two", ttl=timedelta(seconds=30))
    assert second.fencing_token == first.fencing_token + 1
    with pytest.raises(StaleFenceError):
        kernel.runs.mark_running(kernel.run_id, authority=first.authority)


def test_approval_uses_short_command_lease(kernel: ProductKernel) -> None:
    kernel.pause_and_release_execution_lease()
    decided = kernel.approvals.decide(kernel.approval_id, approve=True, command_id=uuid4())
    assert decided.status is ApprovalStatus.APPROVED
    assert kernel.leases.current(kernel.run_id) is None


@pytest.mark.asyncio
async def test_driver_heartbeats_during_long_operation(kernel: ProductKernel) -> None:
    await kernel.driver.run_with_blocked_provider(heartbeat_intervals=3)
    assert kernel.leases.current(kernel.run_id).version >= 4


@pytest.mark.asyncio
async def test_failed_heartbeat_rejects_stale_completion(kernel: ProductKernel) -> None:
    result = await kernel.driver.run_then_lose_lease_during_process()
    assert result.outcome is OutcomeStatus.UNKNOWN
    assert kernel.stale_terminal_write_count == 0


@pytest.mark.parametrize(
    "write_path",
    ["run", "event", "checkpoint", "approval", "mutation_claim", "mutation_finalize",
     "process_claim", "process_finalize", "model_budget", "model_attempt", "repair_state",
     "repair_budget", "diff_validation", "final_verification", "source_revision"],
)
def test_every_critical_write_rejects_stale_fence(kernel: ProductKernel, write_path: str) -> None:
    stale = kernel.acquire_then_replace_lease()
    with pytest.raises(StaleFenceError):
        kernel.invoke_write_path(write_path, authority=stale.authority)
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/unit/test_run_leases.py tests/integration/test_run_fencing.py -q`

Expected: FAIL on missing persistent lease and fence checks.

- [ ] **Step 3: Implement lease state and require authority on critical writes**

```python
class RunLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: UUID
    owner_id: str
    lease_token: UUID
    fencing_token: int = Field(gt=0)
    version: int = Field(gt=0)
    acquired_at: UtcDatetime
    heartbeat_at: UtcDatetime
    expires_at: UtcDatetime
```

Use conditional SQL updates for acquire, renew and release. Require `RunLeaseAuthority` for Run
state, Event, Checkpoint, side-effect claim and terminal writes. Release the execution lease at an
approval pause. `DecideApproval` acquires/releases a short command lease; `ResumeRun` acquires a
new execution lease. A control request never writes a Run terminal state.

`RunDriver` owns an asyncio heartbeat task at less than one third of the lease TTL during Provider
and managed-process waits. Every production caller propagates the current authority into Runtime,
mutation, test, repair and model workflows. If renew fails, the driver starts no new side effect;
an in-flight Provider remains DISPATCHING and an in-flight process remains STARTED until recovery,
so stale completion is rejected and the public outcome becomes UNKNOWN. In this task wire
DecideApproval and ResumeRun Receipt + CAS + Event terminal transitions atomically.

Implement and maintain this write-path matrix; each row must use the same UoW Session and validate
authority before its first mutation:

| Write path | Owning module | Required authority |
|---|---|---|
| Run state/terminal, Checkpoint | `repositories.py` | Run lease |
| Event | `event_log.py` | scope-specific authority |
| Approval CAS | `approval_workflow.py` | command lease |
| Mutation claim/finalize | `mutation_workflow.py` | Run lease |
| Process claim/finalize/unknown | `test_execution_workflow.py` | Run lease |
| Model budget/attempt/retry | `model_workflow.py` | Run lease |
| Repair state/budget/diff/final | `repair_workflow.py` | Run lease |
| Source revision CAS | `source_revisions.py` | Run lease |

- [ ] **Step 4: Run GREEN and cross-process regression**

Run: `uv run --frozen pytest tests/unit/test_run_leases.py tests/integration/test_run_fencing.py tests/integration/test_approval_resume.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/persistence/run_leases.py src/agentforge/persistence/application_uow.py src/agentforge/persistence/event_log.py src/agentforge/persistence/source_revisions.py src/agentforge/application/run_driver.py src/agentforge/runtime/engine.py src/agentforge/runtime/mutations.py src/agentforge/runtime/test_execution.py src/agentforge/runtime/repair.py src/agentforge/models/executor.py src/agentforge/persistence/approval_workflow.py src/agentforge/persistence/mutation_workflow.py src/agentforge/persistence/test_execution_workflow.py src/agentforge/persistence/repair_workflow.py src/agentforge/persistence/model_workflow.py src/agentforge/persistence/repositories.py tests/unit/test_run_leases.py tests/integration/test_run_fencing.py
git commit -m "feat: fence cross process run ownership"
```

### Task 6: Provider attempt journal and safe restart

**Files:**
- Modify: `src/agentforge/domain/enums.py`
- Modify: `src/agentforge/models/domain.py`
- Modify: `src/agentforge/persistence/tables.py`
- Modify: `src/agentforge/persistence/model_workflow.py`
- Modify: `src/agentforge/models/executor.py`
- Modify: `tests/unit/test_model_executor.py`
- Create: `tests/integration/test_model_dispatch_recovery.py`

- [ ] **Step 1: Write PREPARED/DISPATCHING recovery tests**

```python
def test_prepared_attempt_can_be_dispatched_after_restart(model_kernel: ModelKernel) -> None:
    attempt = model_kernel.prepare_attempt()
    assert model_kernel.recover(attempt.attempt_id) is ModelRecoveryAction.DISPATCH


def test_dispatching_attempt_becomes_indeterminate_without_resend(model_kernel: ModelKernel) -> None:
    attempt = model_kernel.mark_dispatching_then_crash()
    assert model_kernel.recover(attempt.attempt_id) is ModelRecoveryAction.MARK_INDETERMINATE
    assert model_kernel.provider.request_count == 0
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/integration/test_model_dispatch_recovery.py -q`

Expected: FAIL because current attempts only distinguish STARTED/COMPLETED/FAILED.

- [ ] **Step 3: Implement attempt transitions around Provider call**

```python
class ModelAttemptStatus(StrEnum):
    PREPARED = "PREPARED"
    DISPATCHING = "DISPATCHING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INDETERMINATE = "INDETERMINATE"
```

Persist PREPARED with request digest, commit DISPATCHING immediately before `provider.generate`,
then persist COMPLETED/FAILED. Recovery from DISPATCHING writes INDETERMINATE and returns an
internal unknown result without calling the Provider. Do not add a `SENT` state or exactly-once
claim.

Update `ModelAttemptRecord` and model-attempt EventType values to the same five-state vocabulary;
remove every STARTED-only Literal so persistence and domain validation cannot disagree.

- [ ] **Step 4: Run GREEN and model regression**

Run: `uv run --frozen pytest tests/integration/test_model_dispatch_recovery.py tests/unit/test_model_executor.py tests/unit/test_model_attempt_queries.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/domain/enums.py src/agentforge/models/domain.py src/agentforge/persistence/tables.py src/agentforge/persistence/model_workflow.py src/agentforge/models/executor.py tests/unit/test_model_executor.py tests/integration/test_model_dispatch_recovery.py
git commit -m "feat: journal uncertain provider dispatch"
```

### Task 7: Trusted Profile and verification source binding

**Files:**
- Create: `src/agentforge/persistence/profile_trust.py`
- Modify: `src/agentforge/tools/testing/profiles.py`
- Modify: `src/agentforge/persistence/test_execution_workflow.py`
- Modify: `src/agentforge/runtime/test_execution.py`
- Modify: `src/agentforge/persistence/tables.py`
- Create: `tests/unit/test_profile_trust.py`
- Modify: `tests/integration/test_final_verification.py`

- [ ] **Step 1: Write trust and final-source-binding tests**

```python
def test_project_profile_requires_exact_digest_trust(profile_kernel: ProfileKernel) -> None:
    challenge = profile_kernel.challenge("visible-tests")
    profile_kernel.trust(challenge, command_id=uuid4())
    assert profile_kernel.resolve_trusted(challenge.profile_id).profile_digest == challenge.profile_digest
    with pytest.raises(ProfileTrustMismatchError):
        profile_kernel.resolve_trusted(challenge.profile_id, argv=("pytest", "different"))


def test_final_verification_rejects_source_change_during_execution(verification_kernel: VerificationKernel) -> None:
    verification_kernel.change_source_after_process_start()
    result = verification_kernel.finish()
    assert result.outcome is OutcomeStatus.UNKNOWN
    assert result.error_code == "SOURCE_REVISION_MISMATCH"
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/unit/test_profile_trust.py tests/integration/test_final_verification.py -q`

Expected: FAIL on missing persisted trust and source execution binding.

- [ ] **Step 3: Implement safe trust identity and verification binding**

```python
class ProfilePurpose(StrEnum):
    DEVELOPMENT = "development"
    VERIFICATION = "verification"
    UTILITY = "utility"


class TrustedProfileIdentity(BaseModel):
    profile_id: str
    profile_version: int
    profile_digest: str
    executable_digest: str
    argv_digest: str
    cwd_identity: str
    config_source_digest: str
```

Persist only identity/digests/enabled timestamps; do not persist environment values or secrets.
Bind ProcessExecutionRow/TestApprovalBindingRow to `source_revision_number` and
`source_revision_digest`. Check actual==expected before launch, bind the start digest, and require
start==recorded==after==current before `VERIFIED`.

TrustProfile uses `WorkspaceCommandAuthority`; Receipt, trust CAS and workspace-scoped Event commit
atomically. Repeating the same command replays the trust fact, while the same command ID with a
different Profile digest returns IDEMPOTENCY_CONFLICT.

- [ ] **Step 4: Run GREEN and security regression**

Run: `uv run --frozen pytest tests/unit/test_profile_trust.py tests/integration/test_final_verification.py tests/security/test_test_profile_security.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/persistence/profile_trust.py src/agentforge/tools/testing/profiles.py src/agentforge/persistence/test_execution_workflow.py src/agentforge/runtime/test_execution.py src/agentforge/persistence/tables.py tests/unit/test_profile_trust.py tests/integration/test_final_verification.py
git commit -m "feat: bind trusted profiles to verified source"
```

### Task 8: A1 recovery matrix and kernel gate

**Files:**
- Create: `src/agentforge/testing/failpoints.py`
- Create: `tests/integration/test_core_failpoints.py`
- Modify: `tests/integration/test_test_execution_recovery.py`

- [ ] **Step 1: Write the three Core failpoint scenarios**

```python
@pytest.mark.parametrize(
    "case",
    [
        CoreFailpointCase.APPROVAL_COMMITTED_BEFORE_ACK,
        CoreFailpointCase.MUTATION_WRITING,
        CoreFailpointCase.LEASE_TAKEOVER_STALE_WRITER,
    ],
)
def test_core_failpoint_has_expected_facts(case: CoreFailpointCase, crash_harness: CrashHarness) -> None:
    crashed = crash_harness.run_worker(case)
    assert crashed.returncode == crash_harness.expected_crash_code
    result = crash_harness.recreate_and_recover(case)
    assert result.actual_classification == result.expected_classification
    assert result.side_effect_count <= 1
    assert result.stale_write_count == 0
```

Add explicit test execution assertions: PREPARED may start, persisted terminal may be consumed,
STARTED with an unconfirmed process tree becomes UNKNOWN and is not re-executed.

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/integration/test_core_failpoints.py tests/integration/test_test_execution_recovery.py -q`

Expected: FAIL because deterministic failpoint injection does not exist.

- [ ] **Step 3: Implement test-only failpoint injection**

```python
class FailpointController(Protocol):
    def hit(self, name: str) -> None: ...


class DisabledFailpoints:
    def hit(self, name: str) -> None:
        return None


class CrashAt:
    def __init__(self, target: str) -> None:
        self._target = target

    def hit(self, name: str) -> None:
        if name == self._target:
            raise SimulatedProcessCrash(name)
```

Inject the interface only at named transaction boundaries; production construction always uses
`DisabledFailpoints`. Persist no test flag in production rows.

- [ ] **Step 4: Run the A1 gate**

Run: `uv run --frozen pytest tests/unit/test_product_schema.py tests/unit/test_event_log.py tests/unit/test_application_receipts.py tests/unit/test_source_revisions.py tests/unit/test_run_leases.py tests/unit/test_profile_trust.py tests/integration/test_atomic_run_creation.py tests/integration/test_mutation_revision_recovery.py tests/integration/test_run_fencing.py tests/integration/test_model_dispatch_recovery.py tests/integration/test_core_failpoints.py -q`

Expected: PASS.

Run: `uv run --frozen pytest -q && uv run --frozen ruff check src tests && uv run --frozen mypy src`

Expected: full suite and all static checks PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/testing/failpoints.py tests/integration/test_core_failpoints.py tests/integration/test_test_execution_recovery.py
git commit -m "test: prove durable kernel recovery boundaries"
```
