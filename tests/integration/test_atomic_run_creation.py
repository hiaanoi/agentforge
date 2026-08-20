from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from agentforge.application.contracts import ReceiptStatus
from agentforge.application.kernel_errors import (
    EventAuthorityError,
    IdempotencyConflictError,
    IncompleteRunBundleError,
    InvalidReceiptTransitionError,
    PersistenceBoundaryError,
)
from agentforge.application.projections import ProductProjector
from agentforge.application.run_creation import (
    RunCreationFailpoint,
    RunCreationWorkflow,
    SimulatedProcessCrash,
    StartRun,
    SubmitMessageRun,
)
from agentforge.application.views import RunProjectionFacts
from agentforge.domain.models import normalize_utc
from agentforge.domain.repair import (
    BudgetProfile,
    RepairCompletionStatus,
    RepairDifficulty,
    RepairTaskPolicy,
)
from agentforge.models.domain import ModelBudget
from agentforge.persistence.application_uow import ApplicationUnitOfWork
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import (
    ConversationCommandAuthority,
    EventLog,
    RunLeaseAuthority,
)
from agentforge.persistence.product_tables import (
    ApplicationCommandReceiptRow,
    ConversationRow,
    RunLeaseRow,
    WorkspaceSourceBindingRow,
)
from agentforge.persistence.receipts import ReceiptStore
from agentforge.persistence.tables import (
    EventRow,
    ModelRuntimeStateRow,
    RepairStateRow,
    RepairTaskPolicyRow,
    RunRow,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _policy(profile: BudgetProfile = BudgetProfile.BASIC) -> RepairTaskPolicy:
    return RepairTaskPolicy(
        task_id="atomic-task",
        policy_version=1,
        difficulty=RepairDifficulty.BASIC,
        budget_profile=profile,
        allowed_write_paths=("src/**",),
        protected_paths=("tests/**",),
        allowed_development_test_profiles=("unit",),
        final_verification_profile_id="hidden",
        allow_file_creation=False,
        max_created_files=0,
        max_changed_files=2,
        max_total_changed_bytes=4096,
        max_single_file_changed_bytes=2048,
        path_case_sensitive=False,
    )


def _command(
    command_id: UUID | None = None,
    *,
    task: str = "fix",
    profile: BudgetProfile = BudgetProfile.BASIC,
    max_steps: int = 5,
    model_budget: ModelBudget | None = None,
) -> StartRun:
    return StartRun(
        command_id=command_id or uuid4(),
        task=task,
        max_steps=max_steps,
        max_tool_calls=6,
        model_provider="mock",
        model_budget=model_budget or ModelBudget(max_model_requests=4, max_retries=1),
        workspace_root_identity="workspace-1",
        git_head=SHA_A,
        initial_source_digest=SHA_B,
        digest_algorithm_version=1,
        config_digest=SHA_C,
        profile_digest=SHA_A,
        repair_policy=_policy(profile),
        baseline_id=UUID(int=5),
        baseline_digest=SHA_B,
    )


def _workflow(path: Path) -> tuple[Database, RunCreationWorkflow]:
    database = Database.from_path(path)
    database.create_schema()
    return database, RunCreationWorkflow(database, ReceiptStore(), EventLog())


def _submit(command_id: UUID | None = None, *, content: str = "fix") -> SubmitMessageRun:
    start = _command(command_id)
    return SubmitMessageRun(
        **start.model_dump(mode="python"),
        conversation_id=UUID(int=201),
        client_message_id=UUID(int=202),
        content=content,
    )


def _message_payload(command: SubmitMessageRun, version: int = 1) -> dict[str, str | int]:
    return {
        "command_id": str(command.command_id),
        "client_message_id": str(command.client_message_id),
        "message_digest": hashlib.sha256(command.content.encode("utf-8")).hexdigest(),
        "conversation_version": version,
    }


def _counts(database: Database) -> tuple[int, ...]:
    with database.session() as session:
        tables = (
            RunRow,
            RepairTaskPolicyRow,
            RepairStateRow,
            ModelRuntimeStateRow,
            WorkspaceSourceBindingRow,
            ApplicationCommandReceiptRow,
            EventRow,
        )
        return tuple(
            int(session.scalar(select(func.count()).select_from(table)) or 0) for table in tables
        )


def test_same_command_returns_same_complete_bundle(tmp_path: Path) -> None:
    database, workflow = _workflow(tmp_path / "bundle.sqlite3")
    command = _command(UUID(int=1))
    first = workflow.create(command)
    repeated = workflow.create(command)
    assert repeated.run_id == first.run_id
    assert repeated.recovery == "REATTACH_CREATED"
    assert _counts(database) == (1, 1, 1, 1, 1, 1, 1)
    with database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        event = session.scalar(select(EventRow))
        assert receipt is not None and receipt.status == ReceiptStatus.IN_PROGRESS.value
        assert event is not None and event.event_type == "RUN_CREATED"
        assert event.sequence_number == 1
    database.close()


def test_swe_bench_pass1_budget_binding_survives_database_reopen(tmp_path: Path) -> None:
    path = tmp_path / "swe-bench-pass1.sqlite3"
    database, workflow = _workflow(path)
    command = _command(
        UUID(int=901),
        profile=BudgetProfile.SWE_BENCH_PASS1,
        max_steps=80,
        model_budget=ModelBudget(
            max_model_requests=52,
            max_retries=2,
            max_output_tokens_per_request=4096,
            max_total_tokens=600000,
        ),
    )
    created = workflow.create(command)
    database.close()

    reopened = Database.from_path(path)
    with reopened.session() as session:
        policy = session.get(RepairTaskPolicyRow, str(created.run_id))
        run = session.get(RunRow, str(created.run_id))
        model = session.get(ModelRuntimeStateRow, str(created.run_id))
        assert policy is not None and run is not None and model is not None
        assert policy.policy_data["budget_profile"] == BudgetProfile.SWE_BENCH_PASS1.value
        assert tuple(policy.policy_data[field] for field in (
            "max_model_calls",
            "max_read_calls",
            "max_edit_attempts",
            "max_test_runs",
            "max_completion_corrections",
            "max_policy_violations",
            "max_wall_time_seconds",
        )) == (50, 80, 8, 8, 2, 3, 1800)
        assert run.max_steps == 80
        assert model.max_model_requests == 52
        assert model.max_retries == 2
        assert model.max_output_tokens_per_request == 4096
        assert model.max_total_tokens == 600000
    reopened.close()


def test_swe_ablation_budget_binding_survives_database_reopen(tmp_path: Path) -> None:
    path = tmp_path / "swe-ablation.sqlite3"
    database, workflow = _workflow(path)
    command = _command(
        UUID(int=902),
        profile=BudgetProfile.SWE_BENCH_ABLATION_100,
        max_steps=160,
        model_budget=ModelBudget(
            max_model_requests=102,
            max_retries=2,
            max_output_tokens_per_request=4096,
            max_total_tokens=1_200_000,
        ),
    )
    created = workflow.create(command)
    database.close()

    reopened = Database.from_path(path)
    with reopened.session() as session:
        policy = session.get(RepairTaskPolicyRow, str(created.run_id))
        model = session.get(ModelRuntimeStateRow, str(created.run_id))
        assert policy is not None and model is not None
        assert policy.policy_data["budget_profile"] == BudgetProfile.SWE_BENCH_ABLATION_100.value
        assert tuple(policy.policy_data[field] for field in (
            "max_model_calls",
            "max_read_calls",
            "max_edit_attempts",
            "max_test_runs",
            "max_completion_corrections",
            "max_policy_violations",
            "max_wall_time_seconds",
        )) == (100, 160, 16, 16, 4, 6, 3600)
        assert model.max_model_requests == 102
        assert model.max_total_tokens == 1_200_000
    reopened.close()


def test_same_id_with_changed_request_is_conflict(tmp_path: Path) -> None:
    database, workflow = _workflow(tmp_path / "conflict.sqlite3")
    command_id = UUID(int=1)
    workflow.create(_command(command_id, task="one"))
    with pytest.raises(IdempotencyConflictError):
        workflow.create(_command(command_id, task="two"))
    assert _counts(database) == (1, 1, 1, 1, 1, 1, 1)
    database.close()


@pytest.mark.parametrize("failpoint", list(RunCreationFailpoint))
def test_crash_rolls_back_every_persistence_stage(
    tmp_path: Path, failpoint: RunCreationFailpoint
) -> None:
    path = tmp_path / f"crash-{failpoint.value}.sqlite3"
    database, workflow = _workflow(path)
    with pytest.raises(SimulatedProcessCrash):
        workflow.create(_command(), failpoint=failpoint)
    database.close()

    reopened = Database.from_path(path)
    reopened.validate_product_schema()
    assert _counts(reopened) in {(0, 0, 0, 0, 0, 0, 0), (1, 1, 1, 1, 1, 1, 1)}
    reopened.close()


def test_concurrent_same_command_creates_one_bundle_with_independent_connections(
    tmp_path: Path,
) -> None:
    path = tmp_path / "concurrent.sqlite3"
    bootstrap, _ = _workflow(path)
    bootstrap.close()
    command = _command(UUID(int=77))

    def create() -> UUID:
        database = Database.from_path(path)
        workflow = RunCreationWorkflow(database, ReceiptStore(), EventLog())
        try:
            return workflow.create(command).run_id
        finally:
            database.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        run_ids = list(executor.map(lambda _: create(), range(2)))
    assert run_ids[0] == run_ids[1]
    reopened = Database.from_path(path)
    assert _counts(reopened) == (1, 1, 1, 1, 1, 1, 1)
    reopened.close()


@pytest.mark.parametrize(
    ("terminal", "run_status", "event_type", "repair_status"),
    [
        (ReceiptStatus.COMPLETED, "COMPLETED", "RUN_COMPLETED", "UNVERIFIED_FINAL"),
        (ReceiptStatus.FAILED, "FAILED", "RUN_FAILED", "RUNTIME_FAILURE"),
        (ReceiptStatus.INDETERMINATE, "FAILED", "RUN_FAILED", "INDETERMINATE"),
    ],
)
def test_terminal_receipt_run_and_first_fact_share_one_transaction_and_timestamp(
    tmp_path: Path,
    terminal: ReceiptStatus,
    run_status: str,
    event_type: str,
    repair_status: str,
) -> None:
    database, workflow = _workflow(tmp_path / f"terminal-{terminal.value}.sqlite3")
    command = _command()
    created = workflow.create(command)
    now = created.run.created_at
    with database.session() as session:
        session.add(
            RunLeaseRow(
                run_id=str(created.run_id),
                owner_id="worker",
                lease_token=str(UUID(int=20)),
                fencing_token=1,
                version=1,
                acquired_at=now,
                heartbeat_at=now,
                expires_at=now + timedelta(seconds=30),
            )
        )

    terminal_result = workflow.transition_terminal(
        command.command_id,
        terminal,
        authority=RunLeaseAuthority(created.run_id, "worker", UUID(int=20), 1, 1),
    )
    repeated = workflow.transition_terminal(
        command.command_id,
        terminal,
        authority=RunLeaseAuthority(created.run_id, "worker", UUID(int=20), 1, 1),
    )
    assert repeated.run_id == terminal_result.run_id
    with database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        run = session.get(RunRow, str(created.run_id))
        repair = session.get(RepairStateRow, str(created.run_id))
        terminal_events = session.scalars(
            select(EventRow).where(EventRow.event_type == event_type)
        ).all()
        assert receipt is not None and run is not None and repair is not None
        assert receipt.status == terminal.value
        assert run.status == run_status
        assert repair.status == repair_status
        assert len(terminal_events) == 1
        assert (
            normalize_utc(receipt.updated_at)
            == normalize_utc(run.updated_at)
            == normalize_utc(terminal_events[0].created_at)
        )
        facts = RunProjectionFacts(
            run=workflow._run_to_domain(run),
            repair_status=RepairCompletionStatus(repair.status),
            event_count=session.scalar(select(func.count()).select_from(EventRow)) or 0,
            last_cursor=max(event.global_cursor for event in terminal_events),
        )
        assert ProductProjector().export_run(facts).lifecycle_status.value == "TERMINAL"
    database.close()


def test_terminal_replay_fails_closed_when_first_terminal_event_is_missing(
    tmp_path: Path,
) -> None:
    database, workflow = _workflow(tmp_path / "missing-terminal-fact.sqlite3")
    command = _command()
    created = workflow.create(command)
    now = created.run.created_at
    with database.session() as session:
        session.add(
            RunLeaseRow(
                run_id=str(created.run_id),
                owner_id="worker",
                lease_token=str(UUID(int=21)),
                fencing_token=1,
                version=1,
                acquired_at=now,
                heartbeat_at=now,
                expires_at=now + timedelta(seconds=30),
            )
        )
    workflow.transition_terminal(
        command.command_id,
        ReceiptStatus.COMPLETED,
        authority=RunLeaseAuthority(created.run_id, "worker", UUID(int=21), 1, 1),
    )
    with database.session() as session:
        terminal_event = session.scalar(
            select(EventRow).where(EventRow.event_type == "RUN_COMPLETED")
        )
        assert terminal_event is not None
        session.delete(terminal_event)

    with pytest.raises(IncompleteRunBundleError):
        workflow.create(command)
    database.close()


@pytest.mark.parametrize(
    ("run_status", "recovery"),
    [
        ("CREATED", "REATTACH_CREATED"),
        ("RUNNING", "REATTACH_RUNNING"),
        ("WAITING_APPROVAL", "PAUSED"),
        ("PAUSED", "PAUSED"),
        ("COMPLETED", "UNKNOWN"),
        ("FAILED", "UNKNOWN"),
    ],
)
def test_in_progress_recovery_is_classified_from_durable_run_state(
    tmp_path: Path,
    run_status: str,
    recovery: str,
) -> None:
    database, workflow = _workflow(tmp_path / f"recover-{run_status}.sqlite3")
    command = _command()
    created = workflow.create(command)
    with database.session() as session:
        run = session.get(RunRow, str(created.run_id))
        assert run is not None
        run.status = run_status
    replayed = workflow.create(command)
    assert replayed.recovery == recovery
    assert _counts(database) == (1, 1, 1, 1, 1, 1, 1)
    database.close()


@pytest.mark.parametrize("mismatch", ["digest", "type", "command_id", "semantic"])
def test_initialize_bundle_validates_receipt_before_any_bundle_insert(
    tmp_path: Path, mismatch: str
) -> None:
    database, workflow = _workflow(tmp_path / f"receipt-mismatch-{mismatch}.sqlite3")
    store = ReceiptStore()
    command = _command()
    with ApplicationUnitOfWork(database) as uow:
        receipt = store.accept(uow.session, command)
        if mismatch == "digest":
            forged = replace(receipt, request_digest="f" * 64)
        elif mismatch == "type":
            forged = replace(receipt, command_type="OTHER")
        elif mismatch == "command_id":
            forged = replace(receipt, command_id=UUID(int=999))
        else:
            forged = receipt
        supplied_command = (
            command.model_copy(update={"task": "changed semantic task"})
            if mismatch == "semantic"
            else command
        )
        with pytest.raises((IdempotencyConflictError, InvalidReceiptTransitionError)):
            workflow.initialize_bundle(uow.session, supplied_command, forged)
        assert int(uow.session.scalar(select(func.count()).select_from(RunRow)) or 0) == 0
    assert _counts(database) == (0, 0, 0, 0, 0, 0, 0)
    database.close()


@pytest.mark.parametrize("claim_before_bundle", [False, True])
def test_submit_message_uses_same_outer_uow_and_conversation_receipt(
    tmp_path: Path, claim_before_bundle: bool
) -> None:
    path = tmp_path / f"submit-{claim_before_bundle}.sqlite3"
    database, workflow = _workflow(path)
    store = ReceiptStore()
    command = _submit()
    with ApplicationUnitOfWork(database) as uow:
        uow.session.add(
            ConversationRow(
                conversation_id=str(command.conversation_id),
                next_ordinal=1,
                version=1,
            )
        )
        receipt = store.accept_for_scope(
            uow.session,
            command,
            scope_type="CONVERSATION",
            scope_id=str(command.conversation_id),
        )
        if claim_before_bundle:
            EventLog().append(
                uow.session,
                ConversationCommandAuthority(
                    str(command.conversation_id), 1, command.command_id
                ),
                "MESSAGE_ACCEPTED",
                _message_payload(command),
            )
            receipt = store.get(uow.session, command.command_id)
        result = workflow.initialize_bundle(uow.session, command, receipt)
        with pytest.raises(EventAuthorityError):
            EventLog().append(
                uow.session,
                ConversationCommandAuthority(
                    str(command.conversation_id), 1, command.command_id
                ),
                "MESSAGE_ACCEPTED",
                _message_payload(command),
            )
        uow.commit()
    assert result.receipt.status is ReceiptStatus.IN_PROGRESS
    assert result.run_id
    with database.session() as session:
        persisted = store.get(session, command.command_id)
        assert persisted.status is ReceiptStatus.IN_PROGRESS
        assert persisted.result_scope_type == "CONVERSATION"
        assert persisted.result_scope_id == str(command.conversation_id)
    replay_receipt: object
    with ApplicationUnitOfWork(database) as uow:
        replay_receipt = store.accept_for_scope(
            uow.session,
            command,
            scope_type="CONVERSATION",
            scope_id=str(command.conversation_id),
        )
        replayed = workflow.initialize_bundle(uow.session, command, replay_receipt)
        uow.commit()
    assert replayed.run_id == result.run_id
    assert _counts(database) == (1, 1, 1, 1, 1, 1, 2)
    with database.session() as session:
        conversation = session.get(ConversationRow, str(command.conversation_id))
        assert conversation is not None and conversation.version == 2
        assert int(
            session.scalar(
                select(func.count()).select_from(EventRow).where(
                    EventRow.event_type == "MESSAGE_ACCEPTED"
                )
            )
            or 0
        ) == 1
        assert int(
            session.scalar(
                select(func.count()).select_from(EventRow).where(
                    EventRow.event_type == "RUN_CREATED"
                )
            )
            or 0
        ) == 1
    database.close()


def test_submit_message_replays_earlier_claim_after_later_conversation_messages(
    tmp_path: Path,
) -> None:
    database, workflow = _workflow(tmp_path / "submit-historical-replay.sqlite3")
    store = ReceiptStore()
    commands = [
        _submit(UUID(int=310 + index), content=f"message-{index}").model_copy(
            update={"client_message_id": UUID(int=410 + index)}
        )
        for index in range(3)
    ]
    with database.session() as session:
        session.add(
            ConversationRow(
                conversation_id=str(commands[0].conversation_id),
                next_ordinal=1,
                version=1,
            )
        )
    created = []
    for command in commands:
        with ApplicationUnitOfWork(database) as uow:
            receipt = store.accept_for_scope(
                uow.session,
                command,
                scope_type="CONVERSATION",
                scope_id=str(command.conversation_id),
            )
            created.append(workflow.initialize_bundle(uow.session, command, receipt))
            uow.commit()
    before = _counts(database)
    for index in (0, 1):
        command = commands[index]
        with ApplicationUnitOfWork(database) as uow:
            receipt = store.accept_for_scope(
                uow.session,
                command,
                scope_type="CONVERSATION",
                scope_id=str(command.conversation_id),
            )
            replayed = workflow.initialize_bundle(uow.session, command, receipt)
            uow.commit()
        assert replayed.run_id == created[index].run_id
    assert _counts(database) == before == (3, 3, 3, 3, 3, 3, 6)
    with database.session() as session:
        conversation = session.get(ConversationRow, str(commands[0].conversation_id))
        assert conversation is not None and conversation.version == 4
    database.close()


@pytest.mark.parametrize("tamper", ["version_rollback", "missing_event", "duplicate_event"])
def test_submit_message_historical_replay_fails_closed_on_invalid_claim_fact(
    tmp_path: Path, tamper: str
) -> None:
    database, workflow = _workflow(tmp_path / f"submit-claim-{tamper}.sqlite3")
    store = ReceiptStore()
    command = _submit(UUID(int=320))
    with database.session() as session:
        session.add(
            ConversationRow(
                conversation_id=str(command.conversation_id),
                next_ordinal=1,
                version=1,
            )
        )
    with ApplicationUnitOfWork(database) as uow:
        receipt = store.accept_for_scope(
            uow.session,
            command,
            scope_type="CONVERSATION",
            scope_id=str(command.conversation_id),
        )
        workflow.initialize_bundle(uow.session, command, receipt)
        uow.commit()
    with database.session() as session:
        event = session.scalar(
            select(EventRow).where(EventRow.event_type == "MESSAGE_ACCEPTED")
        )
        assert event is not None
        if tamper == "version_rollback":
            conversation = session.get(ConversationRow, str(command.conversation_id))
            assert conversation is not None
            conversation.version = 1
        elif tamper == "missing_event":
            session.delete(event)
        else:
            session.add(
                EventRow(
                    schema_version=event.schema_version,
                    event_id=str(uuid4()),
                    scope_type=event.scope_type,
                    scope_id=event.scope_id,
                    run_id=None,
                    event_type=event.event_type,
                    sequence_number=None,
                    payload=event.payload,
                    created_at=event.created_at,
                )
            )
    before = _counts(database)
    with ApplicationUnitOfWork(database) as uow:
        receipt = store.accept_for_scope(
            uow.session,
            command,
            scope_type="CONVERSATION",
            scope_id=str(command.conversation_id),
        )
        with pytest.raises(IncompleteRunBundleError):
            workflow.initialize_bundle(uow.session, command, receipt)
    assert _counts(database) == before
    database.close()


def test_submit_message_outer_uow_rollback_removes_conversation_receipt_and_bundle(
    tmp_path: Path,
) -> None:
    database, workflow = _workflow(tmp_path / "submit-rollback.sqlite3")
    store = ReceiptStore()
    command = _submit()
    with ApplicationUnitOfWork(database) as uow:
        uow.session.add(
            ConversationRow(
                conversation_id=str(command.conversation_id),
                next_ordinal=1,
                version=1,
            )
        )
        receipt = store.accept_for_scope(
            uow.session,
            command,
            scope_type="CONVERSATION",
            scope_id=str(command.conversation_id),
        )
        workflow.initialize_bundle(uow.session, command, receipt)
    assert _counts(database) == (0, 0, 0, 0, 0, 0, 0)
    with database.session() as session:
        assert session.get(ConversationRow, str(command.conversation_id)) is None
    database.close()


def test_submit_message_rejects_wrong_conversation_scope_before_run_insert(
    tmp_path: Path,
) -> None:
    database, workflow = _workflow(tmp_path / "submit-wrong-conversation.sqlite3")
    store = ReceiptStore()
    command = _submit()
    with ApplicationUnitOfWork(database) as uow:
        receipt = store.accept_for_scope(
            uow.session,
            command,
            scope_type="CONVERSATION",
            scope_id=str(UUID(int=999)),
        )
        with pytest.raises(InvalidReceiptTransitionError):
            workflow.initialize_bundle(uow.session, command, receipt)
        assert int(uow.session.scalar(select(func.count()).select_from(RunRow)) or 0) == 0
    database.close()


def test_submit_message_changed_content_conflicts_without_second_run(
    tmp_path: Path,
) -> None:
    database, workflow = _workflow(tmp_path / "submit-content-conflict.sqlite3")
    store = ReceiptStore()
    command = _submit(UUID(int=301), content="first")
    with ApplicationUnitOfWork(database) as uow:
        uow.session.add(
            ConversationRow(
                conversation_id=str(command.conversation_id),
                next_ordinal=1,
                version=1,
            )
        )
        receipt = store.accept_for_scope(
            uow.session,
            command,
            scope_type="CONVERSATION",
            scope_id=str(command.conversation_id),
        )
        workflow.initialize_bundle(uow.session, command, receipt)
        uow.commit()
    with ApplicationUnitOfWork(database) as uow:
        with pytest.raises(IdempotencyConflictError):
            store.accept_for_scope(
                uow.session,
                _submit(UUID(int=301), content="changed"),
                scope_type="CONVERSATION",
                scope_id=str(command.conversation_id),
            )
    assert _counts(database) == (1, 1, 1, 1, 1, 1, 2)
    database.close()


@pytest.mark.parametrize(
    "forged_change",
    [
        {"status": ReceiptStatus.IN_PROGRESS},
        {"status": ReceiptStatus.COMPLETED},
        {"result_scope_type": "RUN", "result_scope_id": str(UUID(int=777))},
        {"result_scope_id": str(UUID(int=778))},
    ],
)
def test_initialize_bundle_rejects_forged_receipt_state_before_run_side_effects(
    tmp_path: Path,
    forged_change: dict[str, object],
) -> None:
    database, workflow = _workflow(tmp_path / f"forged-{uuid4()}.sqlite3")
    store = ReceiptStore()
    command = _submit()
    with ApplicationUnitOfWork(database) as uow:
        uow.session.add(
            ConversationRow(
                conversation_id=str(command.conversation_id),
                next_ordinal=1,
                version=1,
            )
        )
        persisted = store.accept_for_scope(
            uow.session,
            command,
            scope_type="CONVERSATION",
            scope_id=str(command.conversation_id),
        )
        forged = replace(persisted, **forged_change)
        with pytest.raises(InvalidReceiptTransitionError):
            workflow.initialize_bundle(uow.session, command, forged)
        assert int(uow.session.scalar(select(func.count()).select_from(RunRow)) or 0) == 0
        assert int(
            uow.session.scalar(
                select(func.count()).select_from(EventRow).where(
                    EventRow.event_type == "RUN_CREATED"
                )
            )
            or 0
        ) == 0
    database.close()


def test_initialize_bundle_rejects_stale_accepted_record_after_real_claim(
    tmp_path: Path,
) -> None:
    database, workflow = _workflow(tmp_path / "stale-after-claim.sqlite3")
    store = ReceiptStore()
    command = _submit()
    with ApplicationUnitOfWork(database) as uow:
        uow.session.add(
            ConversationRow(
                conversation_id=str(command.conversation_id),
                next_ordinal=1,
                version=1,
            )
        )
        accepted = store.accept_for_scope(
            uow.session,
            command,
            scope_type="CONVERSATION",
            scope_id=str(command.conversation_id),
        )
        EventLog().append(
            uow.session,
            ConversationCommandAuthority(str(command.conversation_id), 1, command.command_id),
            "MESSAGE_ACCEPTED",
            _message_payload(command),
        )
        claimed = store.get(uow.session, command.command_id)
        assert claimed.status is ReceiptStatus.IN_PROGRESS
        forged_accepted = replace(
            claimed,
            status=ReceiptStatus.ACCEPTED,
            updated_at=accepted.updated_at,
        )
        with pytest.raises(InvalidReceiptTransitionError):
            workflow.initialize_bundle(uow.session, command, forged_accepted)
        conversation = uow.session.get(
            ConversationRow, str(command.conversation_id), populate_existing=True
        )
        assert conversation is not None and conversation.version == 2
        assert int(uow.session.scalar(select(func.count()).select_from(RunRow)) or 0) == 0
        assert int(
            uow.session.scalar(
                select(func.count()).select_from(EventRow).where(
                    EventRow.event_type == "MESSAGE_ACCEPTED"
                )
            )
            or 0
        ) == 1
        assert int(
            uow.session.scalar(
                select(func.count()).select_from(EventRow).where(
                    EventRow.event_type == "RUN_CREATED"
                )
            )
            or 0
        ) == 0
    database.close()


@pytest.mark.parametrize(
    "tamper",
    ["run", "source", "model", "policy", "repair", "initial_event", "extra_event"],
)
def test_replay_fails_closed_on_tampered_bundle_fact(
    tmp_path: Path, tamper: str
) -> None:
    database, workflow = _workflow(tmp_path / f"tamper-{tamper}.sqlite3")
    command = _command()
    created = workflow.create(command)
    with database.session() as session:
        if tamper == "run":
            session.get(RunRow, str(created.run_id)).task = "tampered"  # type: ignore[union-attr]
        elif tamper == "source":
            session.get(WorkspaceSourceBindingRow, str(created.run_id)).config_digest = SHA_B  # type: ignore[union-attr]
        elif tamper == "model":
            session.get(ModelRuntimeStateRow, str(created.run_id)).max_model_requests += 1  # type: ignore[union-attr]
        elif tamper == "policy":
            row = session.get(RepairTaskPolicyRow, str(created.run_id))
            assert row is not None
            row.policy_data = {**row.policy_data, "task_id": "tampered"}
        elif tamper == "repair":
            session.get(RepairStateRow, str(created.run_id)).baseline_digest = SHA_A  # type: ignore[union-attr]
        elif tamper == "initial_event":
            event = session.scalar(
                select(EventRow).where(EventRow.event_type == "RUN_CREATED")
            )
            assert event is not None
            event.payload = {**event.payload, "task_digest": SHA_A}
        else:
            session.add(
                EventRow(
                    schema_version=1,
                    event_id=str(uuid4()),
                    scope_type="RUN",
                    scope_id=str(created.run_id),
                    run_id=str(created.run_id),
                    event_type="RUN_CREATED",
                    sequence_number=2,
                    payload={
                        "task_digest": hashlib.sha256(command.task.encode()).hexdigest(),
                        "command_id": str(command.command_id),
                        "command_type": command.command_type,
                    },
                    created_at=created.run.created_at,
                )
            )
    before = _counts(database)
    with pytest.raises(IncompleteRunBundleError):
        workflow.create(command)
    assert _counts(database) == before
    database.close()


@pytest.mark.parametrize(
    ("field", "tampered_value"),
    [
        ("allow_file_creation", 0),
        ("allow_file_creation", "false"),
        ("max_completion_corrections", True),
        ("policy_version", "1"),
        ("unexpected_policy_fact", "extra"),
    ],
)
def test_replay_strictly_rejects_raw_policy_json_type_drift(
    tmp_path: Path, field: str, tampered_value: object
) -> None:
    database, workflow = _workflow(tmp_path / f"policy-json-{field}-{uuid4()}.sqlite3")
    command = _command()
    created = workflow.create(command)
    raw_policy = command.repair_policy.model_dump(mode="json")
    raw_policy[field] = tampered_value
    with database.session() as session:
        session.execute(
            text(
                "UPDATE repair_task_policies SET policy_data = :policy_data "
                "WHERE run_id = :run_id"
            ),
            {
                "policy_data": json.dumps(raw_policy),
                "run_id": str(created.run_id),
            },
        )
    before = _counts(database)
    with pytest.raises(IncompleteRunBundleError):
        workflow.create(command)
    assert _counts(database) == before
    database.close()


def test_replay_rejects_duplicate_raw_policy_json_key(tmp_path: Path) -> None:
    database, workflow = _workflow(tmp_path / "policy-json-duplicate.sqlite3")
    command = _command()
    created = workflow.create(command)
    canonical = json.dumps(command.repair_policy.model_dump(mode="json"))
    duplicated = canonical.replace(
        "{", '{"task_id":"atomic-task",', 1
    )
    with database.session() as session:
        session.execute(
            text(
                "UPDATE repair_task_policies SET policy_data = :policy_data "
                "WHERE run_id = :run_id"
            ),
            {"policy_data": duplicated, "run_id": str(created.run_id)},
        )
    before = _counts(database)
    with pytest.raises(IncompleteRunBundleError):
        workflow.create(command)
    assert _counts(database) == before
    database.close()


def test_replay_accepts_policy_json_key_order_and_whitespace_changes(
    tmp_path: Path,
) -> None:
    database, workflow = _workflow(tmp_path / "policy-json-formatting.sqlite3")
    command = _command()
    created = workflow.create(command)
    reversed_policy = dict(
        reversed(list(command.repair_policy.model_dump(mode="json").items()))
    )
    with database.session() as session:
        session.execute(
            text(
                "UPDATE repair_task_policies SET policy_data = :policy_data "
                "WHERE run_id = :run_id"
            ),
            {
                "policy_data": json.dumps(reversed_policy, indent=2),
                "run_id": str(created.run_id),
            },
        )
    replayed = workflow.create(command)
    assert replayed.run_id == created.run_id
    assert _counts(database) == (1, 1, 1, 1, 1, 1, 1)
    database.close()


@pytest.mark.parametrize(
    "stored_value",
    [123, 1.25, True, "null", json.dumps([]), "invalid-json"],
    ids=["int", "float", "bool", "null", "list", "invalid-string"],
)
def test_replay_fails_closed_on_non_object_policy_storage_type(
    tmp_path: Path, stored_value: object
) -> None:
    database, workflow = _workflow(tmp_path / f"policy-top-level-{uuid4()}.sqlite3")
    command = _command()
    created = workflow.create(command)
    with database.session() as session:
        session.execute(
            text(
                "UPDATE repair_task_policies SET policy_data = :policy_data "
                "WHERE run_id = :run_id"
            ),
            {"policy_data": stored_value, "run_id": str(created.run_id)},
        )
    before_counts = _counts(database)
    with database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        event = session.scalar(select(EventRow))
        assert receipt is not None and event is not None
        before_receipt = (
            receipt.status,
            receipt.result_scope_type,
            receipt.result_scope_id,
            receipt.updated_at,
        )
        before_event = (
            event.event_id,
            event.event_type,
            event.payload.copy(),
            event.created_at,
        )
    with pytest.raises(IncompleteRunBundleError) as raised:
        workflow.create(command)
    assert str(raised.value) == "persisted Run bundle is incomplete"
    assert _counts(database) == before_counts
    with database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        event = session.scalar(select(EventRow))
        assert receipt is not None and event is not None
        assert (
            receipt.status,
            receipt.result_scope_type,
            receipt.result_scope_id,
            receipt.updated_at,
        ) == before_receipt
        assert (
            event.event_id,
            event.event_type,
            event.payload,
            event.created_at,
        ) == before_event
    database.close()


def test_locked_database_maps_to_safe_boundary_error_and_recovers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "locked.sqlite3"
    owner, _ = _workflow(path)
    contender = Database.from_path(path)
    workflow = RunCreationWorkflow(contender, ReceiptStore(), EventLog())
    command = _command()
    with ApplicationUnitOfWork(owner):
        with pytest.raises(PersistenceBoundaryError) as raised:
            workflow.create(command)
        assert str(raised.value) == "kernel persistence operation failed"
        assert str(path) not in str(raised.value)
    created = workflow.create(command)
    assert created.run_id
    owner.close()
    contender.close()
