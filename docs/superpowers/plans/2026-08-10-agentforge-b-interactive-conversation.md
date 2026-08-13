# AgentForge B Interactive Conversation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable multi-turn chat, cross-process conversation recovery, explicit local-sensitive history, and honest cooperative cancellation above the existing AgentApplication and Run.

**Architecture:** Conversation/Turn/Message are product projections linked to existing Runs; they never create a second Agent loop. Chat and slash commands call the same typed Application commands/queries as the Core CLI, while cancellation uses persisted control requests consumed by the current lease owner.

**Tech Stack:** Python 3.11+, Pydantic v2, SQLAlchemy/SQLite, asyncio, argparse terminal REPL, pytest subprocess integration.

---

## File Map

- Add Conversation/Turn/Message rows to `src/agentforge/persistence/product_tables.py`.
- Create `src/agentforge/persistence/conversations.py`.
- Extend `application/commands.py`, `queries.py`, `events.py`, `views.py`, `app.py`.
- Create `src/agentforge/application/cancellation.py`.
- Create `src/agentforge/cli/chat.py`; modify CLI parser/main/render.
- Add `tests/unit/test_conversation_domain.py`, `test_conversation_persistence.py`,
  `test_cancellation.py`, `tests/integration/test_conversation_application.py`, and
  `tests/cli/test_chat_recovery.py`.

### Task 1: Conversation persistence and idempotent Turn creation

**Files:**
- Modify: `src/agentforge/persistence/product_tables.py`
- Create: `src/agentforge/persistence/conversations.py`
- Create: `tests/unit/test_conversation_persistence.py`

- [ ] **Step 1: Write ordering and uniqueness tests**

```python
def test_submit_message_creates_one_turn_message_and_run(conversations: ConversationRepository) -> None:
    first = conversations.submit(command_id=CMD, conversation_id=CONV, client_message_id=MSG, content="fix it")
    repeated = conversations.submit(command_id=CMD, conversation_id=CONV, client_message_id=MSG, content="fix it")
    assert repeated == first
    assert conversations.counts(CONV) == {"turns": 1, "messages": 1, "runs": 1}


def test_messages_use_ordinal_not_timestamp(conversations: ConversationRepository) -> None:
    conversations.insert_same_timestamp_messages(CONV, ("one", "two"))
    assert [message.content for message in conversations.history(CONV)] == ["one", "two"]
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/unit/test_conversation_persistence.py -q`

Expected: FAIL because Conversation persistence does not exist.

- [ ] **Step 3: Implement rows and atomic submit operation**

```python
class ConversationRow(Base):
    __tablename__ = "conversations"
    conversation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    next_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class ConversationTurnRow(Base):
    __tablename__ = "conversation_turns"
    __table_args__ = (UniqueConstraint("conversation_id", "client_message_id"), UniqueConstraint("run_id"))
```

Add Message row with unique `(conversation_id, ordinal)`, unique user-message-to-turn relation, and
unique nullable `source_run_id` for assistant final messages. `SubmitMessage` opens one outer UoW
and calls A1 `RunCreationWorkflow.initialize_bundle(session, ...)`; that method never opens or
commits a nested transaction and is the sole creator/verifier of the persisted `MESSAGE_ACCEPTED`
fact and its Receipt claim. B must not append that Event or advance the Conversation version a
second time. Conversation rows, Receipt, Run bundle and initial Events commit once.

- [ ] **Step 4: Run GREEN**

Run: `uv run --frozen pytest tests/unit/test_conversation_persistence.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/persistence/product_tables.py src/agentforge/persistence/conversations.py tests/unit/test_conversation_persistence.py
git commit -m "feat: persist idempotent conversations"
```

### Task 2: Conversation Application commands, events and views

**Files:**
- Modify: `src/agentforge/application/commands.py`
- Modify: `src/agentforge/application/queries.py`
- Modify: `src/agentforge/application/events.py`
- Modify: `src/agentforge/application/views.py`
- Modify: `src/agentforge/application/app.py`
- Create: `tests/integration/test_conversation_application.py`

- [ ] **Step 1: Write start/submit/reconnect tests**

```python
@pytest.mark.asyncio
async def test_conversation_reconnects_to_same_run(app: AgentApplication) -> None:
    started = await collect(app.stream(StartConversation.new(command_id=uuid4())))
    conversation_id = started[-1].scope_id
    submitted = await collect(app.stream(SubmitMessage(
        command_id=uuid4(), client_message_id=uuid4(),
        conversation_id=conversation_id, content="repair",
    )))
    run_id = submitted[-1].run_id
    recreated = recreate_application(app.database_path)
    history = recreated.query(ConversationHistory(conversation_id=conversation_id))
    assert history.turns[0].run_id == run_id
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/integration/test_conversation_application.py -q`

