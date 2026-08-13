from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from agentforge.application.contracts import (
    ReceiptStatus,
    RunControlRequestStatus,
    RunControlRequestType,
)
from agentforge.application.kernel_errors import InvalidReceiptTransitionError
from agentforge.application.run_commands import CancelRun
from agentforge.domain.models import normalize_utc, utc_now
from agentforge.persistence.application_uow import ApplicationUnitOfWork
from agentforge.persistence.database import Database
from agentforge.persistence.product_tables import RunControlRequestRow
from agentforge.persistence.receipts import ReceiptStore
from agentforge.persistence.tables import RunRow


@dataclass(frozen=True, slots=True)
class RunControlRequest:
    control_request_id: UUID
    run_id: UUID
    command_id: UUID
    request_type: RunControlRequestType
    status: RunControlRequestStatus


class RunControlWorkflow:
    """Command-side cancellation boundary; never writes a Run terminal fact."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._receipts = ReceiptStore()

    def request_cancel(self, command: CancelRun) -> RunControlRequest:
        with ApplicationUnitOfWork(self._database) as uow:
            session = uow.session
            if session.get(RunRow, str(command.run_id)) is None:
                raise InvalidReceiptTransitionError()
            receipt = self._receipts.accept(session, command)
            if receipt.status is ReceiptStatus.COMPLETED:
                row = session.scalar(
                    select(RunControlRequestRow).where(
                        RunControlRequestRow.command_id == str(command.command_id)
                    )
                )
                if row is None or row.run_id != str(command.run_id):
                    raise InvalidReceiptTransitionError()
                result = self._to_domain(row)
                uow.commit()
                return result
            if receipt.status is ReceiptStatus.ACCEPTED:
                self._receipts.mark_in_progress(session, command.command_id, command.run_id)
            elif receipt.status is not ReceiptStatus.IN_PROGRESS:
                raise InvalidReceiptTransitionError()
            request_id = uuid4()
            requested_at = utc_now()
            session.execute(
                sqlite_insert(RunControlRequestRow)
                .values(
                    control_request_id=str(request_id),
                    run_id=str(command.run_id),
                    command_id=str(command.command_id),
                    request_type=RunControlRequestType.CANCEL.value,
                    status=RunControlRequestStatus.REQUESTED.value,
                    requested_at=requested_at,
                    updated_at=requested_at,
                )
                .on_conflict_do_nothing(index_elements=["command_id"])
            )
            row = session.scalar(
                select(RunControlRequestRow).where(
                    RunControlRequestRow.command_id == str(command.command_id)
                )
            )
            if row is None or row.run_id != str(command.run_id):
                raise InvalidReceiptTransitionError()
            self._receipts.complete(session, command.command_id, at=normalize_utc(row.requested_at))
            result = self._to_domain(row)
            uow.commit()
            return result

    @staticmethod
    def _to_domain(row: RunControlRequestRow) -> RunControlRequest:
        return RunControlRequest(
            control_request_id=UUID(row.control_request_id),
            run_id=UUID(row.run_id),
            command_id=UUID(row.command_id),
            request_type=RunControlRequestType(row.request_type),
            status=RunControlRequestStatus(row.status),
        )
