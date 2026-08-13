import os
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from agentforge.domain.enums import (
    ApprovalConsumptionState,
    EvaluationAttemptStatus,
    EvaluationCampaignStatus,
    EvaluationFailureClass,
    EvaluationOutcomeClass,
    EvaluationSlotStatus,
    MutationExecutionStatus,
    ProcessExecutionStatus,
)
from agentforge.evaluation.baseline_models import BaselineExecutionStatus
from agentforge.evaluation.baseline_persistence import (
    BaselineExecutionRepository,
    BaselineExecutionWorkflow,
)
from agentforge.evaluation.campaign_models import (
    EvaluationCampaign,
    EvaluationPilotAttempt,
    EvaluationSlot,
)
from agentforge.evaluation.campaign_persistence import (
    CampaignConflictError,
    EvaluationCampaignRepository,
)
from agentforge.evaluation.formal_fixtures import (
    FormalFixtureLoader,
    FormalFixtureManifest,
)
from agentforge.evaluation.models import RepairEvaluationRun
from agentforge.evaluation.persistence import EvaluationRunRepository
from agentforge.evaluation.pilot_factory import (
    PilotBindingMismatchError,
    PilotExecution,
)
from agentforge.evaluation.pilot_workspace import (
    PilotWorkspaceBinding,
    PilotWorkspaceLease,
    PilotWorkspaceManager,
)
from agentforge.evaluation.protocol import EvaluationProtocol
from agentforge.evaluation.protocol_persistence import (
    EvaluationProtocolRepository,
)
from agentforge.evaluation.provider_factory import (
    EvaluationProviderBindingError,
)
from agentforge.evaluation.reports import render_campaign_report
from agentforge.evaluation.selection import (
    EvaluationSelection,
    resolve_available_scored_runs,
    resolve_effective_runs,
)
from agentforge.evaluation.telemetry import EvaluationTelemetryCollector
from agentforge.evaluation.telemetry_persistence import (
    EvaluationTelemetryRepository,
)
from agentforge.persistence.database import Database
from agentforge.persistence.model_workflow import ModelWorkflow
from agentforge.persistence.mutations import MutationExecutionRepository
from agentforge.persistence.repositories import ApprovalRepository, EventRepository
from agentforge.persistence.test_executions import ProcessExecutionRepository

_TERMINAL_MUTATION_STATES = frozenset(
    {
        MutationExecutionStatus.COMMITTED,
        MutationExecutionStatus.FAILED,
    }
)
_TERMINAL_PROCESS_STATES = frozenset(
    {
        ProcessExecutionStatus.COMPLETED,
        ProcessExecutionStatus.FAILED,
        ProcessExecutionStatus.TIMEOUT,
        ProcessExecutionStatus.CANCELLED,
    }
)
_PILOT_CONFIGURATION_ERROR = "PILOT_CONFIGURATION_ERROR"
_TERMINAL_CAMPAIGN_STATUSES = frozenset(
    {
        EvaluationCampaignStatus.COMPLETED,
        EvaluationCampaignStatus.COMPLETED_WITH_INVALID,
        EvaluationCampaignStatus.BLOCKED,
        EvaluationCampaignStatus.INDETERMINATE,
    }
)


class PilotRuntimePreparer(Protocol):
    def prepare(
        self,
        protocol: EvaluationProtocol,
        manifest: FormalFixtureManifest,
        attempt: EvaluationPilotAttempt,
        lease: PilotWorkspaceLease,
    ) -> PilotExecution: ...


class PilotCampaignResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign: EvaluationCampaign
    selection: EvaluationSelection | None = None
    report_json: str | None = None