Expected: FAIL on missing conversation command variants.

- [ ] **Step 3: Extend closed unions and projection**

```python
ApplicationCommand = Annotated[
    StartConversation | SubmitMessage | StartRun | ResumeRun | DecideApproval | TrustProfile | CancelRun,
    Field(discriminator="type"),
]
ApplicationQuery = Annotated[
    ConversationHistory | RunDetails | PendingApprovals | DoctorReport | ProfileTrustDetails | ExportRunDetails,
    Field(discriminator="type"),
]
```

StartConversation emits conversation-scoped `conversation_started`; SubmitMessage exposes/emits
`message_accepted` by committing and projecting the existing persisted fact owned by A1
`RunCreationWorkflow.initialize_bundle`—it does not repeat the Receipt claim or Conversation
version increment. It creates one linked Run and projects the unique assistant final message from
that Run. Reconnect uses global cursor and existing Receipt semantics.

- [ ] **Step 4: Run GREEN**

Run: `uv run --frozen pytest tests/integration/test_conversation_application.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/application/commands.py src/agentforge/application/queries.py src/agentforge/application/events.py src/agentforge/application/views.py src/agentforge/application/app.py tests/integration/test_conversation_application.py
git commit -m "feat: expose conversation application contract"
```

### Task 3: Explicit local-sensitive history and safe export

**Files:**
- Modify: `src/agentforge/application/views.py`
- Modify: `src/agentforge/application/projections.py`
- Create: `tests/unit/test_sensitive_conversation_views.py`

- [ ] **Step 1: Write local/export separation tests**

```python
def test_local_history_contains_marked_sensitive_content(projector: ProductProjector) -> None:
    view = projector.local_conversation(conversation_facts("private task"))
    assert view.sensitivity is ViewSensitivity.LOCAL_SENSITIVE
    assert view.messages[0].content == "private task"


def test_export_never_contains_message_content(projector: ProductProjector) -> None:
    exported = projector.export_conversation(conversation_facts("private task"))
    assert "private task" not in exported.model_dump_json()
    PublicArtifactScanner().validate(exported.model_dump_json())
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/unit/test_sensitive_conversation_views.py -q`

Expected: FAIL until view types are separate.

- [ ] **Step 3: Implement distinct view types**

```python
class LocalConversationHistoryView(BaseModel):
    sensitivity: Literal[ViewSensitivity.LOCAL_SENSITIVE]
    conversation_id: UUID
    messages: tuple[LocalMessageView, ...]


class ExportConversationHistoryView(BaseModel):
    sensitivity: Literal[ViewSensitivity.EXPORT_SAFE]
    conversation_id: UUID
    turn_count: int
    run_ids: tuple[UUID, ...]
```

No boolean safety switch is allowed. Product Events remain export-safe; only the explicit local
query returns content.

- [ ] **Step 4: Run GREEN**

Run: `uv run --frozen pytest tests/unit/test_sensitive_conversation_views.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/application/views.py src/agentforge/application/projections.py tests/unit/test_sensitive_conversation_views.py
git commit -m "feat: separate local and export conversation views"
```

### Task 4: Cooperative cancellation state machine

**Files:**
- Create: `src/agentforge/application/cancellation.py`
- Modify: `src/agentforge/runtime/engine.py`
- Modify: `src/agentforge/runtime/test_execution.py`
- Modify: `src/agentforge/process/managed.py`
- Modify: `src/agentforge/persistence/test_execution_workflow.py`
- Modify: `src/agentforge/persistence/event_log.py`
- Modify: `src/agentforge/application/app.py`
- Create: `tests/unit/test_cancellation.py`
- Create: `tests/integration/test_cross_process_cancel.py`

- [ ] **Step 1: Write requested/cancelled/unknown tests**

