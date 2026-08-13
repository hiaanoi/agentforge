from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from types import MappingProxyType
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from agentforge.application.contracts import ReceiptStatus
from agentforge.application.kernel_errors import (
    EventAuthorityError,
    EventPayloadError,
    EventPersistenceError,
    StaleFenceError,
)
from agentforge.domain.enums import EventType
from agentforge.domain.models import PersistedEvent, Run
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import (
    ConversationCommandAuthority,
    EventLog,
    RunCreationAuthority,
    RunLeaseAuthority,
    WorkspaceCommandAuthority,
)
from agentforge.persistence.product_tables import (
    ApplicationCommandReceiptRow,
    ConversationRow,
    RunLeaseRow,
)
from agentforge.persistence.repositories import EventRepository, RunRepository
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.persistence.tables import EventRow, RunRow

NOW = datetime(2026, 8, 10, tzinfo=UTC)
DIGEST = "a" * 64


class _SelfReferentialMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        if key != "self":
            raise KeyError(key)
        return self

    def __iter__(self) -> Iterator[str]:
        return iter(("self",))

    def __len__(self) -> int:
        return 1


class _EmptyThenOversizedMapping(Mapping[str, object]):
    """Returns a different root payload if it is observed a second time."""

    def __init__(self) -> None:
        self.items_calls = 0

    def __getitem__(self, key: str) -> object:
        raise AssertionError("root mapping must be observed through items() only")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("root mapping must be observed through items() only")

    def __len__(self) -> int:
        return 0

    def items(self) -> Iterator[tuple[str, object]]:
        self.items_calls += 1
        if self.items_calls == 1:
            return iter(())
        return iter((("oversized", "x" * 64_001),))


class _DuplicateKeyMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise AssertionError("root mapping must be observed through items() only")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("root mapping must be observed through items() only")

    def __len__(self) -> int:
        return 2

    def items(self) -> Iterator[tuple[str, object]]:
        return iter((("value", 1), ("value", 2)))


class _ExplodingItemsMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise AssertionError("root mapping must be observed through items() only")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("root mapping must be observed through items() only")

    def __len__(self) -> int:
        return 1

    def items(self) -> Iterator[tuple[str, object]]:
        def entries() -> Iterator[tuple[str, object]]:
            yield ("first", 1)
            raise RuntimeError("items iterator failed")

        return entries()


def _database(tmp_path: Path) -> Database:
    database = Database.from_path(tmp_path / "events.sqlite3")
    database.create_schema()
    return database


def _create_run(database: Database) -> Run:
    return RunRepository(database).create(Run(task="event authority probe"))


def _receipt(
    command_id: UUID,
    *,
    scope_type: str,
    scope_id: str,
    command_type: str,
    status: ReceiptStatus = ReceiptStatus.ACCEPTED,
) -> ApplicationCommandReceiptRow:
    return ApplicationCommandReceiptRow(
        command_id=str(command_id),
        command_type=command_type,
        request_digest=DIGEST,
        status=status.value,
        result_scope_type=scope_type,
        result_scope_id=scope_id,
        created_at=NOW,
        updated_at=NOW,
    )


def _lease(run_id: UUID, fencing_token: int = 1) -> RunLeaseRow:
    return RunLeaseRow(
        run_id=str(run_id),
        owner_id="test-owner",
        lease_token=str(UUID(int=fencing_token)),
        fencing_token=fencing_token,
        version=fencing_token,
        acquired_at=NOW,
        heartbeat_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )


def _lease_authority(run_id: UUID, fencing_token: int = 1) -> RunLeaseAuthority:
    return RunLeaseAuthority(
        run_id,
        "test-owner",
        UUID(int=fencing_token),
        fencing_token,
        fencing_token,
    )


def _conversation(conversation_id: str, version: int = 1) -> ConversationRow:
    return ConversationRow(
        conversation_id=conversation_id,
        next_ordinal=1,
        version=version,
    )


def test_event_schema_has_database_cursor_and_independent_run_sequence(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)

    columns = {column.name: column for column in inspect(EventRow).columns}

    assert columns["global_cursor"].primary_key is True
    assert columns["global_cursor"].type.python_type is int
    assert columns["event_id"].primary_key is False
    assert columns["event_id"].nullable is False
    assert columns["event_id"].unique is True
    assert columns["schema_version"].nullable is False
    assert columns["run_id"].nullable is True
    assert columns["sequence_number"].nullable is True
    assert {"scope_type", "scope_id"} <= columns.keys()
    assert EventRow.__table__.dialect_options["sqlite"]["autoincrement"] is True
    database.close()


