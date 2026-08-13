from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agentforge.application.contracts import ReceiptStatus
from agentforge.application.kernel_errors import (
    IdempotencyConflictError,
    InvalidReceiptTransitionError,
    PersistenceBoundaryError,
)
from agentforge.domain.models import normalize_utc, utc_now
from agentforge.persistence.product_tables import ApplicationCommandReceiptRow


class ApplicationCommand(Protocol):
    command_id: UUID

    @property
    def command_type(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ReceiptRecord:
    command_id: UUID
    command_type: str
    request_digest: str
    status: ReceiptStatus
    result_scope_type: str | None
    result_scope_id: str | None
    created_at: datetime
    updated_at: datetime


def request_digest(command: ApplicationCommand) -> str:
    """Hash strict canonical JSON for every semantic command field."""
    if not isinstance(command, BaseModel) or type(command.command_id) is not UUID:
        raise TypeError("application command must be a validated model")
    validated = command.__class__.model_validate(
        command.model_dump(mode="python", warnings=False)
    )
    if validated != command:
        raise ValueError("application command is not canonically validated")
    payload = validated.model_dump(mode="json", exclude={"command_id"})
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ReceiptStore:
    """CAS primitives for the closed application receipt lifecycle."""

    def accept(self, session: Session, command: ApplicationCommand) -> ReceiptRecord:
        try:
            return self._accept(session, command, scope=None)
        except SQLAlchemyError:
            raise PersistenceBoundaryError() from None

    def accept_for_scope(
        self,
        session: Session,
        command: ApplicationCommand,
        *,
        scope_type: str,
        scope_id: str,
    ) -> ReceiptRecord:
        if scope_type not in {"CONVERSATION", "WORKSPACE"} or not scope_id:
            raise InvalidReceiptTransitionError()
        try:
            return self._accept(session, command, scope=(scope_type, scope_id))
        except SQLAlchemyError:
            raise PersistenceBoundaryError() from None

    def _accept(
        self,
        session: Session,
        command: ApplicationCommand,
        *,
        scope: tuple[str, str] | None,
    ) -> ReceiptRecord:
        digest = request_digest(command)
        now = utc_now()
        scope_type, scope_id = scope if scope is not None else (None, None)
        statement = (
            sqlite_insert(ApplicationCommandReceiptRow)
            .values(
                command_id=str(command.command_id),
                command_type=command.command_type,
                request_digest=digest,
                status=ReceiptStatus.ACCEPTED.value,
                result_scope_type=scope_type,
                result_scope_id=scope_id,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["command_id"])
        )
        session.execute(statement)
        session.flush()
        row = session.get(
            ApplicationCommandReceiptRow,
            str(command.command_id),
            populate_existing=True,
        )
        if row is None:
            raise InvalidReceiptTransitionError()
        if row.command_type != command.command_type or row.request_digest != digest:
            raise IdempotencyConflictError()
        if scope is not None and (
            row.result_scope_type != scope_type or row.result_scope_id != scope_id
        ):
            raise InvalidReceiptTransitionError()
        return self._to_record(row)

    def get(self, session: Session, command_id: UUID) -> ReceiptRecord:
        try:
            session.flush()
            row = session.get(
                ApplicationCommandReceiptRow,
                str(command_id),
                populate_existing=True,
            )
        except SQLAlchemyError:
            raise PersistenceBoundaryError() from None
        if row is None:
            raise InvalidReceiptTransitionError()
        return self._to_record(row)

    def mark_in_progress(
        self,
        session: Session,
        command_id: UUID,
        run_id: UUID,
        *,
        at: datetime | None = None,
    ) -> ReceiptRecord:
        try:
            return self._mark_in_progress(
                session, command_id, run_id, at=at
            )
        except SQLAlchemyError:
            raise PersistenceBoundaryError() from None

    def _mark_in_progress(
        self,
        session: Session,
        command_id: UUID,
        run_id: UUID,
        *,
        at: datetime | None = None,
    ) -> ReceiptRecord:
        timestamp = normalize_utc(at or utc_now())
        result = session.execute(
            update(ApplicationCommandReceiptRow)
            .where(
                ApplicationCommandReceiptRow.command_id == str(command_id),
                ApplicationCommandReceiptRow.status == ReceiptStatus.ACCEPTED.value,
                ApplicationCommandReceiptRow.result_scope_type.is_(None),
                ApplicationCommandReceiptRow.result_scope_id.is_(None),
            )
            .values(
                status=ReceiptStatus.IN_PROGRESS.value,
                result_scope_type="RUN",
                result_scope_id=str(run_id),
                updated_at=timestamp,
            )
            .execution_options(synchronize_session=False)
        )
        if self._affected_rows(result) == 1:
            session.flush()
            return self.get(session, command_id)
        existing = self.get(session, command_id)
        if (
            existing.status is ReceiptStatus.IN_PROGRESS
            and existing.result_scope_type == "RUN"
            and existing.result_scope_id == str(run_id)
        ):
            return existing
        raise InvalidReceiptTransitionError()

    def complete(self, session: Session, command_id: UUID, *, at: datetime) -> ReceiptRecord:
        return self._terminal(session, command_id, ReceiptStatus.COMPLETED, at)

    def fail(self, session: Session, command_id: UUID, *, at: datetime) -> ReceiptRecord:
        return self._terminal(session, command_id, ReceiptStatus.FAILED, at)

    def mark_indeterminate(
        self, session: Session, command_id: UUID, *, at: datetime
    ) -> ReceiptRecord:
        return self._terminal(session, command_id, ReceiptStatus.INDETERMINATE, at)

    def _terminal(
        self,
        session: Session,
        command_id: UUID,
        terminal: ReceiptStatus,
        at: datetime,
    ) -> ReceiptRecord:
        try:
            return self._terminal_impl(session, command_id, terminal, at)
        except SQLAlchemyError:
            raise PersistenceBoundaryError() from None

    def _terminal_impl(
        self,
        session: Session,
        command_id: UUID,
        terminal: ReceiptStatus,
        at: datetime,
    ) -> ReceiptRecord:
        timestamp = normalize_utc(at)
        result = session.execute(
            update(ApplicationCommandReceiptRow)
            .where(
                ApplicationCommandReceiptRow.command_id == str(command_id),
                ApplicationCommandReceiptRow.status == ReceiptStatus.IN_PROGRESS.value,
                ApplicationCommandReceiptRow.result_scope_type.is_not(None),
                ApplicationCommandReceiptRow.result_scope_id.is_not(None),
            )
            .values(status=terminal.value, updated_at=timestamp)
            .execution_options(synchronize_session=False)
        )
        if self._affected_rows(result) != 1:
            raise InvalidReceiptTransitionError()
        session.flush()
        return self.get(session, command_id)

    @staticmethod
    def _affected_rows(result: object) -> int:
        value = getattr(result, "rowcount", None)
        return value if isinstance(value, int) else 0

    @staticmethod
    def _to_record(row: ApplicationCommandReceiptRow) -> ReceiptRecord:
        return ReceiptRecord(
            command_id=UUID(row.command_id),
            command_type=row.command_type,
            request_digest=row.request_digest,
            status=ReceiptStatus(row.status),
            result_scope_type=row.result_scope_type,
            result_scope_id=row.result_scope_id,
            created_at=normalize_utc(row.created_at),
            updated_at=normalize_utc(row.updated_at),
        )