```python
@pytest.mark.parametrize(
    ("process_state", "expected"),
    [("not_started", CancelStatus.CANCELLED), ("terminated", CancelStatus.CANCELLED),
     ("termination_unconfirmed", CancelStatus.INDETERMINATE)],
)
def test_cancel_never_claims_unconfirmed_termination(process_state: str, expected: CancelStatus) -> None:
    assert cancellation_scenario(process_state).status is expected


def test_repeated_cancel_command_replays_one_control_request(cancel_kernel: CancelKernel) -> None:
    first = cancel_kernel.request(command_id=CMD)
    repeated = cancel_kernel.request(command_id=CMD)
    assert repeated == first
    assert cancel_kernel.control_request_count == 1


def test_stale_executor_cannot_finalize_cancel(cancel_kernel: CancelKernel) -> None:
    stale = cancel_kernel.takeover_after_request()
    with pytest.raises(StaleFenceError):
        cancel_kernel.finalize(authority=stale)
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/unit/test_cancellation.py tests/integration/test_cross_process_cancel.py -q`

Expected: FAIL because control requests are not consumed.

- [ ] **Step 3: Implement cooperative control flow**

```python
class CancelStatus(StrEnum):
    REQUESTED = "REQUESTED"
    CANCELLED = "CANCELLED"
    INDETERMINATE = "INDETERMINATE"
```

CancelRun writes Receipt/control row only. The lease owner checks requests between model/tool steps;
while awaiting a managed test it also races process completion against a bounded control-row poll.
TestExecutionCoordinator asks the owning process supervisor to terminate the exact process tree,
persists termination evidence through the fenced TestExecutionWorkflow, and only then writes
CANCELLED. Lost ownership, poll failure or unconfirmed termination produces UNKNOWN outcome.

Use two explicit transactions. Command-side transaction atomically writes Receipt, control request
and `CANCEL_REQUESTED` Event under command authority. Executor-side transaction, after confirmed
termination, atomically writes control terminal status, Receipt terminal status, `RUN_CANCELLED`
Event and Run terminal under the current fence. If termination is unconfirmed it atomically records
INDETERMINATE/UNKNOWN instead; a stale executor cannot finalize either path.

- [ ] **Step 4: Run GREEN**

Run: `uv run --frozen pytest tests/unit/test_cancellation.py tests/integration/test_cross_process_cancel.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/application/cancellation.py src/agentforge/runtime/engine.py src/agentforge/runtime/test_execution.py src/agentforge/process/managed.py src/agentforge/persistence/test_execution_workflow.py src/agentforge/persistence/event_log.py src/agentforge/application/app.py tests/unit/test_cancellation.py tests/integration/test_cross_process_cancel.py
git commit -m "feat: add honest cooperative cancellation"
```

### Task 5: Chat REPL, slash commands and Interactive gate

**Files:**
- Create: `src/agentforge/cli/chat.py`
- Modify: `src/agentforge/cli/parser.py`
- Modify: `src/agentforge/cli/main.py`
- Modify: `src/agentforge/cli/render.py`
- Create: `tests/cli/test_chat_recovery.py`

- [ ] **Step 1: Write terminal transcript recovery test**

```python
def test_chat_exits_at_approval_and_reopens_same_conversation(cli: CliRunner) -> None:
    first = cli.chat_input("repair the bug\n/exit\n")
    conversation_id = parse_id(first.stdout, "conversation_id")
    approval_id = parse_id(first.stdout, "approval_id")
    cli.run("approve", approval_id)
    resumed = cli.chat_input("/resume\n/exit\n", "--conversation", conversation_id)
    assert "outcome=VERIFIED" in resumed.stdout
```

- [ ] **Step 2: Run RED**

Run: `uv run --frozen pytest tests/cli/test_chat_recovery.py -q`

Expected: FAIL because chat is absent.

- [ ] **Step 3: Implement REPL over AgentApplication only**

```python
SLASH_COMMANDS = frozenset({"/help", "/history", "/inspect", "/approvals", "/resume", "/cancel", "/exit"})
```

Plain text calls SubmitMessage. Slash commands call typed commands/queries; no repository access.
`/history` uses local-sensitive view with an explicit warning. Ctrl-C offers detach or persisted
cancel request only now that cooperative cancellation exists.

- [ ] **Step 4: Run Interactive gate**

Run: `uv run --frozen pytest tests/unit/test_conversation_persistence.py tests/unit/test_sensitive_conversation_views.py tests/unit/test_cancellation.py tests/integration/test_conversation_application.py tests/integration/test_cross_process_cancel.py tests/cli/test_chat_recovery.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/agentforge/cli/chat.py src/agentforge/cli/parser.py src/agentforge/cli/main.py src/agentforge/cli/render.py tests/cli/test_chat_recovery.py
git commit -m "feat: add recoverable AgentForge chat"
```