class PilotRunner:
    def __init__(
        self,
        database: Database,
        runtime_factory: PilotRuntimePreparer,
        workspace_manager: PilotWorkspaceManager,
        manifests: Mapping[str, Path],
    ) -> None:
        self._protocols = EvaluationProtocolRepository(database)
        self._campaigns = EvaluationCampaignRepository(database)
        self._evaluation_runs = EvaluationRunRepository(database)
        self._telemetry = EvaluationTelemetryRepository(database)
        self._telemetry_collector = EvaluationTelemetryCollector(
            ModelWorkflow(database),
            EventRepository(database),
        )
        self._baselines = BaselineExecutionRepository(database)
        self._baseline_workflow = BaselineExecutionWorkflow(database)
        self._approvals = ApprovalRepository(database)
        self._mutations = MutationExecutionRepository(database)
        self._processes = ProcessExecutionRepository(database)
        self._factory = runtime_factory
        self._workspaces = workspace_manager
        self._manifest_paths = dict(manifests)
        self._active_campaigns: set[UUID] = set()

    def create_campaign(
        self,
        protocol: EvaluationProtocol,
    ) -> EvaluationCampaign:
        validated = EvaluationProtocol.model_validate(
            protocol.model_dump(mode="json")
        )
        if validated.task_id not in self._manifest_paths:
            raise KeyError(
                f"No formal Fixture is registered for task {validated.task_id!r}"
            )
        self._protocols.register(validated)
        return self._campaigns.create_campaign(validated)

    async def run_campaign(self, campaign_id: UUID) -> PilotCampaignResult:
        return await self._run_exclusive(campaign_id, recover=False)

    async def recover_campaign(self, campaign_id: UUID) -> PilotCampaignResult:
        return await self._run_exclusive(campaign_id, recover=True)

    async def run_canary_slot(self, campaign_id: UUID) -> PilotCampaignResult:
        """Execute exactly one durable slot without finishing the campaign.

        This is intentionally a bounded operator probe.  It leaves the Study
        and Campaign recoverable so a later full run can continue from the
        durable slot state.
        """
        if campaign_id in self._active_campaigns:
            raise CampaignConflictError("Evaluation campaign is already active")
        self._active_campaigns.add(campaign_id)
        try:
            campaign = self._campaigns.get_campaign(campaign_id)
            protocol = self._protocols.get(campaign.protocol_digest)
            manifest = self._load_manifest(protocol.task_id)
            if campaign.status is EvaluationCampaignStatus.CREATED:
                campaign = self._campaigns.start_campaign(
                    campaign_id,
                    expected_version=campaign.record_version,
                )
            if campaign.status in _TERMINAL_CAMPAIGN_STATUSES:
                return self._result(protocol, campaign)
            slots = self._campaigns.list_slots(campaign_id)
            pending = next(
                (
                    slot
                    for slot in slots
                    if slot.status
                    not in {
                        EvaluationSlotStatus.ACCEPTED,
                        EvaluationSlotStatus.INVALID,
                        EvaluationSlotStatus.INDETERMINATE,
                    }
                ),
                None,
            )
            if pending is None:
                return self._result(protocol, campaign)
            await self._run_slot(protocol, manifest, pending, recover=True)
            return self._result(
                protocol,
                self._campaigns.get_campaign(campaign_id),
            )
        finally:
            self._active_campaigns.discard(campaign_id)

    async def _run_exclusive(
        self,
        campaign_id: UUID,
        *,
        recover: bool,
    ) -> PilotCampaignResult:
        if campaign_id in self._active_campaigns:
            raise CampaignConflictError("Evaluation campaign is already active")
        self._active_campaigns.add(campaign_id)
        try:
            if recover:
                self._reconcile_campaign(campaign_id)
            return await self._run_campaign(campaign_id, recover=recover)
        finally:
            self._active_campaigns.discard(campaign_id)

    async def _run_campaign(
        self,
        campaign_id: UUID,
        *,
        recover: bool,
    ) -> PilotCampaignResult:
        campaign = self._campaigns.get_campaign(campaign_id)
        protocol = self._protocols.get(campaign.protocol_digest)
        manifest = self._load_manifest(protocol.task_id)
        if campaign.status in {
            EvaluationCampaignStatus.COMPLETED,
            EvaluationCampaignStatus.COMPLETED_WITH_INVALID,
            EvaluationCampaignStatus.BLOCKED,
            EvaluationCampaignStatus.INDETERMINATE,
        }:
            return self._result(protocol, campaign)
        if campaign.status is EvaluationCampaignStatus.CREATED:
            campaign = self._campaigns.start_campaign(
                campaign_id,
                expected_version=campaign.record_version,
            )

        configuration_blocked = False
        for slot in self._campaigns.list_slots(campaign_id):
            await self._run_slot(protocol, manifest, slot, recover=recover)
            current_slot = self._campaigns.get_slot(slot.slot_id)
            if current_slot.status is EvaluationSlotStatus.INDETERMINATE:
                break
            if self._slot_has_configuration_failure(current_slot):
                configuration_blocked = True
                break
        slots = self._campaigns.list_slots(campaign_id)
        if configuration_blocked:
            final_status = EvaluationCampaignStatus.BLOCKED
        elif any(
            slot.status is EvaluationSlotStatus.INDETERMINATE for slot in slots
        ):
            final_status = EvaluationCampaignStatus.INDETERMINATE
        elif any(slot.status is EvaluationSlotStatus.INVALID for slot in slots):
            final_status = EvaluationCampaignStatus.COMPLETED_WITH_INVALID
        elif all(slot.status is EvaluationSlotStatus.ACCEPTED for slot in slots):
            final_status = EvaluationCampaignStatus.COMPLETED
        else:
            final_status = EvaluationCampaignStatus.BLOCKED
        campaign = self._campaigns.get_campaign(campaign_id)
        campaign = self._campaigns.finish_campaign(
            campaign_id,
            expected_version=campaign.record_version,
            status=final_status,
        )
        return self._result(protocol, campaign)

    async def _run_slot(
        self,
        protocol: EvaluationProtocol,
        manifest: FormalFixtureManifest,
        slot: EvaluationSlot,
        *,
        recover: bool,
    ) -> None:
        current = self._campaigns.get_slot(slot.slot_id)
        owns_slot = False
        while current.status not in {
            EvaluationSlotStatus.ACCEPTED,
            EvaluationSlotStatus.INVALID,
            EvaluationSlotStatus.INDETERMINATE,
        }:
            if current.status is EvaluationSlotStatus.PENDING:
                claimed = self._campaigns.claim_slot(
                    current.slot_id,
                    expected_version=current.record_version,
                )
                if claimed is None:
                    raise CampaignConflictError("Evaluation slot claim was lost")
                current = claimed
                owns_slot = True
            elif not recover and not owns_slot:
                raise CampaignConflictError(
                    "Evaluation slot requires explicit recovery"
                )
            if current.status not in {
                EvaluationSlotStatus.CLAIMED,
                EvaluationSlotStatus.REPLACEMENT_PENDING,
            }:
                raise CampaignConflictError(
                    "Evaluation slot has an unreconciled active attempt"
                )
            attempts = self._campaigns.list_attempts(current.slot_id)
            predecessor = attempts[-1] if attempts else None
            if current.status is EvaluationSlotStatus.REPLACEMENT_PENDING:
                if predecessor is None:
                    raise CampaignConflictError(
                        "Replacement slot has no predecessor attempt"
                    )
                attempt = self._campaigns.create_attempt(
                    current.slot_id,
                    expected_slot_version=current.record_version,
                    predecessor_attempt_id=predecessor.attempt_id,
                    predecessor_evaluation_run_id=(
                        predecessor.evaluation_run_id
                    ),
                )
            elif predecessor is None:
                attempt = self._campaigns.create_attempt(
                    current.slot_id,
                    expected_slot_version=current.record_version,
                )
            else:
                raise CampaignConflictError(
                    "Claimed slot already has an unreconciled attempt"
                )
            await self._execute_attempt(protocol, manifest, attempt)
            current = self._campaigns.get_slot(current.slot_id)

    async def _execute_attempt(
        self,
        protocol: EvaluationProtocol,
        manifest: FormalFixtureManifest,
        attempt: EvaluationPilotAttempt,
    ) -> None:
        lease: PilotWorkspaceLease | None = None
        try:
            lease = self._workspaces.create(
                manifest,
                PilotWorkspaceBinding(
                    campaign_id=attempt.campaign_id,
                    slot_id=attempt.slot_id,
                    attempt_id=attempt.attempt_id,
                    protocol_digest=protocol.protocol_digest,
                    fixture_asset_digest=protocol.fixture_asset_digest,
                ),
            )
            attempt = self._campaigns.mark_workspace_ready(
                attempt.attempt_id,
                expected_version=attempt.record_version,
                workspace_lease_id=lease.lease_id,
                workspace_root_digest=lease.workspace_root_digest,
                initial_workspace_digest=lease.initial_workspace_digest,
                workspace_path=lease.model_workspace,
            )
            prepared = self._factory.prepare(
                protocol,
                manifest,
                attempt,
                lease,
            )
            attempt = self._campaigns.mark_runtime_ready(
                attempt.attempt_id,
                expected_version=attempt.record_version,
                run_id=prepared.run.run_id,
                baseline_execution_id=(
                    prepared.baseline_execution.baseline_execution_id
                ),
            )
        except Exception as exc:
            current = self._campaigns.get_attempt(attempt.attempt_id)
            if current.status in {
                EvaluationAttemptStatus.CREATED,
                EvaluationAttemptStatus.WORKSPACE_READY,
                EvaluationAttemptStatus.RUNTIME_READY,
            }:
                failure_category = (
                    _PILOT_CONFIGURATION_ERROR
                    if isinstance(
                        exc,
                        (
                            PilotBindingMismatchError,
                            EvaluationProviderBindingError,
                        ),
                    )
                    else "PILOT_PREPARATION_ERROR"
                )
                invalid = self._campaigns.invalidate_pre_run_attempt(
                    current.attempt_id,
                    expected_version=current.record_version,
                    failure_category=failure_category,
                )
                self._finish_invalid_attempt(protocol, invalid)
                if lease is not None:
                    self._workspaces.cleanup(lease, terminal=True)
                return
            raise

        attempt = self._campaigns.start_attempt(
            attempt.attempt_id,
            expected_version=attempt.record_version,
        )
        try:
            result = await prepared.harness.execute(
                prepared.run.run_id,
                prepared.metadata,
            )
            self._validate_result_binding(protocol, manifest, attempt, result)
            result = self._evaluation_runs.save(result)
        except Exception:
            persisted = self._evaluation_runs.find_for_attempt(
                attempt.attempt_id
            )
            if persisted is not None:
                try:
                    self._validate_result_binding(
                        protocol,
                        manifest,
                        attempt,
                        persisted,
                    )
                except ValueError:
                    self._mark_execution_indeterminate(attempt)
                    return
                terminal = self._finish_result_attempt(
                    protocol,
                    attempt,
                    persisted,
                )
                if (
                    lease is not None
                    and terminal.status
                    is not EvaluationAttemptStatus.INDETERMINATE
                ):
                    self._workspaces.cleanup(lease, terminal=True)
                return
            self._mark_execution_indeterminate(attempt)
            return

        terminal = self._finish_result_attempt(protocol, attempt, result)
        if (
            lease is not None
            and terminal.status is not EvaluationAttemptStatus.INDETERMINATE
        ):
            self._workspaces.cleanup(lease, terminal=True)

    def _reconcile_campaign(self, campaign_id: UUID) -> None:
        campaign = self._campaigns.get_campaign(campaign_id)
        protocol = self._protocols.get(campaign.protocol_digest)
        if campaign.status in {
            EvaluationCampaignStatus.COMPLETED,
            EvaluationCampaignStatus.COMPLETED_WITH_INVALID,
            EvaluationCampaignStatus.BLOCKED,
            EvaluationCampaignStatus.INDETERMINATE,
        }:
            self._cleanup_terminal_campaign_workspaces(protocol, campaign_id)
            return
        for slot in self._campaigns.list_slots(campaign_id):
            self._reconcile_slot(protocol, slot)
            current_slot = self._campaigns.get_slot(slot.slot_id)
            if current_slot.status is EvaluationSlotStatus.INDETERMINATE:
                return
            if self._slot_has_configuration_failure(current_slot):
                return

    def _reconcile_slot(
        self,
        protocol: EvaluationProtocol,
        slot: EvaluationSlot,
    ) -> None:
        current_slot = self._campaigns.get_slot(slot.slot_id)
        if current_slot.status in {
            EvaluationSlotStatus.ACCEPTED,
            EvaluationSlotStatus.INVALID,
        }:
            for attempt in self._campaigns.list_attempts(current_slot.slot_id):
                if attempt.status is not EvaluationAttemptStatus.INDETERMINATE:
                    self._cleanup_attempt_workspace(protocol, attempt)
            return
        if current_slot.status in {
            EvaluationSlotStatus.PENDING,
            EvaluationSlotStatus.INDETERMINATE,
        }:
            return
        attempts = self._campaigns.list_attempts(current_slot.slot_id)
        if not attempts:
            if current_slot.status is EvaluationSlotStatus.CLAIMED:
                return
            self._campaigns.finish_slot(
                current_slot.slot_id,
                expected_version=current_slot.record_version,
                status=EvaluationSlotStatus.INDETERMINATE,
            )
            return
        attempt = attempts[-1]
        if attempt.status is EvaluationAttemptStatus.COMPLETED:
            result = self._evaluation_runs.find_for_attempt(attempt.attempt_id)
            if result is None:
                self._finish_slot_indeterminate(current_slot)
                return
            manifest = self._load_manifest(protocol.task_id)
            self._validate_result_binding(protocol, manifest, attempt, result)
            self._ensure_result_telemetry(result)
            self._campaigns.accept_slot(
                current_slot.slot_id,
                expected_version=current_slot.record_version,
                attempt_id=attempt.attempt_id,
                evaluation_run_id=result.evaluation_run_id,
            )
            self._cleanup_attempt_workspace(protocol, attempt)
            return
        if attempt.status is EvaluationAttemptStatus.INVALID:
            self._reconcile_terminal_attempt_result(protocol, attempt)
            if current_slot.status is not EvaluationSlotStatus.REPLACEMENT_PENDING:
                self._finish_invalid_attempt(protocol, attempt)
            self._cleanup_attempt_workspace(protocol, attempt)
            return
        if attempt.status is EvaluationAttemptStatus.INDETERMINATE:
            self._reconcile_terminal_attempt_result(protocol, attempt)
            self._finish_slot_indeterminate(current_slot)
            return
        if attempt.status in {
            EvaluationAttemptStatus.CREATED,
            EvaluationAttemptStatus.WORKSPACE_READY,
            EvaluationAttemptStatus.RUNTIME_READY,
        }:
            invalid = self._campaigns.invalidate_pre_run_attempt(
                attempt.attempt_id,
                expected_version=attempt.record_version,
                failure_category="PILOT_PREPARATION_ERROR",
            )
            self._finish_invalid_attempt(protocol, invalid)
            self._cleanup_attempt_workspace(protocol, invalid)
            return
        self._reconcile_running_attempt(protocol, current_slot, attempt)

    def _reconcile_running_attempt(
        self,
        protocol: EvaluationProtocol,
        slot: EvaluationSlot,
        attempt: EvaluationPilotAttempt,
    ) -> None:
        result = self._evaluation_runs.find_for_attempt(attempt.attempt_id)
        if result is not None:
            manifest = self._load_manifest(protocol.task_id)
            self._validate_result_binding(protocol, manifest, attempt, result)
            terminal = self._finish_result_attempt(protocol, attempt, result)
            if terminal.status is not EvaluationAttemptStatus.INDETERMINATE:
                self._cleanup_attempt_workspace(protocol, terminal)
            return
        if self._has_indeterminate_side_effect(attempt):
            indeterminate = self._campaigns.mark_attempt_indeterminate(
                attempt.attempt_id,
                expected_version=attempt.record_version,
                failure_category="PILOT_SIDE_EFFECT_INDETERMINATE",
            )
            self._finish_slot_indeterminate(
                self._campaigns.get_slot(indeterminate.slot_id)
            )
            return
        invalid = self._campaigns.finish_attempt(
            attempt.attempt_id,
            expected_version=attempt.record_version,
            status=EvaluationAttemptStatus.INVALID,
            failure_category="PILOT_RECOVERY_INTERRUPTED",
            infrastructure_failure=True,
        )
        self._finish_invalid_attempt(protocol, invalid)
        self._cleanup_attempt_workspace(protocol, invalid)

    def _has_indeterminate_side_effect(
        self,
        attempt: EvaluationPilotAttempt,
    ) -> bool:
        if attempt.run_id is None:
            return True
        baseline = self._baselines.find_for_run(attempt.run_id)
        if baseline is not None:
            if baseline.status is BaselineExecutionStatus.STARTED:
                baseline = self._baseline_workflow.recover(attempt.run_id)
            if baseline.status is BaselineExecutionStatus.INDETERMINATE:
                return True
        mutations = self._mutations.list_for_run(attempt.run_id)
        processes = self._processes.list_for_run(attempt.run_id)
        if any(
            item.status
            in {
                MutationExecutionStatus.WRITING,
                MutationExecutionStatus.INDETERMINATE,
            }
            for item in mutations
        ):
            return True
        if any(
            item.status
            in {
                ProcessExecutionStatus.STARTED,
                ProcessExecutionStatus.INDETERMINATE,
            }
            for item in processes
        ):
            return True
        mutations_by_approval = {item.approval_id: item for item in mutations}
        processes_by_approval = {item.approval_id: item for item in processes}
        for approval in self._approvals.list_for_run(attempt.run_id):
            if (
                approval.consumption_state
                is ApprovalConsumptionState.INDETERMINATE
            ):
                return True
            if approval.consumption_state is not ApprovalConsumptionState.CLAIMED:
                continue
            mutation = mutations_by_approval.get(approval.approval_id)
            process = processes_by_approval.get(approval.approval_id)
            if mutation is None and process is None:
                return True
            if (
                mutation is not None
                and mutation.status not in _TERMINAL_MUTATION_STATES
            ):
                return True
            if (
                process is not None
                and process.status not in _TERMINAL_PROCESS_STATES
            ):
                return True
        return False

    def _finish_result_attempt(
        self,
        protocol: EvaluationProtocol,
        attempt: EvaluationPilotAttempt,
        result: RepairEvaluationRun,
    ) -> EvaluationPilotAttempt:
        self._ensure_result_telemetry(result)
        attempt_status = {
            EvaluationOutcomeClass.SCORED: EvaluationAttemptStatus.COMPLETED,
            EvaluationOutcomeClass.INFRASTRUCTURE_INVALID: (
                EvaluationAttemptStatus.INVALID
            ),
            EvaluationOutcomeClass.INDETERMINATE: (
                EvaluationAttemptStatus.INDETERMINATE
            ),
        }[result.outcome_class]
        terminal = self._campaigns.finish_attempt(
            attempt.attempt_id,
            expected_version=attempt.record_version,
            status=attempt_status,
            evaluation_run_id=result.evaluation_run_id,
            failure_category=result.failure_category,
            infrastructure_failure=result.infrastructure_failure,
        )
        if terminal.status is EvaluationAttemptStatus.INDETERMINATE:
            self._finish_slot_indeterminate(
                self._campaigns.get_slot(terminal.slot_id)
            )
            return terminal
        if terminal.status is EvaluationAttemptStatus.INVALID:
            self._finish_invalid_attempt(protocol, terminal)
            return terminal
        slot = self._campaigns.get_slot(terminal.slot_id)
        self._campaigns.accept_slot(
            slot.slot_id,
            expected_version=slot.record_version,
            attempt_id=terminal.attempt_id,
            evaluation_run_id=terminal.evaluation_run_id,
        )
        return terminal

    def _reconcile_terminal_attempt_result(
        self,
        protocol: EvaluationProtocol,
        attempt: EvaluationPilotAttempt,
    ) -> None:
        if attempt.evaluation_run_id is None:
            return
        result = self._evaluation_runs.get(attempt.evaluation_run_id)
        manifest = self._load_manifest(protocol.task_id)
        self._validate_result_binding(protocol, manifest, attempt, result)
        self._ensure_result_telemetry(result)

    def _ensure_result_telemetry(
        self,
        result: RepairEvaluationRun,
    ) -> None:
        self._telemetry.save(self._telemetry_collector.collect(result))

    def _slot_has_configuration_failure(self, slot: EvaluationSlot) -> bool:
        for attempt in reversed(
            self._campaigns.list_attempts(slot.slot_id)
        ):
            if attempt.failure_category == _PILOT_CONFIGURATION_ERROR:
                return True
            if attempt.evaluation_run_id is None:
                continue
            result = self._evaluation_runs.get(attempt.evaluation_run_id)
            return result.failure_class is EvaluationFailureClass.CONFIGURATION
        return False

    def _finish_slot_indeterminate(self, slot: EvaluationSlot) -> None:
        self._campaigns.finish_slot(
            slot.slot_id,
            expected_version=slot.record_version,
            status=EvaluationSlotStatus.INDETERMINATE,
        )

    def _mark_execution_indeterminate(
        self,
        attempt: EvaluationPilotAttempt,
    ) -> None:
        indeterminate = self._campaigns.mark_attempt_indeterminate(
            attempt.attempt_id,
            expected_version=attempt.record_version,
            failure_category="PILOT_EXECUTION_INDETERMINATE",
        )
        self._finish_slot_indeterminate(
            self._campaigns.get_slot(indeterminate.slot_id)
        )

    def _cleanup_attempt_workspace(
        self,
        protocol: EvaluationProtocol,
        attempt: EvaluationPilotAttempt,
    ) -> bool:
        binding = PilotWorkspaceBinding(
            campaign_id=attempt.campaign_id,
            slot_id=attempt.slot_id,
            attempt_id=attempt.attempt_id,
            protocol_digest=protocol.protocol_digest,
            fixture_asset_digest=protocol.fixture_asset_digest,
        )
        try:
            lease = (
                self._workspaces.reopen(
                    attempt.workspace_lease_id,
                    binding,
                )
                if attempt.workspace_lease_id is not None
                else self._workspaces.reopen_for_binding(binding)
            )
        except ValueError:
            return False
        return self._workspaces.cleanup(lease, terminal=True)

    def _cleanup_terminal_campaign_workspaces(
        self,
        protocol: EvaluationProtocol,
        campaign_id: UUID,
    ) -> None:
        for slot in self._campaigns.list_slots(campaign_id):
            if slot.status not in {
                EvaluationSlotStatus.ACCEPTED,
                EvaluationSlotStatus.INVALID,
            }:
                continue
            for attempt in self._campaigns.list_attempts(slot.slot_id):
                if attempt.status is not EvaluationAttemptStatus.INDETERMINATE:
                    self._cleanup_attempt_workspace(protocol, attempt)

    def _finish_invalid_attempt(
        self,
        protocol: EvaluationProtocol,
        attempt: EvaluationPilotAttempt,
    ) -> None:
        slot = self._campaigns.get_slot(attempt.slot_id)
        replacements_used = attempt.attempt_number - 1
        if (
            attempt.failure_category is not None
            and protocol.replacement_policy.allows(attempt.failure_category)
            and replacements_used
            < protocol.replacement_policy.max_replacements_per_slot
        ):
            self._campaigns.request_replacement(
                slot.slot_id,
                expected_version=slot.record_version,
                predecessor_attempt_id=attempt.attempt_id,
            )
            return
        self._campaigns.finish_slot(
            slot.slot_id,
            expected_version=slot.record_version,
            status=EvaluationSlotStatus.INVALID,
        )

    def _result(
        self,
        protocol: EvaluationProtocol,
        campaign: EvaluationCampaign,
    ) -> PilotCampaignResult:
        if campaign.status not in {
            EvaluationCampaignStatus.COMPLETED,
            EvaluationCampaignStatus.COMPLETED_WITH_INVALID,
        }:
            return PilotCampaignResult(campaign=campaign)
        slots = self._campaigns.list_slots(campaign.campaign_id)
        attempts = [
            attempt
            for slot in slots
            for attempt in self._campaigns.list_attempts(slot.slot_id)
        ]
        runs = self._evaluation_runs.list_for_campaign(campaign.campaign_id)
        if campaign.status is EvaluationCampaignStatus.COMPLETED:
            selection = resolve_effective_runs(
                protocol,
                campaign,
                slots,
                attempts,
                runs,
            )
        elif any(
            slot.status is EvaluationSlotStatus.ACCEPTED for slot in slots
        ):
            selection = resolve_available_scored_runs(
                protocol,
                campaign,
                slots,
                attempts,
                runs,
            )
        else:
            return PilotCampaignResult(campaign=campaign)
        return PilotCampaignResult(
            campaign=campaign,
            selection=selection,
            report_json=render_campaign_report(protocol, selection),
        )

    def _load_manifest(self, task_id: str) -> FormalFixtureManifest:
        try:
            path = self._manifest_paths[task_id]
        except KeyError as exc:
            raise KeyError(
                f"No formal Fixture is registered for task {task_id!r}"
            ) from exc
        manifest = FormalFixtureLoader().load(path)
        if manifest.task_id != task_id:
            raise ValueError("Formal Fixture registry task binding does not match")
        return manifest

    @staticmethod
    def _validate_result_binding(
        protocol: EvaluationProtocol,
        manifest: FormalFixtureManifest,
        attempt: EvaluationPilotAttempt,
        result: RepairEvaluationRun,
    ) -> None:
        policy = manifest.to_policy(
            path_case_sensitive=(
                os.path.normcase("AgentForge")
                != os.path.normcase("agentforge")
            )
        )
        if (
            result.protocol_digest != protocol.protocol_digest
            or result.campaign_id != attempt.campaign_id
            or result.slot_id != attempt.slot_id
            or result.attempt_id != attempt.attempt_id
            or result.attempt_number != attempt.attempt_number
            or result.repetition_index != attempt.repetition_index
            or result.task_id != attempt.task_id
            or result.run_id != attempt.run_id
            or result.baseline_execution_id != attempt.baseline_execution_id
            or result.replacement_for_evaluation_run_id
            != attempt.predecessor_evaluation_run_id
            or result.model_id != protocol.provider_binding.model_id
            or result.model_parameters_digest
            != protocol.provider_binding.configuration_digest
            or result.system_prompt_digest != protocol.system_prompt_digest
            or result.task_prompt_digest != protocol.task_prompt_digest
            or result.tool_schema_digest != protocol.tool_schema_digest
            or result.context_policy_version
            != int(protocol.context_policy.version)
            or attempt.initial_workspace_digest is None
            or result.initial_workspace_digest
            != attempt.initial_workspace_digest
            or result.task_policy_digest != protocol.task_policy_digest
            or result.task_policy_digest != policy.policy_digest
            or result.budget_profile != policy.budget_profile
            or result.completion_correction_mode
            != protocol.completion_correction_mode
        ):
            raise ValueError("Evaluation result does not match Pilot attempt")
