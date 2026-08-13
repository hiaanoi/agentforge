from collections.abc import Sequence
from typing import Protocol, TypeVar, cast
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, model_validator

from agentforge.domain.enums import (
    EvaluationCampaignStatus,
    EvaluationStudyStatus,
)
from agentforge.evaluation.campaign_models import EvaluationCampaign
from agentforge.evaluation.campaign_persistence import (
    EvaluationCampaignRepository,
)
from agentforge.evaluation.persistence import EvaluationRunRepository
from agentforge.evaluation.pilot_runner import PilotCampaignResult
from agentforge.evaluation.protocol_persistence import (
    EvaluationProtocolRepository,
)
from agentforge.evaluation.study_builder import EvaluationStudyBuild
from agentforge.evaluation.study_models import (
    EvaluationStudy,
    EvaluationStudyCampaignBinding,
    validate_study_campaign_bindings,
)
from agentforge.evaluation.study_persistence import (
    EvaluationStudyRepository,
)
from agentforge.evaluation.telemetry import EvaluationTelemetryCollector
from agentforge.evaluation.telemetry_persistence import (
    EvaluationTelemetryRepository,
)
from agentforge.persistence.database import Database
from agentforge.persistence.model_workflow import ModelWorkflow
from agentforge.persistence.repositories import EventRepository

_STUDY_NAMESPACE = UUID("a769fbd8-ef76-5e7b-9b67-270f685649cd")
_TERMINAL_STUDY_STATUSES = frozenset(
    {
        EvaluationStudyStatus.COMPLETED,
        EvaluationStudyStatus.COMPLETED_WITH_INFRASTRUCTURE_GAPS,
        EvaluationStudyStatus.ABORTED_CONFIGURATION,
        EvaluationStudyStatus.INDETERMINATE,
    }
)
_TERMINAL_CAMPAIGN_STATUSES = frozenset(
    {
        EvaluationCampaignStatus.COMPLETED,
        EvaluationCampaignStatus.COMPLETED_WITH_INVALID,
        EvaluationCampaignStatus.BLOCKED,
        EvaluationCampaignStatus.INDETERMINATE,
    }
)

_T = TypeVar("_T")


class StudyExecutionConflictError(RuntimeError):
    pass


class StudyCampaignExecutor(Protocol):
    async def run_campaign(
        self,
        campaign_id: UUID,
    ) -> PilotCampaignResult: ...

    async def recover_campaign(
        self,
        campaign_id: UUID,
    ) -> PilotCampaignResult: ...


class EvaluationStudyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    study: EvaluationStudy
    bindings: tuple[
        EvaluationStudyCampaignBinding,
        EvaluationStudyCampaignBinding,
        EvaluationStudyCampaignBinding,
        EvaluationStudyCampaignBinding,
    ]
    campaigns: tuple[
        EvaluationCampaign,
        EvaluationCampaign,
        EvaluationCampaign,
        EvaluationCampaign,
    ]

    @model_validator(mode="after")
    def validate_campaign_facts(self) -> "EvaluationStudyResult":
        for binding, campaign in zip(
            self.bindings,
            self.campaigns,
            strict=True,
        ):
            if (
                binding.study_id != self.study.study_id
                or binding.campaign_id != campaign.campaign_id
                or binding.protocol_digest != campaign.protocol_digest
                or binding.task_id != campaign.task_id
            ):
                raise ValueError("Study result Campaign binding does not match")
            if (
                binding.campaign_status is not None
                and binding.campaign_status is not campaign.status
            ):
                raise ValueError(
                    "Study result Campaign completion does not match"
                )
        if self.study.status in {
            EvaluationStudyStatus.COMPLETED,
            EvaluationStudyStatus.COMPLETED_WITH_INFRASTRUCTURE_GAPS,
        } and any(
            binding.campaign_status is None for binding in self.bindings
        ):
            raise ValueError(
                "Completed Study requires every Campaign completion"
            )
        return self


class EvaluationStudyRegistrar:
    def __init__(self, database: Database) -> None:
        self._protocols = EvaluationProtocolRepository(database)
        self._campaigns = EvaluationCampaignRepository(database)
        self._studies = EvaluationStudyRepository(database)

    def register(
        self,
        build: EvaluationStudyBuild,
    ) -> EvaluationStudy:
        validated = EvaluationStudyBuild.model_validate(
            build.model_dump(mode="json")
        )
        campaigns: list[EvaluationCampaign] = []
        for protocol in validated.protocols:
            self._protocols.register(protocol)
            campaigns.append(self._campaigns.create_campaign(protocol))
        study_id = uuid5(
            _STUDY_NAMESPACE,
            validated.definition.definition_digest,
        )
        return self._studies.create_study(
            validated.definition,
            [campaign.campaign_id for campaign in campaigns],
            study_id=study_id,
        )


class EvaluationStudyRunner:
    def __init__(
        self,
        database: Database,
        campaign_executor: StudyCampaignExecutor,
    ) -> None:
        self._studies = EvaluationStudyRepository(database)
        self._campaigns = EvaluationCampaignRepository(database)
        self._protocols = EvaluationProtocolRepository(database)
        self._evaluation_runs = EvaluationRunRepository(database)
        self._telemetry = EvaluationTelemetryRepository(database)
        self._telemetry_collector = EvaluationTelemetryCollector(
            ModelWorkflow(database),
            EventRepository(database),
        )
        self._campaign_executor = campaign_executor
        self._active_studies: set[UUID] = set()

    async def run(self, study_id: UUID) -> EvaluationStudyResult:
        return await self._run_exclusive(study_id, recover=False)

    async def recover(self, study_id: UUID) -> EvaluationStudyResult:
        return await self._run_exclusive(study_id, recover=True)

    async def _run_exclusive(
        self,
        study_id: UUID,
        *,
        recover: bool,
    ) -> EvaluationStudyResult:
        if study_id in self._active_studies:
            raise StudyExecutionConflictError(
                "Evaluation Study is already active"
            )
        self._active_studies.add(study_id)
        try:
            return await self._drive(study_id, recover=recover)
        finally:
            self._active_studies.discard(study_id)

    async def _drive(
        self,
        study_id: UUID,
        *,
        recover: bool,
    ) -> EvaluationStudyResult:
        study = self._studies.get_study(study_id)
        if study.status in _TERMINAL_STUDY_STATUSES:
            return self._build_result(study)
        if study.status is EvaluationStudyStatus.DRAFT:
            raise StudyExecutionConflictError(
                "Evaluation Study must be authorized before execution"
            )
        if study.status is EvaluationStudyStatus.RUNNING and not recover:
            raise StudyExecutionConflictError(
                "Running Study requires explicit recovery"
            )
        if study.status is EvaluationStudyStatus.AUTHORIZED:
            study = self._studies.start(
                study_id,
                expected_version=study.record_version,
            )
        elif study.status is not EvaluationStudyStatus.RUNNING:
            raise StudyExecutionConflictError(
                "Evaluation Study state cannot be executed"
            )

        definition = self._studies.get_definition(study_id)
        bindings = self._validated_bindings(study, definition)
        has_infrastructure_gaps = any(
            binding.campaign_status
            is EvaluationCampaignStatus.COMPLETED_WITH_INVALID
            for binding in bindings
        )

        for binding in bindings:
            if binding.campaign_status is not None:
                terminal = self._terminal_study_status(
                    binding.campaign_status
                )
                if terminal is not None:
                    return self._finish(study_id, terminal)
                continue

            campaign = self._campaigns.get_campaign(binding.campaign_id)
            if campaign.status in _TERMINAL_CAMPAIGN_STATUSES:
                result = PilotCampaignResult(campaign=campaign)
            else:
                if not recover and (
                    campaign.status is not EvaluationCampaignStatus.CREATED
                ):
                    raise StudyExecutionConflictError(
                        "Active Campaign requires explicit Study recovery"
                    )
                result = (
                    await self._campaign_executor.recover_campaign(
                        binding.campaign_id
                    )
                    if recover
                    else await self._campaign_executor.run_campaign(
                        binding.campaign_id
                    )
                )
                campaign = self._campaigns.get_campaign(binding.campaign_id)
                if (
                    result.campaign.campaign_id != campaign.campaign_id
                    or result.campaign.status is not campaign.status
                ):
                    raise StudyExecutionConflictError(
                        "Campaign executor result does not match durable state"
                    )
            if campaign.status not in _TERMINAL_CAMPAIGN_STATUSES:
                raise StudyExecutionConflictError(
                    "Campaign executor returned a non-terminal result"
                )

            current_study = self._studies.get_study(study_id)
            self._studies.record_campaign_completion(
                study_id,
                campaign.campaign_id,
                campaign_status=campaign.status,
                expected_version=current_study.record_version,
            )
            self._reconcile_campaign_telemetry(campaign.campaign_id)
            terminal = self._terminal_study_status(campaign.status)
            if terminal is not None:
                return self._finish(study_id, terminal)
            if (
                campaign.status
                is EvaluationCampaignStatus.COMPLETED_WITH_INVALID
            ):
                has_infrastructure_gaps = True

        final_status = (
            EvaluationStudyStatus.COMPLETED_WITH_INFRASTRUCTURE_GAPS
            if has_infrastructure_gaps
            else EvaluationStudyStatus.COMPLETED
        )
        return self._finish(study_id, final_status)

    def _finish(
        self,
        study_id: UUID,
        status: EvaluationStudyStatus,
    ) -> EvaluationStudyResult:
        current = self._studies.get_study(study_id)
        if current.status in _TERMINAL_STUDY_STATUSES:
            if current.status is not status:
                raise StudyExecutionConflictError(
                    "Terminal Study status does not match durable Campaigns"
                )
            return self._build_result(current)
        finished = self._studies.finish(
            study_id,
            expected_version=current.record_version,
            status=status,
        )
        return self._build_result(finished)

    def _build_result(
        self,
        study: EvaluationStudy,
    ) -> EvaluationStudyResult:
        definition = self._studies.get_definition(study.study_id)
        bindings = self._validated_bindings(study, definition)
        campaigns = _as_four(
            tuple(
                self._campaigns.get_campaign(binding.campaign_id)
                for binding in bindings
            )
        )
        for campaign in campaigns:
            self._reconcile_campaign_telemetry(campaign.campaign_id)
        return EvaluationStudyResult(
            study=study,
            bindings=bindings,
            campaigns=campaigns,
        )

    def _validated_bindings(
        self,
        study: EvaluationStudy,
        definition: object,
    ) -> tuple[
        EvaluationStudyCampaignBinding,
        EvaluationStudyCampaignBinding,
        EvaluationStudyCampaignBinding,
        EvaluationStudyCampaignBinding,
    ]:
        from agentforge.evaluation.study_models import (
            EvaluationStudyDefinition,
        )

        if not isinstance(definition, EvaluationStudyDefinition):
            raise StudyExecutionConflictError(
                "Persisted Study definition is invalid"
            )
        bindings = _as_four(
            tuple(self._studies.list_campaign_bindings(study.study_id))
        )
        try:
            validated = validate_study_campaign_bindings(
                definition,
                study,
                bindings,
            )
        except ValueError as exc:
            raise StudyExecutionConflictError(
                "Study Campaign bindings failed validation"
            ) from exc
        for binding in validated:
            protocol = self._protocols.get(binding.protocol_digest)
            campaign = self._campaigns.get_campaign(binding.campaign_id)
            if (
                protocol.task_id != binding.task_id
                or protocol.protocol_digest != binding.protocol_digest
                or campaign.task_id != binding.task_id
                or campaign.protocol_digest != binding.protocol_digest
                or campaign.repetition_count
                != definition.repetitions_per_task
            ):
                raise StudyExecutionConflictError(
                    "Study Protocol or Campaign binding has drifted"
                )
            if (
                binding.campaign_status is not None
                and campaign.status is not binding.campaign_status
            ):
                raise StudyExecutionConflictError(
                    "Study Campaign completion has drifted"
                )
        return validated

    def _reconcile_campaign_telemetry(self, campaign_id: UUID) -> None:
        for result in self._evaluation_runs.list_for_campaign(campaign_id):
            if self._telemetry.find(result.evaluation_run_id) is None:
                self._telemetry.save(
                    self._telemetry_collector.collect(result)
                )

    @staticmethod
    def _terminal_study_status(
        campaign_status: EvaluationCampaignStatus,
    ) -> EvaluationStudyStatus | None:
        if campaign_status is EvaluationCampaignStatus.BLOCKED:
            return EvaluationStudyStatus.ABORTED_CONFIGURATION
        if campaign_status is EvaluationCampaignStatus.INDETERMINATE:
            return EvaluationStudyStatus.INDETERMINATE
        return None


def _as_four(
    values: Sequence[_T],
) -> tuple[_T, _T, _T, _T]:
    if len(values) != 4:
        raise StudyExecutionConflictError(
            "Evaluation Study requires exactly four bound values"
        )
    return cast(tuple[_T, _T, _T, _T], tuple(values))