def test_persisted_event_domain_rejects_invalid_scope_topology() -> None:
    run_id = uuid4()
    base = {
        "schema_version": 1,
        "event_id": uuid4(),
        "global_cursor": 1,
        "scope_type": "RUN",
        "scope_id": str(run_id),
        "run_id": run_id,
        "sequence_number": 1,
        "event_type": "RUN_STARTED",
        "payload": {},
        "created_at": NOW,
    }
    invalid = [
        {**base, "schema_version": True},
        {**base, "scope_type": "UNKNOWN"},
        {**base, "run_id": None},
        {**base, "sequence_number": None},
        {**base, "sequence_number": True},
        {**base, "scope_id": str(uuid4())},
        {
            **base,
            "scope_type": "CONVERSATION",
            "run_id": base["run_id"],
            "sequence_number": None,
        },
        {
            **base,
            "scope_type": "WORKSPACE",
            "run_id": None,
            "sequence_number": 1,
        },
        {**base, "scope_id": ""},
        {**base, "event_type": ""},
    ]
    for candidate in invalid:
        with pytest.raises(ValidationError):
            PersistedEvent.model_validate(candidate)


@pytest.mark.parametrize(
    "overrides",
    [
        {"scope_type": "UNKNOWN", "run_id": None, "sequence_number": None},
        {"run_id": None},
        {"sequence_number": None},
        {"scope_id": "wrong-run"},
        {"scope_type": "CONVERSATION", "sequence_number": None},
        {"scope_type": "WORKSPACE", "run_id": None, "sequence_number": 1},
        {"scope_id": ""},
        {"event_type": ""},
        {"sequence_number": -1},
    ],
)
def test_event_database_rejects_invalid_scope_topology(
    tmp_path: Path,
    overrides: dict[str, object],
) -> None:
    database = _database(tmp_path)
    run = _create_run(database)
    values: dict[str, object] = {
        "schema_version": 1,
        "event_id": str(uuid4()),
        "scope_type": "RUN",
        "scope_id": str(run.run_id),
        "run_id": str(run.run_id),
        "event_type": "RUN_STARTED",
        "sequence_number": 1,
        "payload": {},
        "created_at": NOW,
    }
    values.update(overrides)
    with pytest.raises(IntegrityError), database.session() as session:
        session.add(EventRow(**values))
    database.close()


