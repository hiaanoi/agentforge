from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import JSON, exists, insert, literal, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agentforge.application.kernel_errors import StaleFenceError
from agentforge.domain.enums import (
    ApprovalConsumptionState,
    ApprovalStatus,
    EventType,
    RejectionStrategy,
    RunStatus,
)
from agentforge.domain.errors import (
    ApprovalNotFoundError,
    CheckpointNotFoundError,
    DuplicateApprovalError,
    RunNotFoundError,
)
from agentforge.domain.models import ApprovalRequest, Checkpoint, Event, PersistedEvent, Run
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import EventLog, RunCreationAuthority, RunLeaseAuthority
from agentforge.persistence.run_leases import RunLeaseStore, claim_bound_write
from agentforge.persistence.tables import (
    ApprovalRequestRow,
    CheckpointRow,
    EventRow,
    RunRow,
)


class RunRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database

    def create(self, run: Run) -> Run:
        with self._database.session() as session:
            session.add(self._to_row(run))
        return run

    def get(self, run_id: UUID) -> Run:
        with self._database.session() as session:
            row = session.get(RunRow, str(run_id))
            if row is None:
                raise RunNotFoundError(f"Run {run_id} does not exist")
            return self._to_domain(row)

    def save(self, run: Run, *, authority: RunLeaseAuthority) -> Run:
        with self._database.session() as session:
            claim_bound_write(session, run.run_id, authority)
            now = RunLeaseStore(self._database).now(session)
            conditions = RunLeaseStore.write_conditions(authority, now=now)
            changed = session.execute(
                update(RunRow)
                .where(
                    RunRow.run_id == str(run.run_id),
                    exists().where(*conditions),
                )
                .values(
                    task=run.task,
                    status=run.status.value,
                    current_step=run.current_step,
                    max_steps=run.max_steps,
                    tool_call_count=run.tool_call_count,
                    max_tool_calls=run.max_tool_calls,
                    model_provider=run.model_provider,
                    updated_at=run.updated_at,
                    total_token_usage=run.total_token_usage,
                    estimated_cost=run.estimated_cost,
                    error_message=run.error_message,
                    final_output=run.final_output,
                )
                .execution_options(synchronize_session=False)
            )
            if int(getattr(changed, "rowcount", 0) or 0) != 1:
                if session.get(RunRow, str(run.run_id)) is None:
                    raise RunNotFoundError(f"Run {run.run_id} does not exist")
                raise StaleFenceError()
        return run

    @staticmethod
    def _to_row(run: Run) -> RunRow:
        return RunRow(
            run_id=str(run.run_id),
            task=run.task,
            status=run.status.value,
            current_step=run.current_step,
            max_steps=run.max_steps,
            tool_call_count=run.tool_call_count,
            max_tool_calls=run.max_tool_calls,
            model_provider=run.model_provider,
            created_at=run.created_at,
            updated_at=run.updated_at,
            total_token_usage=run.total_token_usage,
            estimated_cost=run.estimated_cost,
            error_message=run.error_message,
            final_output=run.final_output,
        )

    @staticmethod
    def _to_domain(row: RunRow) -> Run:
        return Run(
            run_id=UUID(row.run_id),
            task=row.task,
            status=RunStatus(row.status),
            current_step=row.current_step,
            max_steps=row.max_steps,
            tool_call_count=row.tool_call_count,
            max_tool_calls=row.max_tool_calls,
            model_provider=row.model_provider,
            created_at=row.created_at,
            updated_at=row.updated_at,
            total_token_usage=row.total_token_usage,
            estimated_cost=row.estimated_cost,
            error_message=row.error_message,
            final_output=row.final_output,
        )


class EventRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        """The immutable transaction domain bound to this repository."""

        return self._database

    def append_created(
        self,
        run_id: UUID,
        payload: dict[str, JsonValue] | None = None,
    ) -> Event:
        with self._database.session() as session:
            persisted = EventLog().append(
                session,
                RunCreationAuthority(run_id),
                EventType.RUN_CREATED,
                payload or {},
            )
            return self._to_legacy_domain(persisted)

    @staticmethod
    def append_in_session(
        session: Session,
        authority: RunLeaseAuthority,
        event_type: EventType,
        payload: dict[str, JsonValue],
    ) -> Event:
        persisted = EventLog().append(session, authority, event_type, payload)
        return EventRepository._to_legacy_domain(persisted)

    def list_for_run(self, run_id: UUID) -> list[Event]:
        with self._database.session() as session:
            rows = session.scalars(
                select(EventRow)
                .where(EventRow.run_id == str(run_id))
                .order_by(EventRow.sequence_number)
            ).all()
            return [self._to_domain(row) for row in rows]

    @staticmethod
    def _to_domain(row: EventRow) -> Event:
        if row.schema_version != 1:
            raise RuntimeError("unsupported persisted Event schema")
        if row.run_id is None or row.sequence_number is None:
            raise RuntimeError("non-Run event cannot be projected as legacy Event")
        return Event(
            event_id=UUID(row.event_id),
            run_id=UUID(row.run_id),
            event_type=EventType(row.event_type),
            sequence_number=row.sequence_number,
            payload=row.payload,
            created_at=row.created_at,
        )

    @staticmethod
    def _to_legacy_domain(event: PersistedEvent) -> Event:
        if event.run_id is None or event.sequence_number is None:
            raise RuntimeError("non-Run event cannot be projected as legacy Event")
        return Event(
            event_id=event.event_id,
            run_id=event.run_id,
            event_type=EventType(event.event_type),
            sequence_number=event.sequence_number,
            payload=event.payload,
            created_at=event.created_at,
        )


class CheckpointRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def save(
        self,
        run_id: UUID,
        step_number: int,
        runtime_state: dict[str, JsonValue],
        *,
        authority: RunLeaseAuthority,
    ) -> Checkpoint:
        checkpoint = Checkpoint(
            run_id=run_id,
            step_number=step_number,
            runtime_state=runtime_state,
        )
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            now = RunLeaseStore(self._database).now(session)
            statement = insert(CheckpointRow).from_select(
                [
                    "checkpoint_id", "run_id", "step_number", "runtime_state", "created_at"
                ],
                select(
                    literal(str(checkpoint.checkpoint_id)),
                    literal(str(checkpoint.run_id)),
                    literal(checkpoint.step_number),
                    literal(checkpoint.runtime_state, type_=JSON),
                    literal(checkpoint.created_at),
                ).where(
                    exists().where(
                        *RunLeaseStore.write_conditions(authority, now=now)
                    )
                ),
            )
            changed = session.execute(statement)
            if int(getattr(changed, "rowcount", 0) or 0) != 1:
                raise StaleFenceError()
        return checkpoint

    def latest(self, run_id: UUID) -> Checkpoint | None:
        with self._database.session() as session:
            row = session.scalar(
                select(CheckpointRow)
                .where(CheckpointRow.run_id == str(run_id))
                .order_by(CheckpointRow.step_number.desc(), CheckpointRow.created_at.desc())
                .limit(1)
            )
            if row is None:
                return None
            return Checkpoint(
                checkpoint_id=UUID(row.checkpoint_id),
                run_id=UUID(row.run_id),
                step_number=row.step_number,
                runtime_state=row.runtime_state,
                created_at=row.created_at,
            )

    def get(self, checkpoint_id: UUID) -> Checkpoint:
        with self._database.session() as session:
            row = session.get(CheckpointRow, str(checkpoint_id))
            if row is None:
                raise CheckpointNotFoundError(
                    f"Checkpoint {checkpoint_id} does not exist"
                )
            return Checkpoint(
                checkpoint_id=UUID(row.checkpoint_id),
                run_id=UUID(row.run_id),
                step_number=row.step_number,
                runtime_state=row.runtime_state,
                created_at=row.created_at,
            )

    def list_for_run(self, run_id: UUID) -> list[Checkpoint]:
        with self._database.session() as session:
            rows = session.scalars(
                select(CheckpointRow)
                .where(CheckpointRow.run_id == str(run_id))
                .order_by(CheckpointRow.step_number, CheckpointRow.created_at)
            ).all()
            return [
                Checkpoint(
                    checkpoint_id=UUID(row.checkpoint_id),
                    run_id=UUID(row.run_id),
                    step_number=row.step_number,
                    runtime_state=row.runtime_state,
                    created_at=row.created_at,
                )
                for row in rows
            ]


class ApprovalRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, approval: ApprovalRequest) -> ApprovalRequest:
        try:
            with self._database.session() as session:
                session.add(self._to_row(approval))
        except IntegrityError as exc:
            raise DuplicateApprovalError(
                "An approval already exists for this Run checkpoint"
            ) from exc
        return approval

    def get(self, approval_id: UUID) -> ApprovalRequest:
        with self._database.session() as session:
            row = session.get(ApprovalRequestRow, str(approval_id))
            if row is None:
                raise ApprovalNotFoundError(
                    f"Approval {approval_id} does not exist"
                )
            return self._to_domain(row)

    def list_pending(self, run_id: UUID | None = None) -> list[ApprovalRequest]:
        statement = select(ApprovalRequestRow).where(
            ApprovalRequestRow.status == ApprovalStatus.PENDING.value
        )
        if run_id is not None:
            statement = statement.where(ApprovalRequestRow.run_id == str(run_id))
        statement = statement.order_by(
            ApprovalRequestRow.requested_at, ApprovalRequestRow.approval_id
        )
        with self._database.session() as session:
            rows = session.scalars(statement).all()
            return [self._to_domain(row) for row in rows]

    def list_for_run(self, run_id: UUID) -> list[ApprovalRequest]:
        with self._database.session() as session:
            rows = session.scalars(
                select(ApprovalRequestRow)
                .where(ApprovalRequestRow.run_id == str(run_id))
                .order_by(ApprovalRequestRow.requested_at, ApprovalRequestRow.approval_id)
            ).all()
            return [self._to_domain(row) for row in rows]

    @staticmethod
    def _to_row(approval: ApprovalRequest) -> ApprovalRequestRow:
        return ApprovalRequestRow(
            approval_id=str(approval.approval_id),
            run_id=str(approval.run_id),
            checkpoint_id=str(approval.checkpoint_id),
            tool_name=approval.tool_name,
            sanitized_arguments=approval.sanitized_arguments,
            request_digest=approval.request_digest,
            status=approval.status.value,
            rejection_strategy=approval.rejection_strategy.value,
            consumption_state=approval.consumption_state.value,
            decision_note=approval.decision_note,
            result_status=approval.result_status,
            result_summary=approval.result_summary,
            requested_at=approval.requested_at,
            decided_at=approval.decided_at,
            consumed_at=approval.consumed_at,
        )

    @staticmethod
    def _to_domain(row: ApprovalRequestRow) -> ApprovalRequest:
        return ApprovalRequest(
            approval_id=UUID(row.approval_id),
            run_id=UUID(row.run_id),
            checkpoint_id=UUID(row.checkpoint_id),
            tool_name=row.tool_name,
            sanitized_arguments=row.sanitized_arguments,
            request_digest=row.request_digest,
            status=ApprovalStatus(row.status),
            rejection_strategy=RejectionStrategy(row.rejection_strategy),
            consumption_state=ApprovalConsumptionState(row.consumption_state),
            decision_note=row.decision_note,
            result_status=row.result_status,
            result_summary=row.result_summary,
            requested_at=row.requested_at,
            decided_at=row.decided_at,
            consumed_at=row.consumed_at,
        )
