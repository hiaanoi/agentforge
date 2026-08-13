from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, TypeAlias, cast
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agentforge.application.contracts import ReceiptStatus
from agentforge.application.kernel_errors import (
    EventAuthorityError,
    EventPayloadError,
    EventPersistenceError,
    StaleFenceError,
)
from agentforge.domain.enums import EventType
from agentforge.domain.models import PersistedEvent, utc_now
from agentforge.domain.strict_json import (
    StrictJsonError,
    copy_and_measure_json_root_mapping,
)
from agentforge.persistence.product_tables import (
    ApplicationCommandReceiptRow,
    ConversationRow,
    RunLeaseRow,
)
from agentforge.persistence.tables import EventRow, RunRow

_CONVERSATION_COMMAND_BY_EVENT = {
    "CONVERSATION_STARTED": frozenset({"START_CONVERSATION"}),
    "MESSAGE_ACCEPTED": frozenset({"SUBMIT_MESSAGE"}),
}
_WORKSPACE_COMMAND_BY_EVENT = {
    "PROFILE_TRUSTED": frozenset({"TRUST_PROFILE"}),
}
def _require_uuid(value: object) -> None:
    if type(value) is not UUID:
        raise EventAuthorityError()


def _require_identity(value: object, *, max_length: int) -> None:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > max_length
    ):
        raise EventAuthorityError()


@dataclass(frozen=True, slots=True)
class RunCreationAuthority:
    run_id: UUID

    def __post_init__(self) -> None:
        _require_uuid(self.run_id)


@dataclass(frozen=True, slots=True)
class RunLeaseAuthority:
    """Ownership epoch plus the latest renew-CAS snapshot.

    ``version`` serializes lease renew/release operations.  Durable Run writes are
    fenced by owner, lease token, fencing token, database expiry, and release state;
    a heartbeat-only version advance does not create a new ownership epoch.
    """

    run_id: UUID
    owner_id: str
    lease_token: UUID
    fencing_token: int
    version: int

    def __post_init__(self) -> None:
        _require_uuid(self.run_id)
        _require_identity(self.owner_id, max_length=200)
        _require_uuid(self.lease_token)
        if (
            type(self.fencing_token) is not int
            or self.fencing_token < 1
            or type(self.version) is not int
            or self.version < 1
        ):
            raise EventAuthorityError()


class RunAuthorityProvider(Protocol):
    """Live ownership view; async callers snapshot only at synchronous writes."""

    @property
    def authority(self) -> RunLeaseAuthority: ...


@dataclass(frozen=True, slots=True)
class ConversationCommandAuthority:
    conversation_id: str
    conversation_version: int
    command_receipt: UUID

    def __post_init__(self) -> None:
        _require_identity(self.conversation_id, max_length=36)
        if type(self.conversation_version) is not int or self.conversation_version < 1:
            raise EventAuthorityError()
        _require_uuid(self.command_receipt)


@dataclass(frozen=True, slots=True)
class WorkspaceCommandAuthority:
    workspace_identity: str
    command_receipt: UUID

    def __post_init__(self) -> None:
        _require_identity(self.workspace_identity, max_length=64)
        _require_uuid(self.command_receipt)


EventAuthority: TypeAlias = (
    RunCreationAuthority
    | RunLeaseAuthority
    | ConversationCommandAuthority
    | WorkspaceCommandAuthority
)
EventScopeType: TypeAlias = Literal["RUN", "CONVERSATION", "WORKSPACE"]


@dataclass(frozen=True, slots=True)
class _EventScope:
    scope_type: EventScopeType
    scope_id: str
    run_id: UUID | None
    creation_only: bool = False
    owner_id: str | None = None
    lease_token: UUID | None = None
    fencing_token: int | None = None
    authorized_at: datetime | None = None


class EventLog:
    """The sole allocator and persistence path for durable product events."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock

    def append(
        self,
        session: Session,
        authority: EventAuthority,
        event_type: EventType | str,
        payload: Mapping[str, JsonValue],
    ) -> PersistedEvent:
        value = self._event_type_value(event_type)
        copied_payload = self._copy_json_payload(payload)
        if type(authority) in {
            ConversationCommandAuthority,
            WorkspaceCommandAuthority,
        }:
            return self._append_command_event_atomically(
                session,
                cast(
                    ConversationCommandAuthority | WorkspaceCommandAuthority,
                    authority,
                ),
                value,
                copied_payload,
            )
        try:
            scope = self._authorize(session, authority, value)
            return self._append(session, scope, value, copied_payload)
        except (EventAuthorityError, StaleFenceError):
            raise
        except SQLAlchemyError:
            raise EventPersistenceError() from None

    def _append_command_event_atomically(
        self,
        session: Session,
        authority: ConversationCommandAuthority | WorkspaceCommandAuthority,
        event_type: str,
        payload: dict[str, JsonValue],
    ) -> PersistedEvent:
        self._ensure_outer_transaction(session)
        try:
            with session.begin_nested():
                scope = self._authorize(session, authority, event_type)
                return self._append(session, scope, event_type, payload)
        except EventAuthorityError:
            raise
        except SQLAlchemyError:
            raise EventPersistenceError() from None

    @staticmethod
    def _ensure_outer_transaction(session: Session) -> None:
        connection = session.connection()
        if connection.dialect.name != "sqlite":
            return
        driver_connection = getattr(connection.connection, "driver_connection", None)
        if not bool(getattr(driver_connection, "in_transaction", False)):
            connection.exec_driver_sql("BEGIN")

    def _append(
        self,
        session: Session,
        scope: _EventScope,
        event_type: EventType | str,
        payload: Mapping[str, JsonValue],
    ) -> PersistedEvent:
        sequence = self._claim_run_sequence(session, scope)
        value = self._event_type_value(event_type)
        row = EventRow(
            schema_version=1,
            event_id=str(uuid4()),
            scope_type=scope.scope_type,
            scope_id=scope.scope_id,
            run_id=str(scope.run_id) if scope.run_id is not None else None,
            sequence_number=sequence,
            event_type=value,
            payload=dict(payload),
            created_at=utc_now(),
        )
        session.add(row)
        session.flush()
        return self._to_domain(row)

    def _authorize(
        self,
        session: Session,
        authority: EventAuthority,
        event_type: str,
    ) -> _EventScope:
        if type(authority) is RunCreationAuthority:
            row = session.get(RunRow, str(authority.run_id))
            if (
                row is None
                or event_type != EventType.RUN_CREATED.value
                or row.next_event_sequence != 1
            ):
                raise EventAuthorityError()
            return _EventScope(
                "RUN",
                str(authority.run_id),
                authority.run_id,
                creation_only=True,
            )
        if type(authority) is RunLeaseAuthority:
            now = self._now(session)
            lease = session.get(RunLeaseRow, str(authority.run_id))
            if (
                lease is None
                or lease.owner_id != authority.owner_id
                or lease.lease_token != str(authority.lease_token)
                or lease.fencing_token != authority.fencing_token
                or lease.released_at is not None
                or lease.expires_at <= now
            ):
                raise StaleFenceError()
            return _EventScope(
                "RUN",
                str(authority.run_id),
                authority.run_id,
                owner_id=authority.owner_id,
                lease_token=authority.lease_token,
                fencing_token=authority.fencing_token,
                authorized_at=now,
            )
        if type(authority) is ConversationCommandAuthority:
            command_types = _CONVERSATION_COMMAND_BY_EVENT.get(event_type)
            if command_types is None:
                raise EventAuthorityError()
            self._claim_command_receipt(
                session,
                authority.command_receipt,
                "CONVERSATION",
                authority.conversation_id,
                command_types,
                conversation_version=authority.conversation_version,
            )
            self._claim_conversation_version(
                session,
                authority.conversation_id,
                authority.conversation_version,
            )
            return _EventScope("CONVERSATION", authority.conversation_id, None)
        if type(authority) is WorkspaceCommandAuthority:
            command_types = _WORKSPACE_COMMAND_BY_EVENT.get(event_type)
            if command_types is None:
                raise EventAuthorityError()
            self._claim_command_receipt(
                session,
                authority.command_receipt,
                "WORKSPACE",
                authority.workspace_identity,
                command_types,
            )
            return _EventScope("WORKSPACE", authority.workspace_identity, None)
        raise EventAuthorityError()

    @staticmethod
    def _event_type_value(event_type: EventType | str) -> str:
        value = event_type.value if type(event_type) is EventType else event_type
        if type(value) is not str or not value:
            raise EventAuthorityError()
        return value

    @staticmethod
    def _copy_json_payload(payload: object) -> dict[str, JsonValue]:
        try:
            copied, _ = copy_and_measure_json_root_mapping(payload)
            return cast(dict[str, JsonValue], copied)
        except EventPayloadError:
            raise
        except StrictJsonError:
            raise EventPayloadError() from None
        except Exception:
            raise EventPayloadError() from None

    @staticmethod
    def _claim_command_receipt(
        session: Session,
        command_receipt: UUID,
        scope_type: str,
        scope_id: str,
        command_types: frozenset[str],
        conversation_version: int | None = None,
    ) -> None:
        statement = update(ApplicationCommandReceiptRow).where(
            ApplicationCommandReceiptRow.command_id == str(command_receipt),
            ApplicationCommandReceiptRow.status == ReceiptStatus.ACCEPTED.value,
            ApplicationCommandReceiptRow.result_scope_type == scope_type,
            ApplicationCommandReceiptRow.result_scope_id == scope_id,
            ApplicationCommandReceiptRow.command_type.in_(command_types),
        )
        if conversation_version is not None:
            statement = statement.where(
                exists().where(
                    ConversationRow.conversation_id == scope_id,
                    ConversationRow.version == conversation_version,
                )
            )
        result = session.execute(
            statement
            .values(
                status=ReceiptStatus.IN_PROGRESS.value,
                updated_at=utc_now(),
            )
            .execution_options(synchronize_session=False)
        )
        if EventLog._affected_rows(result) != 1:
            raise EventAuthorityError()

    @staticmethod
    def _claim_conversation_version(
        session: Session,
        conversation_id: str,
        expected_version: int,
    ) -> None:
        result = session.execute(
            update(ConversationRow)
            .where(
                ConversationRow.conversation_id == conversation_id,
                ConversationRow.version == expected_version,
            )
            .values(version=expected_version + 1)
            .execution_options(synchronize_session=False)
        )
        if EventLog._affected_rows(result) != 1:
            raise EventAuthorityError()
        row = session.get(ConversationRow, conversation_id)
        if row is not None:
            session.expire(row, ["version"])

    @staticmethod
    def _claim_run_sequence(session: Session, scope: _EventScope) -> int | None:
        if scope.run_id is None:
            return None
        run_id = scope.run_id
        for _ in range(100):
            row = session.get(RunRow, str(run_id))
            if row is None:
                raise EventAuthorityError()
            sequence = row.next_event_sequence
            version = row.event_sequence_version
            statement = update(RunRow).where(
                RunRow.run_id == str(run_id),
                RunRow.event_sequence_version == version,
            )
            if scope.creation_only:
                statement = statement.where(RunRow.next_event_sequence == 1)
            if scope.fencing_token is not None:
                statement = statement.where(
                    exists().where(
                        RunLeaseRow.run_id == str(run_id),
                        RunLeaseRow.owner_id == scope.owner_id,
                        RunLeaseRow.lease_token == str(scope.lease_token),
                        RunLeaseRow.fencing_token == scope.fencing_token,
                        RunLeaseRow.released_at.is_(None),
                        RunLeaseRow.expires_at > scope.authorized_at,
                    )
                )
            result = session.execute(
                statement.values(
                    next_event_sequence=sequence + 1,
                    event_sequence_version=version + 1,
                ).execution_options(synchronize_session=False)
            )
            if EventLog._affected_rows(result) == 1:
                session.expire(row, ["next_event_sequence", "event_sequence_version"])
                return sequence
            session.expire(row, ["next_event_sequence", "event_sequence_version"])
            if scope.creation_only and row.next_event_sequence != 1:
                raise EventAuthorityError()
            if scope.fencing_token is not None:
                active = session.scalar(
                    select(func.count()).select_from(RunLeaseRow).where(
                        RunLeaseRow.run_id == str(run_id),
                        RunLeaseRow.owner_id == scope.owner_id,
                        RunLeaseRow.lease_token == str(scope.lease_token),
                        RunLeaseRow.fencing_token == scope.fencing_token,
                        RunLeaseRow.released_at.is_(None),
                        RunLeaseRow.expires_at > scope.authorized_at,
                    )
                )
                if active != 1:
                    raise StaleFenceError()
        raise StaleFenceError()

    def _now(self, session: Session) -> datetime:
        value = (
            self._clock()
            if self._clock is not None
            else session.scalar(func.current_timestamp())
        )
        if type(value) is not datetime:
            raise EventPersistenceError()
        if value.tzinfo is None or value.utcoffset() is None:
            if self._clock is not None:
                raise EventPersistenceError()
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _affected_rows(result: object) -> int:
        value = getattr(result, "rowcount", None)
        return value if isinstance(value, int) else 0

    @staticmethod
    def _to_domain(row: EventRow) -> PersistedEvent:
        if row.schema_version != 1:
            raise RuntimeError("unsupported persisted Event schema")
        return PersistedEvent(
            schema_version=1,
            event_id=UUID(row.event_id),
            global_cursor=row.global_cursor,
            scope_type=cast(EventScopeType, row.scope_type),
            scope_id=row.scope_id,
            run_id=UUID(row.run_id) if row.run_id is not None else None,
            sequence_number=row.sequence_number,
            event_type=row.event_type,
            payload=row.payload,
            created_at=row.created_at,
        )