def test_event_log_orders_scopes_without_run_sequence_collision(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run = _create_run(database)
    conversation_id = "conversation-1"
    command_id = uuid4()
    events = EventLog(clock=lambda: NOW)
    with database.session() as session:
        session.add(_lease(run.run_id))
        session.add(_conversation(conversation_id))
        session.add(
            _receipt(
                command_id,
                scope_type="CONVERSATION",
                scope_id=conversation_id,
                command_type="START_CONVERSATION",
            )
        )
        session.flush()
        created = events.append(
            session,
            RunCreationAuthority(run.run_id),
            EventType.RUN_CREATED,
            {},
        )
        conversation = events.append(
            session,
            ConversationCommandAuthority(conversation_id, 1, command_id),
            "CONVERSATION_STARTED",
            {},
        )
        resumed = events.append(
            session,
            _lease_authority(run.run_id, 1),
            EventType.RUN_RESUMED,
            {},
        )

    assert all(isinstance(event, PersistedEvent) for event in (created, conversation, resumed))
    assert [
        created.schema_version,
        conversation.schema_version,
        resumed.schema_version,
    ] == [1, 1, 1]
    assert [created.global_cursor, conversation.global_cursor, resumed.global_cursor] == [1, 2, 3]
    assert created.sequence_number == 1
    assert conversation.sequence_number is None
    assert resumed.sequence_number == 2
    assert conversation.run_id is None
    assert (conversation.scope_type, conversation.scope_id) == (
        "CONVERSATION",
        conversation_id,
    )
    with database.session() as session:
        row = session.get(RunRow, str(run.run_id))
        conversation_row = session.get(ConversationRow, conversation_id)
        assert row is not None
        assert row.next_event_sequence == 3
        assert row.event_sequence_version == 2
        assert conversation_row is not None and conversation_row.version == 2
    database.close()


def test_stale_run_authority_cannot_append_or_advance_sequence(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run = _create_run(database)
    with database.session() as session:
        session.add(_lease(run.run_id, fencing_token=2))

    with pytest.raises(StaleFenceError, match="stale event authority"):
        with database.session() as session:
            EventLog().append(
                session,
                _lease_authority(run.run_id, 1),
                EventType.RUN_FAILED,
                {},
            )

    with database.session() as session:
        row = session.get(RunRow, str(run.run_id))
        assert row is not None
        assert row.next_event_sequence == 1
        assert session.scalars(select(EventRow)).all() == []
    database.close()


def test_heartbeat_version_is_cas_metadata_not_a_run_write_fence(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run = _create_run(database)
    with database.session() as session:
        lease = _lease(run.run_id)
        lease.version = 12
        session.add(lease)

    with database.session() as session:
        persisted = EventLog(clock=lambda: NOW).append(
            session,
            _lease_authority(run.run_id),
            EventType.RUN_FAILED,
            {},
        )

    assert persisted.event_type == EventType.RUN_FAILED.value
    database.close()


def test_fence_is_rechecked_in_sequence_claim(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run = _create_run(database)
    with database.session() as session:
        session.add(_lease(run.run_id, fencing_token=1))

    class LeaseReplacedAfterAuthorization(EventLog):
        def _authorize(self, session, authority, event_type):  # type: ignore[no-untyped-def]
            scope = super()._authorize(session, authority, event_type)
            lease = session.get(RunLeaseRow, str(run.run_id))
            assert lease is not None
            lease.fencing_token = 2
            lease.version = 2
            session.flush()
            return scope

    with pytest.raises(StaleFenceError, match="stale event authority"):
        with database.session() as session:
            LeaseReplacedAfterAuthorization().append(
                session,
                _lease_authority(run.run_id, 1),
                EventType.RUN_FAILED,
                {},
            )

    with database.session() as session:
        assert session.scalars(select(EventRow)).all() == []
    database.close()


def test_creation_authority_cannot_impersonate_active_run_owner(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run = _create_run(database)

    with pytest.raises(EventAuthorityError, match="event authority rejected"):
        with database.session() as session:
            EventLog().append(
                session,
                RunCreationAuthority(run.run_id),
                EventType.RUN_FAILED,
                {},
            )

    with database.session() as session:
        row = session.get(RunRow, str(run.run_id))
        assert row is not None
        assert row.next_event_sequence == 1
    database.close()


def test_creation_authority_is_single_use_for_initial_event(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run = _create_run(database)
    with database.session() as session:
        EventLog().append(
            session,
            RunCreationAuthority(run.run_id),
            EventType.RUN_CREATED,
            {},
        )
        with pytest.raises(EventAuthorityError, match="event authority rejected"):
            EventLog().append(
                session,
                RunCreationAuthority(run.run_id),
                EventType.RUN_CREATED,
                {},
            )
    database.close()


def test_concurrent_creation_authorities_cannot_both_publish(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run = _create_run(database)
    barrier = Barrier(2)

    class RacingEventLog(EventLog):
        def _authorize(self, session, authority, event_type):  # type: ignore[no-untyped-def]
            scope = super()._authorize(session, authority, event_type)
            barrier.wait()
            return scope

    def append() -> str:
        try:
            with database.session() as session:
                RacingEventLog().append(
                    session,
                    RunCreationAuthority(run.run_id),
                    EventType.RUN_CREATED,
                    {},
                )
        except EventAuthorityError:
            return "REJECTED"
        return "CREATED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: append(), range(2)))

    assert sorted(outcomes) == ["CREATED", "REJECTED"]
    with database.session() as session:
        assert len(session.scalars(select(EventRow)).all()) == 1
    database.close()


def test_workspace_authority_requires_matching_live_receipt_and_never_touches_run(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    run = _create_run(database)
    workspace_identity = "workspace:demo"
    command_id = uuid4()
    with database.session() as session:
        session.add(
            _receipt(
                command_id,
                scope_type="WORKSPACE",
                scope_id=workspace_identity,
                command_type="TRUST_PROFILE",
            )
        )
        event = EventLog().append(
            session,
            WorkspaceCommandAuthority(workspace_identity, command_id),
            "PROFILE_TRUSTED",
            {"profile_id": "tests"},
        )

    assert event.run_id is None
    assert event.sequence_number is None
    assert event.scope_type == "WORKSPACE"
    with database.session() as session:
        row = session.get(RunRow, str(run.run_id))
        assert row is not None
        assert row.next_event_sequence == 1
    database.close()


@pytest.mark.parametrize(
    "authority",
    [
        ConversationCommandAuthority("conversation-1", 1, uuid4()),
        WorkspaceCommandAuthority("workspace:demo", uuid4()),
    ],
)
def test_command_authority_fails_closed_without_matching_receipt(
    tmp_path: Path,
    authority: ConversationCommandAuthority | WorkspaceCommandAuthority,
) -> None:
    database = _database(tmp_path)

    with pytest.raises(EventAuthorityError, match="event authority rejected"):
        with database.session() as session:
            EventLog().append(session, authority, "UNAUTHORIZED_EVENT", {})

    with database.session() as session:
        assert session.scalars(select(EventRow)).all() == []
    database.close()


@pytest.mark.parametrize("authority_version", [1, 3])
def test_conversation_authority_rejects_stale_or_future_version(
    tmp_path: Path,
    authority_version: int,
) -> None:
    database = _database(tmp_path)
    conversation_id = str(uuid4())
    command_id = uuid4()
    with database.session() as session:
        session.add(_conversation(conversation_id, version=2))
        session.add(
            _receipt(
                command_id,
                scope_type="CONVERSATION",
                scope_id=conversation_id,
                command_type="SUBMIT_MESSAGE",
            )
        )

    with pytest.raises(EventAuthorityError, match="event authority rejected"):
        with database.session() as session:
            EventLog().append(
                session,
                ConversationCommandAuthority(conversation_id, authority_version, command_id),
                "MESSAGE_ACCEPTED",
                {},
            )
    database.close()


def test_conversation_authority_rejects_missing_conversation(tmp_path: Path) -> None:
    database = _database(tmp_path)
    conversation_id = str(uuid4())
    command_id = uuid4()
    with database.session() as session:
        session.add(
            _receipt(
                command_id,
                scope_type="CONVERSATION",
                scope_id=conversation_id,
                command_type="SUBMIT_MESSAGE",
            )
        )

    with database.session() as session:
        with pytest.raises(EventAuthorityError, match="event authority rejected"):
            EventLog().append(
                session,
                ConversationCommandAuthority(conversation_id, 1, command_id),
                "MESSAGE_ACCEPTED",
                {},
            )
        conversation = session.get(ConversationRow, conversation_id)
        receipt = session.get(ApplicationCommandReceiptRow, str(command_id))
        assert conversation is None
        assert receipt is not None and receipt.status == ReceiptStatus.ACCEPTED.value
        assert session.scalars(select(EventRow)).all() == []
    database.close()


def test_conversation_receipt_is_claimed_once_in_outer_transaction(tmp_path: Path) -> None:
    database = _database(tmp_path)
    conversation_id = str(uuid4())
    command_id = uuid4()
    authority = ConversationCommandAuthority(conversation_id, 1, command_id)
    with database.session() as session:
        session.add(_conversation(conversation_id))
        session.add(
            _receipt(
                command_id,
                scope_type="CONVERSATION",
                scope_id=conversation_id,
                command_type="SUBMIT_MESSAGE",
            )
        )
        EventLog().append(session, authority, "MESSAGE_ACCEPTED", {})
        with pytest.raises(EventAuthorityError, match="event authority rejected"):
            EventLog().append(session, authority, "MESSAGE_ACCEPTED", {})

    with database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command_id))
        assert receipt is not None
        assert receipt.status == ReceiptStatus.IN_PROGRESS.value
        assert len(session.scalars(select(EventRow)).all()) == 1
    database.close()


def test_conversation_version_is_consumed_and_rejects_second_accepted_receipt(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    conversation_id = str(uuid4())
    first_command, second_command = uuid4(), uuid4()
    with database.session() as session:
        session.add(_conversation(conversation_id))
        session.add_all(
            [
                _receipt(
                    command_id,
                    scope_type="CONVERSATION",
                    scope_id=conversation_id,
                    command_type="SUBMIT_MESSAGE",
                )
                for command_id in (first_command, second_command)
            ]
        )
        EventLog().append(
            session,
            ConversationCommandAuthority(conversation_id, 1, first_command),
            "MESSAGE_ACCEPTED",
            {},
        )
        with pytest.raises(EventAuthorityError, match="event authority rejected"):
            EventLog().append(
                session,
                ConversationCommandAuthority(conversation_id, 1, second_command),
                "MESSAGE_ACCEPTED",
                {},
            )
        conversation = session.get(ConversationRow, conversation_id)
        second = session.get(ApplicationCommandReceiptRow, str(second_command))
        assert conversation is not None and conversation.version == 2
        assert second is not None and second.status == ReceiptStatus.ACCEPTED.value
        assert len(session.scalars(select(EventRow)).all()) == 1
    database.close()


def test_concurrent_conversation_commands_consume_version_once(tmp_path: Path) -> None:
    database = _database(tmp_path)
    conversation_id = str(uuid4())
    commands = (uuid4(), uuid4())
    with database.session() as session:
        session.add(_conversation(conversation_id))
        session.add_all(
            [
                _receipt(
                    command_id,
                    scope_type="CONVERSATION",
                    scope_id=conversation_id,
                    command_type="SUBMIT_MESSAGE",
                )
                for command_id in commands
            ]
        )

    def append(command_id: UUID) -> str:
        try:
            with database.session() as session:
                EventLog().append(
                    session,
                    ConversationCommandAuthority(conversation_id, 1, command_id),
                    "MESSAGE_ACCEPTED",
                    {},
                )
        except EventAuthorityError:
            return "REJECTED"
        return "CREATED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(append, commands))

    assert sorted(outcomes) == ["CREATED", "REJECTED"]
    with database.session() as session:
        conversation = session.get(ConversationRow, conversation_id)
        statuses = {
            session.get(ApplicationCommandReceiptRow, str(command_id)).status  # type: ignore[union-attr]
            for command_id in commands
        }
        assert conversation is not None and conversation.version == 2
        assert statuses == {ReceiptStatus.ACCEPTED.value, ReceiptStatus.IN_PROGRESS.value}
        assert len(session.scalars(select(EventRow)).all()) == 1
    database.close()


def test_event_flush_failure_rolls_back_conversation_and_receipt_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentforge.persistence.event_log as event_log_module

    database = _database(tmp_path)
    conversation_id = str(uuid4())
    command_id = uuid4()
    duplicate_event_id = uuid4()
    with database.session() as session:
        session.add(_conversation(conversation_id))
        session.add(
            _receipt(
                command_id,
                scope_type="CONVERSATION",
                scope_id=conversation_id,
                command_type="SUBMIT_MESSAGE",
            )
        )
        session.add(
            EventRow(
                schema_version=1,
                event_id=str(duplicate_event_id),
                scope_type="WORKSPACE",
                scope_id="existing-workspace",
                run_id=None,
                event_type="PROFILE_TRUSTED",
                sequence_number=None,
                payload={},
                created_at=NOW,
            )
        )

    monkeypatch.setattr(event_log_module, "uuid4", lambda: duplicate_event_id)
    with database.session() as session:
        with pytest.raises(EventPersistenceError, match="event persistence failed"):
            EventLog().append(
                session,
                ConversationCommandAuthority(conversation_id, 1, command_id),
                "MESSAGE_ACCEPTED",
                {},
            )
        conversation = session.get(ConversationRow, conversation_id)
        receipt = session.get(ApplicationCommandReceiptRow, str(command_id))
        assert conversation is not None and conversation.version == 1
        assert receipt is not None and receipt.status == ReceiptStatus.ACCEPTED.value
        assert len(session.scalars(select(EventRow)).all()) == 1
    database.close()


def test_outer_uow_rollback_reverts_conversation_receipt_version_and_event(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    conversation_id = str(uuid4())
    command_id = uuid4()
    with database.session() as session:
        session.add(_conversation(conversation_id))
        session.add(
            _receipt(
                command_id,
                scope_type="CONVERSATION",
                scope_id=conversation_id,
                command_type="SUBMIT_MESSAGE",
            )
        )

    with pytest.raises(RuntimeError, match="rollback outer UoW"):
        with database.session() as session:
            EventLog().append(
                session,
                ConversationCommandAuthority(conversation_id, 1, command_id),
                "MESSAGE_ACCEPTED",
                {},
            )
            raise RuntimeError("rollback outer UoW")

    with database.session() as session:
        conversation = session.get(ConversationRow, conversation_id)
        receipt = session.get(ApplicationCommandReceiptRow, str(command_id))
        assert conversation is not None and conversation.version == 1
        assert receipt is not None and receipt.status == ReceiptStatus.ACCEPTED.value
        assert session.scalars(select(EventRow)).all() == []
    database.close()


@pytest.mark.parametrize(
    ("scope_type", "scope_id", "command_type", "status"),
    [
        ("WORKSPACE", "workspace:wrong", "SUBMIT_MESSAGE", ReceiptStatus.ACCEPTED),
        ("CONVERSATION", "conversation-1", "TRUST_PROFILE", ReceiptStatus.ACCEPTED),
        ("CONVERSATION", "conversation-1", "SUBMIT_MESSAGE", ReceiptStatus.IN_PROGRESS),
        ("CONVERSATION", "conversation-1", "SUBMIT_MESSAGE", ReceiptStatus.COMPLETED),
    ],
)
def test_conversation_authority_rejects_wrong_or_replayed_receipt(
    tmp_path: Path,
    scope_type: str,
    scope_id: str,
    command_type: str,
    status: ReceiptStatus,
) -> None:
    database = _database(tmp_path)
    conversation_id = "conversation-1"
    command_id = uuid4()
    with database.session() as session:
        session.add(_conversation(conversation_id))
        session.add(
            _receipt(
                command_id,
                scope_type=scope_type,
                scope_id=scope_id,
                command_type=command_type,
                status=status,
            )
        )

    with database.session() as session:
        with pytest.raises(EventAuthorityError, match="event authority rejected"):
            EventLog().append(
                session,
                ConversationCommandAuthority(conversation_id, 1, command_id),
                "MESSAGE_ACCEPTED",
                {},
            )
        conversation = session.get(ConversationRow, conversation_id)
        receipt = session.get(ApplicationCommandReceiptRow, str(command_id))
        assert conversation is not None and conversation.version == 1
        assert receipt is not None and receipt.status == status.value
        assert session.scalars(select(EventRow)).all() == []
    database.close()


@pytest.mark.parametrize(
    ("scope_type", "scope_id", "command_type", "status"),
    [
        ("WORKSPACE", "workspace:wrong", "TRUST_PROFILE", ReceiptStatus.ACCEPTED),
        ("WORKSPACE", "workspace:demo", "SUBMIT_MESSAGE", ReceiptStatus.ACCEPTED),
        ("WORKSPACE", "workspace:demo", "TRUST_PROFILE", ReceiptStatus.IN_PROGRESS),
        ("WORKSPACE", "workspace:demo", "TRUST_PROFILE", ReceiptStatus.COMPLETED),
    ],
)
def test_workspace_authority_rejects_wrong_or_replayed_receipt(
    tmp_path: Path,
    scope_type: str,
    scope_id: str,
    command_type: str,
    status: ReceiptStatus,
) -> None:
    database = _database(tmp_path)
    command_id = uuid4()
    with database.session() as session:
        session.add(
            _receipt(
                command_id,
                scope_type=scope_type,
                scope_id=scope_id,
                command_type=command_type,
                status=status,
            )
        )
    with pytest.raises(EventAuthorityError, match="event authority rejected"):
        with database.session() as session:
            EventLog().append(
                session,
                WorkspaceCommandAuthority("workspace:demo", command_id),
                "PROFILE_TRUSTED",
                {},
            )
    database.close()


@pytest.mark.parametrize(
    "event_type",
    ["UNKNOWN_CONVERSATION_EVENT", EventType.RUN_CANCELLED, "PROFILE_TRUSTED"],
)
def test_submit_receipt_cannot_publish_outside_closed_conversation_events(
    tmp_path: Path,
    event_type: EventType | str,
) -> None:
    database = _database(tmp_path)
    run = _create_run(database)
    conversation_id = str(uuid4())
    command_id = uuid4()
    with database.session() as session:
        session.add(_conversation(conversation_id))
        session.add(
            _receipt(
                command_id,
                scope_type="CONVERSATION",
                scope_id=conversation_id,
                command_type="SUBMIT_MESSAGE",
            )
        )
        session.flush()
        with pytest.raises(EventAuthorityError, match="event authority rejected"):
            EventLog().append(
                session,
                ConversationCommandAuthority(conversation_id, 1, command_id),
                event_type,
                {},
            )
        receipt = session.get(ApplicationCommandReceiptRow, str(command_id))
        row = session.get(RunRow, str(run.run_id))
        assert receipt is not None and receipt.status == ReceiptStatus.ACCEPTED.value
        assert row is not None and row.next_event_sequence == 1
        assert session.scalars(select(EventRow)).all() == []
    database.close()


@pytest.mark.parametrize(
    "event_type",
    [
        "UNKNOWN_WORKSPACE_EVENT",
        EventType.RUN_CANCELLED,
        "CONVERSATION_STARTED",
        "MESSAGE_ACCEPTED",
    ],
)
def test_trust_receipt_cannot_publish_outside_closed_workspace_events(
    tmp_path: Path,
    event_type: EventType | str,
) -> None:
    database = _database(tmp_path)
    run = _create_run(database)
    command_id = uuid4()
    with database.session() as session:
        session.add(
            _receipt(
                command_id,
                scope_type="WORKSPACE",
                scope_id="workspace:demo",
                command_type="TRUST_PROFILE",
            )
        )
        session.flush()
        with pytest.raises(EventAuthorityError, match="event authority rejected"):
            EventLog().append(
                session,
                WorkspaceCommandAuthority("workspace:demo", command_id),
                event_type,
                {},
            )
        receipt = session.get(ApplicationCommandReceiptRow, str(command_id))
        row = session.get(RunRow, str(run.run_id))
        assert receipt is not None and receipt.status == ReceiptStatus.ACCEPTED.value
        assert row is not None and row.next_event_sequence == 1
        assert session.scalars(select(EventRow)).all() == []
    database.close()


def test_event_schema_version_is_fixed_in_domain_and_database(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run = _create_run(database)
    with database.session() as session:
        persisted = EventLog().append(
            session,
            RunCreationAuthority(run.run_id),
            EventType.RUN_CREATED,
            {},
        )
    assert persisted.event_type == EventType.RUN_CREATED.value
    with database.session() as session:
        [row] = session.scalars(select(EventRow)).all()
        assert row.schema_version == 1

    with pytest.raises(ValidationError):
        PersistedEvent(
            schema_version=2,
            event_id=uuid4(),
            global_cursor=1,
            scope_type="RUN",
            scope_id=str(run.run_id),
            run_id=run.run_id,
            sequence_number=2,
            event_type="RUN_STARTED",
            payload={},
            created_at=NOW,
        )

    with pytest.raises(IntegrityError), database.session() as session:
        session.add(
            EventRow(
                schema_version=2,
                event_id=str(uuid4()),
                scope_type="RUN",
                scope_id=str(run.run_id),
                run_id=str(run.run_id),
                event_type="RUN_STARTED",
                sequence_number=2,
                payload={},
                created_at=NOW,
            )
        )
    database.close()


def test_event_log_rejects_authority_outside_closed_union(tmp_path: Path) -> None:
    database = _database(tmp_path)

    with pytest.raises(EventAuthorityError, match="event authority rejected"):
        with database.session() as session:
            EventLog().append(session, object(), "SPOOFED_EVENT", {})  # type: ignore[arg-type]

    database.close()


@pytest.mark.parametrize(
    "factory",
    [
        lambda: RunCreationAuthority("not-a-uuid"),  # type: ignore[arg-type]
        lambda: RunLeaseAuthority(uuid4(), "owner", uuid4(), True, 1),
        lambda: RunLeaseAuthority(uuid4(), "owner", uuid4(), 1, 0),
        lambda: RunLeaseAuthority(uuid4(), "", uuid4(), 1, 1),
        lambda: RunLeaseAuthority(uuid4(), "owner", "token", 1, 1),  # type: ignore[arg-type]
        lambda: ConversationCommandAuthority("conversation", True, uuid4()),
        lambda: ConversationCommandAuthority("", 1, uuid4()),
        lambda: ConversationCommandAuthority("x" * 37, 1, uuid4()),
        lambda: ConversationCommandAuthority("conversation", 1, "receipt"),  # type: ignore[arg-type]
        lambda: WorkspaceCommandAuthority(" ", uuid4()),
        lambda: WorkspaceCommandAuthority("x" * 65, uuid4()),
        lambda: WorkspaceCommandAuthority("workspace", "receipt"),  # type: ignore[arg-type]
    ],
)
def test_authority_constructors_reject_bool_and_invalid_identity_boundaries(
    factory,
) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(EventAuthorityError, match="event authority rejected"):
        factory()


def test_event_log_rejects_authority_subclasses(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run = _create_run(database)

    class ForgedRunCreationAuthority(RunCreationAuthority):
        pass

    with pytest.raises(EventAuthorityError, match="event authority rejected"):
        with database.session() as session:
            EventLog().append(
                session,
                ForgedRunCreationAuthority(run.run_id),
                EventType.RUN_CREATED,
                {},
            )
    database.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"value": uuid4()},
        {"value": Decimal("1")},
        {"value": (1, 2)},
        {"value": {1, 2}},
        {"nested": {1: "bad-key"}},
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": float("-inf")},
    ],
)
def test_invalid_payload_fails_before_run_sequence_or_event(
    tmp_path: Path,
    payload: object,
) -> None:
    database = _database(tmp_path)
    run = _create_run(database)
    with pytest.raises(EventPayloadError, match="event payload rejected"):
        with database.session() as session:
            EventLog().append(
                session,
                RunCreationAuthority(run.run_id),
                EventType.RUN_CREATED,
                payload,  # type: ignore[arg-type]
            )
    with database.session() as session:
        row = session.get(RunRow, str(run.run_id))
        assert row is not None and row.next_event_sequence == 1
        assert session.scalars(select(EventRow)).all() == []
    database.close()


def test_cyclic_and_overdeep_payloads_fail_safely_without_side_effects(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    run = _create_run(database)
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    overdeep: object = "leaf"
    for _ in range(66):
        overdeep = [overdeep]

    for payload in (cyclic, {"value": overdeep}):
        with pytest.raises(EventPayloadError, match="event payload rejected"):
            with database.session() as session:
                EventLog().append(
                    session,
                    RunCreationAuthority(run.run_id),
                    EventType.RUN_CREATED,
                    payload,  # type: ignore[arg-type]
                )
    with database.session() as session:
        row = session.get(RunRow, str(run.run_id))
        assert row is not None and row.next_event_sequence == 1
        assert session.scalars(select(EventRow)).all() == []
    database.close()


def test_invalid_payload_precedes_conversation_receipt_and_version_claim(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    conversation_id = str(uuid4())
    command_id = uuid4()
    with database.session() as session:
        session.add(_conversation(conversation_id))
        session.add(
            _receipt(
                command_id,
                scope_type="CONVERSATION",
                scope_id=conversation_id,
                command_type="SUBMIT_MESSAGE",
            )
        )
        with pytest.raises(EventPayloadError, match="event payload rejected") as exc_info:
            EventLog().append(
                session,
                ConversationCommandAuthority(conversation_id, 1, command_id),
                "MESSAGE_ACCEPTED",
                {"private_path": Decimal("1.5")},  # type: ignore[dict-item]
            )
        assert str(exc_info.value) == "event payload rejected"
        conversation = session.get(ConversationRow, conversation_id)
        receipt = session.get(ApplicationCommandReceiptRow, str(command_id))
        assert conversation is not None and conversation.version == 1
        assert receipt is not None and receipt.status == ReceiptStatus.ACCEPTED.value
        assert session.scalars(select(EventRow)).all() == []
    database.close()


def test_payload_is_recursively_copied_before_persistence(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run = _create_run(database)
    nested = [1, {"value": "original"}]
    payload = {"nested": nested}
    with database.session() as session:
        persisted = EventLog().append(
            session,
            RunCreationAuthority(run.run_id),
            EventType.RUN_CREATED,
            payload,
        )
        nested[1]["value"] = "mutated"  # type: ignore[index]
        assert persisted.payload == {"nested": [1, {"value": "original"}]}
    with database.session() as session:
        [row] = session.scalars(select(EventRow)).all()
        assert row.payload == {"nested": [1, {"value": "original"}]}
    database.close()


def test_root_mapping_proxy_is_copied_to_plain_json_payload(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run = _create_run(database)
    source = {"nested": [1, {"value": "original"}]}
    payload = MappingProxyType(source)

    with database.session() as session:
        persisted = EventLog().append(
            session,
            RunCreationAuthority(run.run_id),
            EventType.RUN_CREATED,
            payload,  # type: ignore[arg-type]
        )
        source["nested"][1]["value"] = "mutated"  # type: ignore[index]
        assert type(persisted.payload) is dict
        assert persisted.payload == {"nested": [1, {"value": "original"}]}

    with database.session() as session:
        [row] = session.scalars(select(EventRow)).all()
        assert type(row.payload) is dict
        assert row.payload == {"nested": [1, {"value": "original"}]}
    database.close()


def test_root_mapping_is_observed_once_and_persists_its_first_snapshot(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    run = _create_run(database)
    payload = _EmptyThenOversizedMapping()

    with database.session() as session:
        persisted = EventLog().append(
            session,
            RunCreationAuthority(run.run_id),
            EventType.RUN_CREATED,
            payload,  # type: ignore[arg-type]
        )
        assert persisted.payload == {}

    assert payload.items_calls == 1
    with database.session() as session:
        [row] = session.scalars(select(EventRow)).all()
        assert row.payload == {}
    database.close()


@pytest.mark.parametrize(
    "payload",
    [_DuplicateKeyMapping(), _ExplodingItemsMapping()],
)
def test_unstable_root_mapping_rejection_has_no_run_side_effects(
    tmp_path: Path,
    payload: Mapping[str, object],
) -> None:
    database = _database(tmp_path)
    run = _create_run(database)

    with pytest.raises(EventPayloadError, match="event payload rejected"):
        with database.session() as session:
            EventLog().append(
                session,
                RunCreationAuthority(run.run_id),
                EventType.RUN_CREATED,
                payload,  # type: ignore[arg-type]
            )

    with database.session() as session:
        row = session.get(RunRow, str(run.run_id))
        assert row is not None and row.next_event_sequence == 1
        assert session.scalars(select(EventRow)).all() == []
    database.close()


@pytest.mark.parametrize(
    "payload",
    [
        MappingProxyType({1: "bad-key"}),
        _SelfReferentialMapping(),
        MappingProxyType({"nested": MappingProxyType({"value": "not-json-value"})}),
    ],
)
def test_invalid_root_mappings_fail_without_run_side_effects(
    tmp_path: Path,
    payload: Mapping[object, object],
) -> None:
    database = _database(tmp_path)
    run = _create_run(database)

    with pytest.raises(EventPayloadError, match="event payload rejected"):
        with database.session() as session:
            EventLog().append(
                session,
                RunCreationAuthority(run.run_id),
                EventType.RUN_CREATED,
                payload,  # type: ignore[arg-type]
            )

    with database.session() as session:
        row = session.get(RunRow, str(run.run_id))
        assert row is not None and row.next_event_sequence == 1
        assert session.scalars(select(EventRow)).all() == []
    database.close()


def test_rollback_does_not_publish_event_or_create_identity_collision(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run = _create_run(database)
    with pytest.raises(RuntimeError, match="rollback"):
        with database.session() as session:
            EventLog().append(
                session,
                RunCreationAuthority(run.run_id),
                EventType.RUN_CREATED,
                {},
            )
            raise RuntimeError("rollback")

    with database.session() as session:
        persisted = EventLog().append(
            session,
            RunCreationAuthority(run.run_id),
            EventType.RUN_CREATED,
            {},
        )

    assert persisted.global_cursor > 0
    assert persisted.sequence_number is not None
    assert persisted.sequence_number > 0
    with database.session() as session:
        assert len(session.scalars(select(EventRow)).all()) == 1
    database.close()


def test_created_event_adapter_projects_a_strict_read_model(tmp_path: Path) -> None:
    database = _database(tmp_path)
    run = _create_run(database)
    repository = EventRepository(database)

    appended = repository.append_created(run.run_id, {"source": "creation"})
    [loaded] = repository.list_for_run(run.run_id)

    assert appended == loaded
    assert loaded.event_type is EventType.RUN_CREATED
    assert loaded.sequence_number == 1
    assert loaded.payload == {"source": "creation"}
    database.close()


def test_concurrent_fenced_appends_have_unique_cursor_and_run_sequence(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    run = _create_run(database)
    lease = RunLeaseStore(database).acquire(
        run.run_id,
        owner_id="event-writer",
        ttl=timedelta(seconds=30),
    )

    def append(index: int) -> None:
        with database.session() as session:
            EventLog().append(
                session,
                lease.authority,
                EventType.RUN_STARTED,
                {"index": index},
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(append, range(8)))

    with database.session() as session:
        rows = session.scalars(
            select(EventRow).order_by(EventRow.global_cursor)
        ).all()
    assert len({row.global_cursor for row in rows}) == 8
    sequences = [row.sequence_number for row in rows]
    assert all(sequence is not None and sequence > 0 for sequence in sequences)
    assert len(set(sequences)) == 8
    database.close()


def test_only_event_log_constructs_production_run_event_rows() -> None:
    source_root = Path("src/agentforge")
    direct_event_row = re.compile(r"(?<![A-Za-z0-9_])EventRow\(")
    offenders = []
    for path in source_root.rglob("*.py"):
        if path.name in {"event_log.py", "tables.py"}:
            continue
        if direct_event_row.search(path.read_text(encoding="utf-8")):
            offenders.append(path.relative_to(source_root).as_posix())
    assert offenders == []


def test_legacy_append_seam_is_not_exported_from_persistence_package() -> None:
    import agentforge.persistence as persistence

    assert not hasattr(persistence, "_append_legacy_run_event")


def test_evaluator_repair_provenance_is_confined_to_private_persistence_and_evaluation() -> None:
    source_root = Path("src/agentforge")
    allowed = {
        "evaluation",
        "persistence/approval_workflow.py",
        "persistence/mutation_workflow.py",
        "persistence/repair_terminal.py",
        "persistence/test_execution_workflow.py",
    }
    offenders = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        evaluator_references = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr
            in {
                "EVALUATOR_LEGACY",
                "_evaluator_only_create",
                "_evaluator_only_build",
            }
        ]
        relative = path.relative_to(source_root).as_posix()
        if evaluator_references and not (
            relative.startswith("evaluation/") or relative in allowed
        ):
            offenders.append(relative)
    assert offenders == []


def test_event_log_rejects_wide_payload_before_authorization_or_side_effects() -> None:
    with pytest.raises(EventPayloadError):
        EventLog._copy_json_payload({"values": list(range(10_001))})


def test_event_log_rejects_canonical_json_over_byte_limit() -> None:
    with pytest.raises(EventPayloadError):
        EventLog._copy_json_payload({"values": ["x" * 100 for _ in range(10_000)]})
