from collections.abc import Sequence
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agentforge.domain.enums import (
    EvaluationCampaignStatus,
    EvaluationStudyStatus,
    EventType,
)
from agentforge.domain.models import utc_now
from agentforge.evaluation.study_models import (
    EvaluationStudy,
    EvaluationStudyCampaignBinding,
    EvaluationStudyDefinition,
    EvaluationStudyEvent,
    RealModelAuthorization,
    validate_real_model_authorization,
    validate_study_campaign_bindings,
)
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.tables import (
    EvaluationCampaignRow,
    EvaluationStudyCampaignRow,
    EvaluationStudyEventRow,
    EvaluationStudyRow,
)

_CAMPAIGN_TERMINAL = frozenset(
    {
        EvaluationCampaignStatus.COMPLETED,
        EvaluationCampaignStatus.COMPLETED_WITH_INVALID,
        EvaluationCampaignStatus.BLOCKED,
        EvaluationCampaignStatus.INDETERMINATE,
    }
)
_STUDY_TERMINAL = frozenset(
    {
        EvaluationStudyStatus.COMPLETED,
        EvaluationStudyStatus.COMPLETED_WITH_INFRASTRUCTURE_GAPS,
        EvaluationStudyStatus.ABORTED_CONFIGURATION,
        EvaluationStudyStatus.INDETERMINATE,
    }
)


class StudyConflictError(RuntimeError):
    pass


class EvaluationStudyRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create_study(
        self,
        definition: EvaluationStudyDefinition,
        campaign_ids: Sequence[UUID],
        *,
        study_id: UUID | None = None,
    ) -> EvaluationStudy:
        validated = EvaluationStudyDefinition.model_validate(
            definition.model_dump(mode="json")
        )
        identity = study_id or uuid4()
        try:
            with self._database.session() as session:
                existing = session.get(EvaluationStudyRow, str(identity))
                if existing is not None:
                    persisted = self._study(existing)
                    persisted_definition = self._definition(existing)
                    persisted_campaign_ids = tuple(
                        UUID(row.campaign_id)
                        for row in self._binding_rows(session, identity)
                    )
                    if (
                        persisted_definition != validated
                        or persisted_campaign_ids != tuple(campaign_ids)
                    ):
                        raise StudyConflictError(
                            "Evaluation Study identity conflict"
                        )
                    return persisted
                if len(campaign_ids) != 4:
                    raise StudyConflictError(
                        "Evaluation Study requires four Campaigns"
                    )
                if len(set(campaign_ids)) != 4:
                    raise StudyConflictError(
                        "Evaluation Study Campaign IDs must be unique"
                    )
                study = EvaluationStudy(
                    study_id=identity,
                    definition_digest=validated.definition_digest,
                )
                bindings = self._build_bindings(
                    session,
                    validated,
                    study,
                    campaign_ids,
                )
                session.add(
                    EvaluationStudyRow(
                        study_id=str(study.study_id),
                        definition_digest=study.definition_digest,
                        definition_data=validated.model_dump(mode="json"),
                        authorization_digest=None,
                        authorization_data=None,
                        status=study.status.value,
                        record_version=study.record_version,
                        created_at=study.created_at,
                        updated_at=study.updated_at,
                        completed_at=None,
                    )
                )
                session.flush()
                for binding in bindings:
                    session.add(
                        EvaluationStudyCampaignRow(
                            study_id=str(binding.study_id),
                            task_order=binding.task_order,
                            task_id=binding.task_id,
                            protocol_digest=binding.protocol_digest,
                            campaign_id=str(binding.campaign_id),
                            campaign_status=None,
                            completed_at=None,
                        )
                    )
                session.flush()
                self._append_event(
                    session,
                    study.study_id,
                    EventType.EVALUATION_STUDY_CREATED,
                    {
                        "definition_digest": study.definition_digest,
                        "protocol_count": 4,
                        "campaign_count": 4,
                        "planned_slot_count": 12,
                    },
                )
                return study
        except IntegrityError as exc:
            raise StudyConflictError(
                "Evaluation Study identity conflict"
            ) from exc

    def get_study(self, study_id: UUID) -> EvaluationStudy:
        with self._database.session() as session:
            return self._study(self._require_study(session, study_id))

    def get_definition(
        self,
        study_id: UUID,
    ) -> EvaluationStudyDefinition:
        with self._database.session() as session:
            return self._definition(self._require_study(session, study_id))

    def get_authorization(
        self,
        study_id: UUID,
    ) -> RealModelAuthorization:
        with self._database.session() as session:
            row = self._require_study(session, study_id)
            if row.authorization_data is None:
                raise StudyConflictError("Evaluation Study is not authorized")
            authorization = RealModelAuthorization.model_validate(
                row.authorization_data
            )
            if authorization.authorization_digest != row.authorization_digest:
                raise StudyConflictError(
                    "Persisted Study authorization digest drift"
                )
            return authorization

    def list_campaign_bindings(
        self,
        study_id: UUID,
    ) -> list[EvaluationStudyCampaignBinding]:
        with self._database.session() as session:
            self._require_study(session, study_id)
            return [
                self._binding(row)
                for row in self._binding_rows(session, study_id)
            ]

    def authorize(
        self,
        study_id: UUID,
        authorization: RealModelAuthorization,
        *,
        expected_version: int,
    ) -> EvaluationStudy:
        with self._database.session() as session:
            row = self._require_study(session, study_id)
            definition = self._definition(row)
            validate_real_model_authorization(definition, authorization)
            if row.status != EvaluationStudyStatus.DRAFT.value:
                raise StudyConflictError(
                    "Evaluation Study authorization state conflicted"
                )
            now = utc_now()
            changed = ApprovalWorkflow._affected_rows(
                session.execute(
                    update(EvaluationStudyRow)
                    .where(
                        EvaluationStudyRow.study_id == str(study_id),
                        EvaluationStudyRow.status
                        == EvaluationStudyStatus.DRAFT.value,
                        EvaluationStudyRow.record_version == expected_version,
                    )
                    .values(
                        authorization_digest=(
                            authorization.authorization_digest
                        ),
                        authorization_data=authorization.model_dump(
                            mode="json"
                        ),
                        status=EvaluationStudyStatus.AUTHORIZED.value,
                        record_version=(
                            EvaluationStudyRow.record_version + 1
                        ),
                        updated_at=now,
                    )
                )
            )
            if changed != 1:
                raise StudyConflictError(
                    "Evaluation Study authorization version conflicted"
                )
            self._append_event(
                session,
                study_id,
                EventType.EVALUATION_STUDY_AUTHORIZED,
                {
                    "definition_digest": definition.definition_digest,
                    "authorization_digest": authorization.authorization_digest,
                    "maximum_campaigns": authorization.maximum_campaigns,
                    "maximum_planned_slots": (
                        authorization.maximum_planned_slots
                    ),
                },
            )
            return self._study(self._require_study(session, study_id))

    def start(
        self,
        study_id: UUID,
        *,
        expected_version: int,
    ) -> EvaluationStudy:
        with self._database.session() as session:
            current = self._require_study(session, study_id)
            if current.status == EvaluationStudyStatus.RUNNING.value:
                raise StudyConflictError(
                    "Evaluation Study execution claim conflicted"
                )
            if EvaluationStudyStatus(current.status) in _STUDY_TERMINAL:
                raise StudyConflictError("Evaluation Study is terminal")
            now = utc_now()
            changed = ApprovalWorkflow._affected_rows(
                session.execute(
                    update(EvaluationStudyRow)
                    .where(
                        EvaluationStudyRow.study_id == str(study_id),
                        EvaluationStudyRow.status
                        == EvaluationStudyStatus.AUTHORIZED.value,
                        EvaluationStudyRow.record_version == expected_version,
                    )
                    .values(
                        status=EvaluationStudyStatus.RUNNING.value,
                        record_version=(
                            EvaluationStudyRow.record_version + 1
                        ),
                        updated_at=now,
                    )
                )
            )
            if changed != 1:
                raise StudyConflictError(
                    "Evaluation Study start version conflicted"
                )
            self._append_event(
                session,
                study_id,
                EventType.EVALUATION_STUDY_STARTED,
                {"status": EvaluationStudyStatus.RUNNING.value},
            )
            return self._study(self._require_study(session, study_id))

    def record_campaign_completion(
        self,
        study_id: UUID,
        campaign_id: UUID,
        *,
        campaign_status: EvaluationCampaignStatus,
        expected_version: int,
    ) -> EvaluationStudy:
        if campaign_status not in _CAMPAIGN_TERMINAL:
            raise ValueError("Study Campaign completion requires terminal status")
        with self._database.session() as session:
            study_row = self._require_study(session, study_id)
            binding_rows = self._binding_rows(session, study_id)
            target = next(
                (
                    row
                    for row in binding_rows
                    if row.campaign_id == str(campaign_id)
                ),
                None,
            )
            if target is None:
                raise StudyConflictError("Campaign is not bound to Study")
            if target.campaign_status is not None:
                if target.campaign_status == campaign_status.value:
                    return self._study(study_row)
                raise StudyConflictError(
                    "Study Campaign completion identity conflict"
                )
            next_pending = next(
                (row for row in binding_rows if row.campaign_status is None),
                None,
            )
            if next_pending is None or next_pending.campaign_id != str(
                campaign_id
            ):
                raise StudyConflictError(
                    "Study Campaign completion order conflicted"
                )
            campaign = session.get(EvaluationCampaignRow, str(campaign_id))
            if campaign is None or campaign.status != campaign_status.value:
                raise StudyConflictError(
                    "Study Campaign completion does not match Campaign"
                )
            if study_row.status != EvaluationStudyStatus.RUNNING.value:
                raise StudyConflictError("Evaluation Study is not running")
            now = utc_now()
            changed = ApprovalWorkflow._affected_rows(
                session.execute(
                    update(EvaluationStudyRow)
                    .where(
                        EvaluationStudyRow.study_id == str(study_id),
                        EvaluationStudyRow.status
                        == EvaluationStudyStatus.RUNNING.value,
                        EvaluationStudyRow.record_version == expected_version,
                    )
                    .values(
                        record_version=(
                            EvaluationStudyRow.record_version + 1
                        ),
                        updated_at=now,
                    )
                )
            )
            if changed != 1:
                raise StudyConflictError(
                    "Study Campaign completion version conflicted"
                )
            target.campaign_status = campaign_status.value
            target.completed_at = now
            self._append_event(
                session,
                study_id,
                EventType.EVALUATION_STUDY_CAMPAIGN_COMPLETED,
                {
                    "campaign_id": str(campaign_id),
                    "task_order": target.task_order,
                    "campaign_status": campaign_status.value,
                },
            )
            return self._study(self._require_study(session, study_id))

    def finish(
        self,
        study_id: UUID,
        *,
        expected_version: int,
        status: EvaluationStudyStatus,
    ) -> EvaluationStudy:
        if status not in _STUDY_TERMINAL:
            raise ValueError("Study finish requires a terminal status")
        with self._database.session() as session:
            current = self._require_study(session, study_id)
            if EvaluationStudyStatus(current.status) in _STUDY_TERMINAL:
                raise StudyConflictError("Evaluation Study is terminal")
            now = utc_now()
            changed = ApprovalWorkflow._affected_rows(
                session.execute(
                    update(EvaluationStudyRow)
                    .where(
                        EvaluationStudyRow.study_id == str(study_id),
                        EvaluationStudyRow.status
                        == EvaluationStudyStatus.RUNNING.value,
                        EvaluationStudyRow.record_version == expected_version,
                    )
                    .values(
                        status=status.value,
                        record_version=(
                            EvaluationStudyRow.record_version + 1
                        ),
                        updated_at=now,
                        completed_at=now,
                    )
                )
            )
            if changed != 1:
                raise StudyConflictError(
                    "Evaluation Study finish version conflicted"
                )
            event_type = {
                EvaluationStudyStatus.COMPLETED: (
                    EventType.EVALUATION_STUDY_COMPLETED
                ),
                EvaluationStudyStatus.COMPLETED_WITH_INFRASTRUCTURE_GAPS: (
                    EventType.EVALUATION_STUDY_COMPLETED
                ),
                EvaluationStudyStatus.ABORTED_CONFIGURATION: (
                    EventType.EVALUATION_STUDY_ABORTED
                ),
                EvaluationStudyStatus.INDETERMINATE: (
                    EventType.EVALUATION_STUDY_INDETERMINATE
                ),
            }[status]
            self._append_event(
                session,
                study_id,
                event_type,
                {"status": status.value},
            )
            return self._study(self._require_study(session, study_id))

    def list_events(self, study_id: UUID) -> list[EvaluationStudyEvent]:
        with self._database.session() as session:
            self._require_study(session, study_id)
            rows = session.scalars(
                select(EvaluationStudyEventRow)
                .where(EvaluationStudyEventRow.study_id == str(study_id))
                .order_by(EvaluationStudyEventRow.sequence_number)
            ).all()
            return [
                EvaluationStudyEvent(
                    event_id=UUID(row.event_id),
                    study_id=UUID(row.study_id),
                    event_type=EventType(row.event_type),
                    sequence_number=row.sequence_number,
                    payload=row.payload,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    @staticmethod
    def _build_bindings(
        session: Session,
        definition: EvaluationStudyDefinition,
        study: EvaluationStudy,
        campaign_ids: Sequence[UUID],
    ) -> tuple[
        EvaluationStudyCampaignBinding,
        EvaluationStudyCampaignBinding,
        EvaluationStudyCampaignBinding,
        EvaluationStudyCampaignBinding,
    ]:
        values: list[EvaluationStudyCampaignBinding] = []
        for index, campaign_id in enumerate(campaign_ids):
            campaign = session.get(EvaluationCampaignRow, str(campaign_id))
            if campaign is None:
                raise StudyConflictError(
                    "Bound Evaluation Campaign is missing"
                )
            if (
                campaign.task_id != definition.task_ids[index]
                or campaign.protocol_digest
                != definition.protocol_digests[index]
                or campaign.repetition_count
                != definition.repetitions_per_task
                or campaign.status
                != EvaluationCampaignStatus.CREATED.value
            ):
                raise StudyConflictError(
                    "Bound Evaluation Campaign facts do not match Study"
                )
            values.append(
                EvaluationStudyCampaignBinding(
                    study_id=study.study_id,
                    task_id=campaign.task_id,
                    task_order=index,
                    protocol_digest=campaign.protocol_digest,
                    campaign_id=campaign_id,
                )
            )
        bindings = cast(
            tuple[
                EvaluationStudyCampaignBinding,
                EvaluationStudyCampaignBinding,
                EvaluationStudyCampaignBinding,
                EvaluationStudyCampaignBinding,
            ],
            tuple(values),
        )
        return validate_study_campaign_bindings(
            definition,
            study,
            bindings,
        )

    @staticmethod
    def _study(row: EvaluationStudyRow) -> EvaluationStudy:
        return EvaluationStudy(
            study_id=UUID(row.study_id),
            definition_digest=row.definition_digest,
            authorization_digest=row.authorization_digest,
            status=EvaluationStudyStatus(row.status),
            record_version=row.record_version,
            created_at=row.created_at,
            updated_at=row.updated_at,
            completed_at=row.completed_at,
        )

    @staticmethod
    def _definition(
        row: EvaluationStudyRow,
    ) -> EvaluationStudyDefinition:
        definition = EvaluationStudyDefinition.model_validate(
            row.definition_data
        )
        if definition.definition_digest != row.definition_digest:
            raise StudyConflictError(
                "Persisted Study definition digest drift"
            )
        return definition

    @staticmethod
    def _binding(
        row: EvaluationStudyCampaignRow,
    ) -> EvaluationStudyCampaignBinding:
        return EvaluationStudyCampaignBinding(
            study_id=UUID(row.study_id),
            task_id=row.task_id,
            task_order=row.task_order,
            protocol_digest=row.protocol_digest,
            campaign_id=UUID(row.campaign_id),
            campaign_status=(
                EvaluationCampaignStatus(row.campaign_status)
                if row.campaign_status is not None
                else None
            ),
            completed_at=row.completed_at,
        )

    @staticmethod
    def _binding_rows(
        session: Session,
        study_id: UUID,
    ) -> list[EvaluationStudyCampaignRow]:
        return list(
            session.scalars(
                select(EvaluationStudyCampaignRow)
                .where(EvaluationStudyCampaignRow.study_id == str(study_id))
                .order_by(EvaluationStudyCampaignRow.task_order)
            ).all()
        )

    @staticmethod
    def _require_study(
        session: Session,
        study_id: UUID,
    ) -> EvaluationStudyRow:
        row = session.get(EvaluationStudyRow, str(study_id))
        if row is None:
            raise StudyConflictError(f"Evaluation Study {study_id} is missing")
        return row

    @staticmethod
    def _append_event(
        session: Session,
        study_id: UUID,
        event_type: EventType,
        payload: dict[str, str | int | bool | None],
    ) -> None:
        maximum = session.scalar(
            select(func.max(EvaluationStudyEventRow.sequence_number)).where(
                EvaluationStudyEventRow.study_id == str(study_id)
            )
        )
        event = EvaluationStudyEvent(
            study_id=study_id,
            event_type=event_type,
            sequence_number=(maximum or 0) + 1,
            payload=payload,
        )
        session.add(
            EvaluationStudyEventRow(
                event_id=str(event.event_id),
                study_id=str(study_id),
                event_type=event.event_type.value,
                sequence_number=event.sequence_number,
                payload=event.payload,
                created_at=event.created_at,
            )
        )
