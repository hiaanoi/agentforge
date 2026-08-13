from pathlib import Path
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Update

from agentforge.domain.enums import (
    EvaluationAttemptStatus,
    EvaluationCampaignStatus,
    EvaluationSlotStatus,
    EventType,
)
from agentforge.domain.models import utc_now
from agentforge.evaluation.campaign_models import (
    EvaluationCampaign,
    EvaluationCampaignEvent,
    EvaluationPilotAttempt,
    EvaluationSlot,
)
from agentforge.evaluation.protocol import EvaluationProtocol
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.tables import (
    EvaluationCampaignEventRow,
    EvaluationCampaignRow,
    EvaluationPilotAttemptRow,
    EvaluationSlotRow,
)

CAMPAIGN_TERMINAL = frozenset(
    {
        EvaluationCampaignStatus.COMPLETED,
        EvaluationCampaignStatus.COMPLETED_WITH_INVALID,
        EvaluationCampaignStatus.BLOCKED,
        EvaluationCampaignStatus.INDETERMINATE,
    }
)
SLOT_TERMINAL = frozenset(
    {
        EvaluationSlotStatus.ACCEPTED,
        EvaluationSlotStatus.INVALID,
        EvaluationSlotStatus.INDETERMINATE,
    }
)
ATTEMPT_TERMINAL = frozenset(
    {
        EvaluationAttemptStatus.COMPLETED,
        EvaluationAttemptStatus.INVALID,
        EvaluationAttemptStatus.INDETERMINATE,
    }
)


class CampaignConflictError(RuntimeError):
    pass


class EvaluationCampaignRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database

    def create_campaign(
        self,
        protocol: EvaluationProtocol,
        *,
        campaign_id: UUID | None = None,
    ) -> EvaluationCampaign:
        requested_id = campaign_id or uuid4()
        now = utc_now()
        requested = EvaluationCampaign(
            campaign_id=requested_id,
            protocol_digest=protocol.protocol_digest,
            task_id=protocol.task_id,
            repetition_count=protocol.repetition_count,
            created_at=now,
            updated_at=now,
        )
        try:
            with self._database.session() as session:
                existing = session.scalar(
                    select(EvaluationCampaignRow).where(
                        EvaluationCampaignRow.protocol_digest
                        == protocol.protocol_digest
                    )
                )
                if existing is not None:
                    persisted = self._campaign(existing)
                    if (
                        (campaign_id is not None and persisted.campaign_id != campaign_id)
                        or persisted.protocol_digest != protocol.protocol_digest
                        or persisted.task_id != protocol.task_id
                        or persisted.repetition_count != protocol.repetition_count
                    ):
                        raise CampaignConflictError(
                            "Campaign protocol is bound to different facts"
                        )
                    return persisted
                session.add(self._campaign_row(requested))
                session.flush()
                for repetition_index in range(protocol.repetition_count):
                    slot = EvaluationSlot(
                        campaign_id=requested.campaign_id,
                        protocol_digest=protocol.protocol_digest,
                        task_id=protocol.task_id,
                        repetition_index=repetition_index,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(self._slot_row(slot))
                self._append_event(
                    session,
                    requested.campaign_id,
                    EventType.EVALUATION_CAMPAIGN_CREATED,
                    {
                        "protocol_digest": protocol.protocol_digest,
                        "task_id": protocol.task_id,
                        "repetition_count": protocol.repetition_count,
                    },
                )
        except IntegrityError as exc:
            raise CampaignConflictError("Campaign creation conflicted") from exc
        return requested

    def get_campaign(self, campaign_id: UUID) -> EvaluationCampaign:
        with self._database.session() as session:
            return self._campaign(self._require_campaign(session, campaign_id))

    def list_slots(self, campaign_id: UUID) -> list[EvaluationSlot]:
        with self._database.session() as session:
            rows = session.scalars(
                select(EvaluationSlotRow)
                .where(EvaluationSlotRow.campaign_id == str(campaign_id))
                .order_by(EvaluationSlotRow.repetition_index)
            ).all()
            return [self._slot(row) for row in rows]

    def get_slot(self, slot_id: UUID) -> EvaluationSlot:
        with self._database.session() as session:
            return self._slot(self._require_slot(session, slot_id))

    def get_attempt(self, attempt_id: UUID) -> EvaluationPilotAttempt:
        with self._database.session() as session:
            return self._attempt(self._require_attempt(session, attempt_id))

    def list_attempts(self, slot_id: UUID) -> list[EvaluationPilotAttempt]:
        with self._database.session() as session:
            rows = session.scalars(
                select(EvaluationPilotAttemptRow)
                .where(EvaluationPilotAttemptRow.slot_id == str(slot_id))
                .order_by(EvaluationPilotAttemptRow.attempt_number)
            ).all()
            return [self._attempt(row) for row in rows]

    def list_events(self, campaign_id: UUID) -> list[EvaluationCampaignEvent]:
        with self._database.session() as session:
            rows = session.scalars(
                select(EvaluationCampaignEventRow)
                .where(EvaluationCampaignEventRow.campaign_id == str(campaign_id))
                .order_by(EvaluationCampaignEventRow.sequence_number)
            ).all()
            return [
                EvaluationCampaignEvent(
                    event_id=UUID(row.event_id),
                    campaign_id=UUID(row.campaign_id),
                    event_type=EventType(row.event_type),
                    sequence_number=row.sequence_number,
                    payload=row.payload,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    def start_campaign(
        self,
        campaign_id: UUID,
        *,
        expected_version: int,
    ) -> EvaluationCampaign:
        now = utc_now()
        with self._database.session() as session:
            row = self._require_campaign(session, campaign_id)
            if row.status == EvaluationCampaignStatus.RUNNING.value:
                return self._campaign(row)
            if EvaluationCampaignStatus(row.status) in CAMPAIGN_TERMINAL:
                raise CampaignConflictError("Campaign is terminal")
            changed = self._update_count(
                session,
                update(EvaluationCampaignRow)
                .where(
                    EvaluationCampaignRow.campaign_id == str(campaign_id),
                    EvaluationCampaignRow.status
                    == EvaluationCampaignStatus.CREATED.value,
                    EvaluationCampaignRow.record_version == expected_version,
                )
                .values(
                    status=EvaluationCampaignStatus.RUNNING.value,
                    record_version=EvaluationCampaignRow.record_version + 1,
                    updated_at=now,
                ),
            )
            if changed != 1:
                raise CampaignConflictError("Campaign start transition conflicted")
            self._append_event(
                session,
                campaign_id,
                EventType.EVALUATION_CAMPAIGN_STARTED,
                {"status": EvaluationCampaignStatus.RUNNING.value},
            )
            return self._campaign(self._require_campaign(session, campaign_id))

    def claim_slot(
        self,
        slot_id: UUID,
        *,
        expected_version: int,
    ) -> EvaluationSlot | None:
        now = utc_now()
        with self._database.session() as session:
            row = self._require_slot(session, slot_id)
            changed = self._update_count(
                session,
                update(EvaluationSlotRow)
                .where(
                    EvaluationSlotRow.slot_id == str(slot_id),
                    EvaluationSlotRow.status == EvaluationSlotStatus.PENDING.value,
                    EvaluationSlotRow.record_version == expected_version,
                )
                .values(
                    status=EvaluationSlotStatus.CLAIMED.value,
                    record_version=EvaluationSlotRow.record_version + 1,
                    updated_at=now,
                ),
            )
            if changed != 1:
                return None
            self._append_event(
                session,
                UUID(row.campaign_id),
                EventType.EVALUATION_SLOT_CLAIMED,
                {
                    "slot_id": row.slot_id,
                    "repetition_index": row.repetition_index,
                },
            )
            return self._slot(self._require_slot(session, slot_id))

    def create_attempt(
        self,
        slot_id: UUID,
        *,
        expected_slot_version: int,
        predecessor_attempt_id: UUID | None = None,
        predecessor_evaluation_run_id: UUID | None = None,
    ) -> EvaluationPilotAttempt:
        now = utc_now()
        try:
            with self._database.session() as session:
                slot = self._require_slot(session, slot_id)
                status = EvaluationSlotStatus(slot.status)
                if slot.record_version != expected_slot_version:
                    raise CampaignConflictError("Slot version conflicted")
                if status not in {
                    EvaluationSlotStatus.CLAIMED,
                    EvaluationSlotStatus.REPLACEMENT_PENDING,
                }:
                    raise CampaignConflictError("Slot cannot create an attempt")
                active = session.scalar(
                    select(EvaluationPilotAttemptRow).where(
                        EvaluationPilotAttemptRow.slot_id == str(slot_id),
                        EvaluationPilotAttemptRow.status.not_in(
                            [item.value for item in ATTEMPT_TERMINAL]
                        ),
                    )
                )
                if active is not None:
                    raise CampaignConflictError("Slot already has an active attempt")
                maximum = session.scalar(
                    select(func.max(EvaluationPilotAttemptRow.attempt_number)).where(
                        EvaluationPilotAttemptRow.slot_id == str(slot_id)
                    )
                )
                attempt_number = (maximum or 0) + 1
                if attempt_number == 1:
                    if (
                        predecessor_attempt_id is not None
                        or predecessor_evaluation_run_id is not None
                    ):
                        raise CampaignConflictError(
                            "Initial attempt cannot have a predecessor"
                        )
                else:
                    self._validate_predecessor(
                        session,
                        slot_id,
                        predecessor_attempt_id,
                        predecessor_evaluation_run_id,
                    )
                attempt = EvaluationPilotAttempt(
                    campaign_id=UUID(slot.campaign_id),
                    slot_id=slot_id,
                    protocol_digest=slot.protocol_digest,
                    task_id=slot.task_id,
                    repetition_index=slot.repetition_index,
                    attempt_number=attempt_number,
                    predecessor_attempt_id=predecessor_attempt_id,
                    predecessor_evaluation_run_id=predecessor_evaluation_run_id,
                    created_at=now,
                    updated_at=now,
                )
                session.add(self._attempt_row(attempt))
                if status is EvaluationSlotStatus.REPLACEMENT_PENDING:
                    slot.status = EvaluationSlotStatus.CLAIMED.value
                    slot.record_version += 1
                    slot.updated_at = now
                self._append_event(
                    session,
                    attempt.campaign_id,
                    (
                        EventType.EVALUATION_REPLACEMENT_CREATED
                        if attempt_number > 1
                        else EventType.EVALUATION_ATTEMPT_CREATED
                    ),
                    self._attempt_payload(attempt),
                )
                return attempt
        except IntegrityError as exc:
            raise CampaignConflictError("Attempt creation conflicted") from exc

    def mark_workspace_ready(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        workspace_lease_id: UUID,
        workspace_root_digest: str,
        initial_workspace_digest: str,
        workspace_path: Path,
    ) -> EvaluationPilotAttempt:
        return self._transition_attempt(
            attempt_id,
            expected_version=expected_version,
            source=EvaluationAttemptStatus.CREATED,
            target=EvaluationAttemptStatus.WORKSPACE_READY,
            event_type=EventType.EVALUATION_ATTEMPT_WORKSPACE_READY,
            values={
                "workspace_lease_id": str(workspace_lease_id),
                "workspace_root_digest": workspace_root_digest,
                "initial_workspace_digest": initial_workspace_digest,
                "workspace_path": str(workspace_path.resolve()),
            },
        )

    def mark_runtime_ready(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        run_id: UUID,
        baseline_execution_id: UUID,
    ) -> EvaluationPilotAttempt:
        return self._transition_attempt(
            attempt_id,
            expected_version=expected_version,
            source=EvaluationAttemptStatus.WORKSPACE_READY,
            target=EvaluationAttemptStatus.RUNTIME_READY,
            event_type=EventType.EVALUATION_ATTEMPT_RUNTIME_READY,
            values={
                "run_id": str(run_id),
                "baseline_execution_id": str(baseline_execution_id),
            },
        )

    def start_attempt(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
    ) -> EvaluationPilotAttempt:
        now = utc_now()
        with self._database.session() as session:
            attempt_row = self._require_attempt(session, attempt_id)
            attempt = self._attempt(attempt_row)
            slot = self._require_slot(session, attempt.slot_id)
            if attempt.status is EvaluationAttemptStatus.RUNNING:
                if slot.status != EvaluationSlotStatus.RUNNING.value:
                    raise CampaignConflictError("Attempt and slot states diverged")
                return attempt
            if attempt.status in ATTEMPT_TERMINAL:
                raise CampaignConflictError("Attempt is terminal")
            attempt_changed = self._update_count(
                session,
                update(EvaluationPilotAttemptRow)
                .where(
                    EvaluationPilotAttemptRow.attempt_id == str(attempt_id),
                    EvaluationPilotAttemptRow.status
                    == EvaluationAttemptStatus.RUNTIME_READY.value,
                    EvaluationPilotAttemptRow.record_version == expected_version,
                )
                .values(
                    status=EvaluationAttemptStatus.RUNNING.value,
                    record_version=EvaluationPilotAttemptRow.record_version + 1,
                    updated_at=now,
                    started_at=now,
                ),
            )
            slot_changed = self._update_count(
                session,
                update(EvaluationSlotRow)
                .where(
                    EvaluationSlotRow.slot_id == attempt_row.slot_id,
                    EvaluationSlotRow.status == EvaluationSlotStatus.CLAIMED.value,
                    EvaluationSlotRow.record_version == slot.record_version,
                )
                .values(
                    status=EvaluationSlotStatus.RUNNING.value,
                    record_version=EvaluationSlotRow.record_version + 1,
                    updated_at=now,
                ),
            )
            if attempt_changed != 1 or slot_changed != 1:
                raise CampaignConflictError("Attempt start transition conflicted")
            started = self._attempt(self._require_attempt(session, attempt_id))
            self._append_event(
                session,
                started.campaign_id,
                EventType.EVALUATION_ATTEMPT_STARTED,
                self._attempt_payload(started),
            )
            return started

    def finish_attempt(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        status: EvaluationAttemptStatus,
        evaluation_run_id: UUID | None = None,
        failure_category: str | None = None,
        infrastructure_failure: bool,
    ) -> EvaluationPilotAttempt:
        if status not in ATTEMPT_TERMINAL:
            raise ValueError("Attempt finish requires a terminal status")
        with self._database.session() as session:
            current = self._attempt(self._require_attempt(session, attempt_id))
            if current.status in ATTEMPT_TERMINAL:
                if (
                    current.status is status
                    and current.evaluation_run_id == evaluation_run_id
                    and current.failure_category == failure_category
                    and current.infrastructure_failure is infrastructure_failure
                ):
                    return current
                raise CampaignConflictError("Attempt is terminal")
        event_type = {
            EvaluationAttemptStatus.COMPLETED: EventType.EVALUATION_ATTEMPT_COMPLETED,
            EvaluationAttemptStatus.INVALID: EventType.EVALUATION_ATTEMPT_INVALID,
            EvaluationAttemptStatus.INDETERMINATE: (
                EventType.EVALUATION_ATTEMPT_INDETERMINATE
            ),
        }[status]
        return self._transition_attempt(
            attempt_id,
            expected_version=expected_version,
            source=EvaluationAttemptStatus.RUNNING,
            target=status,
            event_type=event_type,
            values={
                "evaluation_run_id": (
                    str(evaluation_run_id) if evaluation_run_id else None
                ),
                "failure_category": failure_category,
                "infrastructure_failure": infrastructure_failure,
                "completed_at": utc_now(),
            },
        )

    def invalidate_pre_run_attempt(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        failure_category: str,
    ) -> EvaluationPilotAttempt:
        return self._terminate_attempt_from_states(
            attempt_id,
            expected_version=expected_version,
            allowed_sources={
                EvaluationAttemptStatus.CREATED,
                EvaluationAttemptStatus.WORKSPACE_READY,
                EvaluationAttemptStatus.RUNTIME_READY,
            },
            target=EvaluationAttemptStatus.INVALID,
            failure_category=failure_category,
            infrastructure_failure=True,
            event_type=EventType.EVALUATION_ATTEMPT_INVALID,
        )

    def mark_attempt_indeterminate(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        failure_category: str,
    ) -> EvaluationPilotAttempt:
        return self._terminate_attempt_from_states(
            attempt_id,
            expected_version=expected_version,
            allowed_sources={
                EvaluationAttemptStatus.CREATED,
                EvaluationAttemptStatus.WORKSPACE_READY,
                EvaluationAttemptStatus.RUNTIME_READY,
                EvaluationAttemptStatus.RUNNING,
            },
            target=EvaluationAttemptStatus.INDETERMINATE,
            failure_category=failure_category,
            infrastructure_failure=False,
            event_type=EventType.EVALUATION_ATTEMPT_INDETERMINATE,
        )

    def request_replacement(
        self,
        slot_id: UUID,
        *,
        expected_version: int,
        predecessor_attempt_id: UUID,
    ) -> EvaluationSlot:
        now = utc_now()
        with self._database.session() as session:
            predecessor = self._require_attempt(session, predecessor_attempt_id)
            if (
                predecessor.slot_id != str(slot_id)
                or predecessor.status != EvaluationAttemptStatus.INVALID.value
                or not predecessor.infrastructure_failure
            ):
                raise CampaignConflictError(
                    "Replacement predecessor is not infrastructure-invalid"
                )
            changed = self._update_count(
                session,
                update(EvaluationSlotRow)
                .where(
                    EvaluationSlotRow.slot_id == str(slot_id),
                    EvaluationSlotRow.status.in_(
                        (
                            EvaluationSlotStatus.CLAIMED.value,
                            EvaluationSlotStatus.RUNNING.value,
                        )
                    ),
                    EvaluationSlotRow.record_version == expected_version,
                )
                .values(
                    status=EvaluationSlotStatus.REPLACEMENT_PENDING.value,
                    record_version=EvaluationSlotRow.record_version + 1,
                    updated_at=now,
                ),
            )
            if changed != 1:
                raise CampaignConflictError("Replacement transition conflicted")
            return self._slot(self._require_slot(session, slot_id))

    def finish_slot(
        self,
        slot_id: UUID,
        *,
        expected_version: int,
        status: EvaluationSlotStatus,
    ) -> EvaluationSlot:
        if status not in {
            EvaluationSlotStatus.INVALID,
            EvaluationSlotStatus.INDETERMINATE,
        }:
            raise ValueError("Slot finish requires INVALID or INDETERMINATE")
        now = utc_now()
        with self._database.session() as session:
            current = self._slot(self._require_slot(session, slot_id))
            if current.status in SLOT_TERMINAL:
                if current.status is status:
                    return current
                raise CampaignConflictError("Slot is terminal")
            changed = self._update_count(
                session,
                update(EvaluationSlotRow)
                .where(
                    EvaluationSlotRow.slot_id == str(slot_id),
                    EvaluationSlotRow.record_version == expected_version,
                    EvaluationSlotRow.status.in_(
                        [
                            EvaluationSlotStatus.CLAIMED.value,
                            EvaluationSlotStatus.RUNNING.value,
                            EvaluationSlotStatus.REPLACEMENT_PENDING.value,
                        ]
                    ),
                )
                .values(
                    status=status.value,
                    record_version=EvaluationSlotRow.record_version + 1,
                    updated_at=now,
                    completed_at=now,
                ),
            )
            if changed != 1:
                raise CampaignConflictError("Slot terminal transition conflicted")
            row = self._require_slot(session, slot_id)
            self._append_event(
                session,
                UUID(row.campaign_id),
                (
                    EventType.EVALUATION_SLOT_INVALID
                    if status is EvaluationSlotStatus.INVALID
                    else EventType.EVALUATION_SLOT_INDETERMINATE
                ),
                {
                    "slot_id": row.slot_id,
                    "repetition_index": row.repetition_index,
                    "status": status.value,
                },
            )
            return self._slot(row)

    def accept_slot(
        self,
        slot_id: UUID,
        *,
        expected_version: int,
        attempt_id: UUID,
        evaluation_run_id: UUID | None,
    ) -> EvaluationSlot:
        now = utc_now()
        with self._database.session() as session:
            current = self._slot(self._require_slot(session, slot_id))
            if current.status is EvaluationSlotStatus.ACCEPTED:
                if (
                    current.selected_attempt_id == attempt_id
                    and current.selected_evaluation_run_id == evaluation_run_id
                ):
                    return current
                raise CampaignConflictError("Slot is terminal")
            attempt = self._require_attempt(session, attempt_id)
            if (
                attempt.slot_id != str(slot_id)
                or attempt.status != EvaluationAttemptStatus.COMPLETED.value
                or attempt.evaluation_run_id
                != (str(evaluation_run_id) if evaluation_run_id else None)
            ):
                raise CampaignConflictError("Selected attempt is not completed")
            changed = self._update_count(
                session,
                update(EvaluationSlotRow)
                .where(
                    EvaluationSlotRow.slot_id == str(slot_id),
                    EvaluationSlotRow.status == EvaluationSlotStatus.RUNNING.value,
                    EvaluationSlotRow.record_version == expected_version,
                )
                .values(
                    status=EvaluationSlotStatus.ACCEPTED.value,
                    selected_attempt_id=str(attempt_id),
                    selected_evaluation_run_id=(
                        str(evaluation_run_id) if evaluation_run_id else None
                    ),
                    record_version=EvaluationSlotRow.record_version + 1,
                    updated_at=now,
                    completed_at=now,
                ),
            )
            if changed != 1:
                raise CampaignConflictError("Slot acceptance conflicted")
            self._append_event(
                session,
                UUID(attempt.campaign_id),
                EventType.EVALUATION_SLOT_ACCEPTED,
                {
                    "slot_id": str(slot_id),
                    "attempt_id": str(attempt_id),
                    "evaluation_run_id": (
                        str(evaluation_run_id) if evaluation_run_id else None
                    ),
                },
            )
            return self._slot(self._require_slot(session, slot_id))

    def finish_campaign(
        self,
        campaign_id: UUID,
        *,
        expected_version: int,
        status: EvaluationCampaignStatus,
    ) -> EvaluationCampaign:
        if status not in CAMPAIGN_TERMINAL:
            raise ValueError("Campaign finish requires a terminal status")
        now = utc_now()
        with self._database.session() as session:
            current = self._campaign(self._require_campaign(session, campaign_id))
            if current.status in CAMPAIGN_TERMINAL:
                if current.status is status:
                    return current
                raise CampaignConflictError("Campaign is terminal")
            slots = session.scalars(
                select(EvaluationSlotRow).where(
                    EvaluationSlotRow.campaign_id == str(campaign_id)
                )
            ).all()
            if status is EvaluationCampaignStatus.COMPLETED and any(
                row.status != EvaluationSlotStatus.ACCEPTED.value for row in slots
            ):
                raise CampaignConflictError(
                    "Completed campaign requires all slots accepted"
                )
            changed = self._update_count(
                session,
                update(EvaluationCampaignRow)
                .where(
                    EvaluationCampaignRow.campaign_id == str(campaign_id),
                    EvaluationCampaignRow.status
                    == EvaluationCampaignStatus.RUNNING.value,
                    EvaluationCampaignRow.record_version == expected_version,
                )
                .values(
                    status=status.value,
                    record_version=EvaluationCampaignRow.record_version + 1,
                    updated_at=now,
                    completed_at=now,
                ),
            )
            if changed != 1:
                raise CampaignConflictError("Campaign terminal transition conflicted")
            self._append_event(
                session,
                campaign_id,
                EventType.EVALUATION_CAMPAIGN_COMPLETED,
                {
                    "status": status.value,
                    "slot_count": len(slots),
                    "accepted_slot_count": sum(
                        row.status == EvaluationSlotStatus.ACCEPTED.value
                        for row in slots
                    ),
                },
            )
            return self._campaign(self._require_campaign(session, campaign_id))

    def _transition_attempt(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        source: EvaluationAttemptStatus,
        target: EvaluationAttemptStatus,
        event_type: EventType,
        values: dict[str, object],
    ) -> EvaluationPilotAttempt:
        now = utc_now()
        with self._database.session() as session:
            current = self._attempt(self._require_attempt(session, attempt_id))
            if current.status in ATTEMPT_TERMINAL:
                raise CampaignConflictError("Attempt is terminal")
            changed = self._update_count(
                session,
                update(EvaluationPilotAttemptRow)
                .where(
                    EvaluationPilotAttemptRow.attempt_id == str(attempt_id),
                    EvaluationPilotAttemptRow.status == source.value,
                    EvaluationPilotAttemptRow.record_version == expected_version,
                )
                .values(
                    **values,
                    status=target.value,
                    record_version=EvaluationPilotAttemptRow.record_version + 1,
                    updated_at=now,
                ),
            )
            if changed != 1:
                raise CampaignConflictError("Attempt transition conflicted")
            row = self._require_attempt(session, attempt_id)
            record = self._attempt(row)
            self._append_event(
                session,
                record.campaign_id,
                event_type,
                self._attempt_payload(record),
            )
            return record

    def _terminate_attempt_from_states(
        self,
        attempt_id: UUID,
        *,
        expected_version: int,
        allowed_sources: set[EvaluationAttemptStatus],
        target: EvaluationAttemptStatus,
        failure_category: str,
        infrastructure_failure: bool,
        event_type: EventType,
    ) -> EvaluationPilotAttempt:
        now = utc_now()
        with self._database.session() as session:
            current = self._attempt(self._require_attempt(session, attempt_id))
            if current.status in ATTEMPT_TERMINAL:
                if (
                    current.status is target
                    and current.failure_category == failure_category
                    and current.infrastructure_failure is infrastructure_failure
                ):
                    return current
                raise CampaignConflictError("Attempt is terminal")
            changed = self._update_count(
                session,
                update(EvaluationPilotAttemptRow)
                .where(
                    EvaluationPilotAttemptRow.attempt_id == str(attempt_id),
                    EvaluationPilotAttemptRow.record_version == expected_version,
                    EvaluationPilotAttemptRow.status.in_(
                        [item.value for item in allowed_sources]
                    ),
                )
                .values(
                    status=target.value,
                    failure_category=failure_category,
                    infrastructure_failure=infrastructure_failure,
                    record_version=EvaluationPilotAttemptRow.record_version + 1,
                    updated_at=now,
                    completed_at=now,
                ),
            )
            if changed != 1:
                raise CampaignConflictError("Attempt terminal transition conflicted")
            record = self._attempt(self._require_attempt(session, attempt_id))
            self._append_event(
                session,
                record.campaign_id,
                event_type,
                self._attempt_payload(record),
            )
            return record

    @staticmethod
    def _validate_predecessor(
        session: Session,
        slot_id: UUID,
        predecessor_attempt_id: UUID | None,
        predecessor_evaluation_run_id: UUID | None,
    ) -> None:
        if predecessor_attempt_id is None:
            raise CampaignConflictError("Replacement attempt requires predecessor")
        predecessor = session.get(
            EvaluationPilotAttemptRow, str(predecessor_attempt_id)
        )
        if (
            predecessor is None
            or predecessor.slot_id != str(slot_id)
            or predecessor.status != EvaluationAttemptStatus.INVALID.value
            or not predecessor.infrastructure_failure
            or predecessor.evaluation_run_id
            != (
                str(predecessor_evaluation_run_id)
                if predecessor_evaluation_run_id
                else None
            )
        ):
            raise CampaignConflictError("Replacement predecessor does not match")

    @staticmethod
    def _append_event(
        session: Session,
        campaign_id: UUID,
        event_type: EventType,
        payload: dict[str, JsonValue],
    ) -> None:
        maximum = session.scalar(
            select(func.max(EvaluationCampaignEventRow.sequence_number)).where(
                EvaluationCampaignEventRow.campaign_id == str(campaign_id)
            )
        )
        session.add(
            EvaluationCampaignEventRow(
                event_id=str(uuid4()),
                campaign_id=str(campaign_id),
                event_type=event_type.value,
                sequence_number=(maximum or 0) + 1,
                payload=payload,
                created_at=utc_now(),
            )
        )

    @staticmethod
    def _attempt_payload(
        attempt: EvaluationPilotAttempt,
    ) -> dict[str, JsonValue]:
        return {
            "attempt_id": str(attempt.attempt_id),
            "slot_id": str(attempt.slot_id),
            "attempt_number": attempt.attempt_number,
            "status": attempt.status.value,
            "workspace_lease_id": (
                str(attempt.workspace_lease_id)
                if attempt.workspace_lease_id
                else None
            ),
            "workspace_root_digest": attempt.workspace_root_digest,
            "initial_workspace_digest": attempt.initial_workspace_digest,
            "run_id": str(attempt.run_id) if attempt.run_id else None,
            "baseline_execution_id": (
                str(attempt.baseline_execution_id)
                if attempt.baseline_execution_id
                else None
            ),
            "evaluation_run_id": (
                str(attempt.evaluation_run_id)
                if attempt.evaluation_run_id
                else None
            ),
            "failure_category": attempt.failure_category,
            "infrastructure_failure": attempt.infrastructure_failure,
        }

    @staticmethod
    def _update_count(session: Session, statement: Update) -> int:
        return ApprovalWorkflow._affected_rows(session.execute(statement))

    @staticmethod
    def _require_campaign(
        session: Session, campaign_id: UUID
    ) -> EvaluationCampaignRow:
        row = session.get(EvaluationCampaignRow, str(campaign_id))
        if row is None:
            raise KeyError(f"Evaluation campaign {campaign_id} is missing")
        return row

    @staticmethod
    def _require_slot(session: Session, slot_id: UUID) -> EvaluationSlotRow:
        row = session.get(EvaluationSlotRow, str(slot_id))
        if row is None:
            raise KeyError(f"Evaluation slot {slot_id} is missing")
        return row

    @staticmethod
    def _require_attempt(
        session: Session, attempt_id: UUID
    ) -> EvaluationPilotAttemptRow:
        row = session.get(EvaluationPilotAttemptRow, str(attempt_id))
        if row is None:
            raise KeyError(f"Evaluation attempt {attempt_id} is missing")
        return row

    @staticmethod
    def _campaign_row(value: EvaluationCampaign) -> EvaluationCampaignRow:
        return EvaluationCampaignRow(
            campaign_id=str(value.campaign_id),
            protocol_digest=value.protocol_digest,
            task_id=value.task_id,
            repetition_count=value.repetition_count,
            status=value.status.value,
            record_version=value.record_version,
            created_at=value.created_at,
            updated_at=value.updated_at,
            completed_at=value.completed_at,
        )

    @staticmethod
    def _slot_row(value: EvaluationSlot) -> EvaluationSlotRow:
        return EvaluationSlotRow(
            slot_id=str(value.slot_id),
            campaign_id=str(value.campaign_id),
            protocol_digest=value.protocol_digest,
            task_id=value.task_id,
            repetition_index=value.repetition_index,
            status=value.status.value,
            selected_attempt_id=(
                str(value.selected_attempt_id) if value.selected_attempt_id else None
            ),
            selected_evaluation_run_id=(
                str(value.selected_evaluation_run_id)
                if value.selected_evaluation_run_id
                else None
            ),
            record_version=value.record_version,
            created_at=value.created_at,
            updated_at=value.updated_at,
            completed_at=value.completed_at,
        )

    @staticmethod
    def _attempt_row(value: EvaluationPilotAttempt) -> EvaluationPilotAttemptRow:
        return EvaluationPilotAttemptRow(
            attempt_id=str(value.attempt_id),
            campaign_id=str(value.campaign_id),
            slot_id=str(value.slot_id),
            protocol_digest=value.protocol_digest,
            task_id=value.task_id,
            repetition_index=value.repetition_index,
            attempt_number=value.attempt_number,
            status=value.status.value,
            predecessor_attempt_id=(
                str(value.predecessor_attempt_id)
                if value.predecessor_attempt_id
                else None
            ),
            predecessor_evaluation_run_id=(
                str(value.predecessor_evaluation_run_id)
                if value.predecessor_evaluation_run_id
                else None
            ),
            workspace_lease_id=(
                str(value.workspace_lease_id) if value.workspace_lease_id else None
            ),
            workspace_root_digest=value.workspace_root_digest,
            initial_workspace_digest=value.initial_workspace_digest,
            workspace_path=str(value.workspace_path) if value.workspace_path else None,
            run_id=str(value.run_id) if value.run_id else None,
            baseline_execution_id=(
                str(value.baseline_execution_id)
                if value.baseline_execution_id
                else None
            ),
            evaluation_run_id=(
                str(value.evaluation_run_id) if value.evaluation_run_id else None
            ),
            failure_category=value.failure_category,
            infrastructure_failure=value.infrastructure_failure,
            record_version=value.record_version,
            created_at=value.created_at,
            updated_at=value.updated_at,
            started_at=value.started_at,
            completed_at=value.completed_at,
        )

    @staticmethod
    def _campaign(row: EvaluationCampaignRow) -> EvaluationCampaign:
        return EvaluationCampaign(
            campaign_id=UUID(row.campaign_id),
            protocol_digest=row.protocol_digest,
            task_id=row.task_id,
            repetition_count=row.repetition_count,
            status=EvaluationCampaignStatus(row.status),
            record_version=row.record_version,
            created_at=row.created_at,
            updated_at=row.updated_at,
            completed_at=row.completed_at,
        )

    @staticmethod
    def _slot(row: EvaluationSlotRow) -> EvaluationSlot:
        return EvaluationSlot(
            slot_id=UUID(row.slot_id),
            campaign_id=UUID(row.campaign_id),
            protocol_digest=row.protocol_digest,
            task_id=row.task_id,
            repetition_index=row.repetition_index,
            status=EvaluationSlotStatus(row.status),
            selected_attempt_id=(
                UUID(row.selected_attempt_id) if row.selected_attempt_id else None
            ),
            selected_evaluation_run_id=(
                UUID(row.selected_evaluation_run_id)
                if row.selected_evaluation_run_id
                else None
            ),
            record_version=row.record_version,
            created_at=row.created_at,
            updated_at=row.updated_at,
            completed_at=row.completed_at,
        )

    @staticmethod
    def _attempt(row: EvaluationPilotAttemptRow) -> EvaluationPilotAttempt:
        return EvaluationPilotAttempt(
            attempt_id=UUID(row.attempt_id),
            campaign_id=UUID(row.campaign_id),
            slot_id=UUID(row.slot_id),
            protocol_digest=row.protocol_digest,
            task_id=row.task_id,
            repetition_index=row.repetition_index,
            attempt_number=row.attempt_number,
            status=EvaluationAttemptStatus(row.status),
            predecessor_attempt_id=(
                UUID(row.predecessor_attempt_id)
                if row.predecessor_attempt_id
                else None
            ),
            predecessor_evaluation_run_id=(
                UUID(row.predecessor_evaluation_run_id)
                if row.predecessor_evaluation_run_id
                else None
            ),
            workspace_lease_id=(
                UUID(row.workspace_lease_id) if row.workspace_lease_id else None
            ),
            workspace_root_digest=row.workspace_root_digest,
            initial_workspace_digest=row.initial_workspace_digest,
            workspace_path=Path(row.workspace_path) if row.workspace_path else None,
            run_id=UUID(row.run_id) if row.run_id else None,
            baseline_execution_id=(
                UUID(row.baseline_execution_id)
                if row.baseline_execution_id
                else None
            ),
            evaluation_run_id=(
                UUID(row.evaluation_run_id) if row.evaluation_run_id else None
            ),
            failure_category=row.failure_category,
            infrastructure_failure=row.infrastructure_failure,
            record_version=row.record_version,
            created_at=row.created_at,
            updated_at=row.updated_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )
