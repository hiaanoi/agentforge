import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_pilot_campaign_runner import ScriptedFactory, protocol_for
from test_pilot_runtime_factory import TASK_ROOT

from agentforge.domain.enums import (
    ApprovalConsumptionState,
    ApprovalStatus,
    EvaluationAttemptStatus,
    EvaluationCampaignStatus,
    EvaluationSlotStatus,
    MutationExecutionStatus,
    ProcessExecutionStatus,
)
from agentforge.domain.models import ApprovalRequest, utc_now
from agentforge.domain.mutations import MutationExecutionRecord
from agentforge.domain.repair import RepairCompletionStatus
from agentforge.domain.test_execution import (
    ProcessExecutionRecord,
    TestExecutionPlan,
)
from agentforge.evaluation.baseline_models import (
    BaselineExecutionRecord,
    BaselineExecutionStatus,
)
from agentforge.evaluation.baseline_persistence import (
    BaselineExecutionRepository,
    BaselineExecutionWorkflow,
)
from agentforge.evaluation.campaign_models import EvaluationPilotAttempt
from agentforge.evaluation.campaign_persistence import (
    CampaignConflictError,
    EvaluationCampaignRepository,
)
from agentforge.evaluation.formal_fixtures import FormalFixtureLoader
from agentforge.evaluation.persistence import EvaluationRunRepository
from agentforge.evaluation.pilot_runner import PilotRunner
from agentforge.evaluation.pilot_workspace import (
    PilotWorkspaceBinding,
    PilotWorkspaceLease,
    PilotWorkspaceManager,
)
from agentforge.evaluation.protocol import EvaluationProtocol, ReplacementPolicy
from agentforge.evaluation.telemetry_persistence import (
    EvaluationTelemetryRepository,
)
from agentforge.persistence.database import Database
from agentforge.persistence.mutations import MutationExecutionRepository
from agentforge.persistence.repositories import (
    ApprovalRepository,
    CheckpointRepository,
)
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.persistence.test_executions import ProcessExecutionRepository

SHA = "e" * 64
SUCCESS = (RepairCompletionStatus.VERIFIED_SUCCESS, False, None)
PYTHON_EXECUTABLE = str(Path(sys.executable).resolve())


def recovery_protocol(tmp_path: Path) -> EvaluationProtocol:
    protocol = protocol_for(tmp_path)
    updated = protocol.model_copy(
        update={
            "replacement_policy": ReplacementPolicy(
                max_replacements_per_slot=1,
                replaceable_failure_categories=(
                    "MODEL_TIMEOUT",
                    "PILOT_PREPARATION_ERROR",
                    "PILOT_RECOVERY_INTERRUPTED",
                    "WORKSPACE_PREPARATION_ERROR",
                ),
            ),
            "protocol_digest": "",
        }
    )
    return EvaluationProtocol.model_validate(updated.model_dump(mode="json"))


def recovery_runner(
    tmp_path: Path,
    outcomes: list[tuple[RepairCompletionStatus, bool, str | None]],
) -> tuple[
    Database,
    ScriptedFactory,
    PilotRunner,
    PilotWorkspaceManager,
    EvaluationProtocol,
]:
    database = Database.from_path(tmp_path / "campaign.sqlite3")
    database.create_schema()
    factory = ScriptedFactory(database, outcomes)
    workspaces = PilotWorkspaceManager(tmp_path / "pilot-workspaces")
    protocol = recovery_protocol(tmp_path)
    pilot = PilotRunner(
        database,
        factory,
        workspaces,
        {FormalFixtureLoader().load(TASK_ROOT).task_id: TASK_ROOT},
    )
    return database, factory, pilot, workspaces, protocol


def claim_first_attempt(
    repository: EvaluationCampaignRepository,
    campaign_id,
) -> EvaluationPilotAttempt:
    campaign = repository.get_campaign(campaign_id)
    repository.start_campaign(
        campaign_id,
        expected_version=campaign.record_version,
    )
    slot = repository.list_slots(campaign_id)[0]
    claimed = repository.claim_slot(
        slot.slot_id,
        expected_version=slot.record_version,
    )
    assert claimed is not None
    return repository.create_attempt(
        claimed.slot_id,
        expected_slot_version=claimed.record_version,
    )


def create_lease(
    workspaces: PilotWorkspaceManager,
    protocol: EvaluationProtocol,
    attempt: EvaluationPilotAttempt,
) -> PilotWorkspaceLease:
    return workspaces.create(
        FormalFixtureLoader().load(TASK_ROOT),
        PilotWorkspaceBinding(
            campaign_id=attempt.campaign_id,
            slot_id=attempt.slot_id,
            attempt_id=attempt.attempt_id,
            protocol_digest=protocol.protocol_digest,
            fixture_asset_digest=protocol.fixture_asset_digest,
        ),
    )


def mark_workspace_ready(
    repository: EvaluationCampaignRepository,
    attempt: EvaluationPilotAttempt,
    lease: PilotWorkspaceLease,
) -> EvaluationPilotAttempt:
    return repository.mark_workspace_ready(
        attempt.attempt_id,
        expected_version=attempt.record_version,
        workspace_lease_id=lease.lease_id,
        workspace_root_digest=lease.workspace_root_digest,
        initial_workspace_digest=lease.initial_workspace_digest,
        workspace_path=lease.model_workspace,
    )


def prepare_running_attempt(
    repository: EvaluationCampaignRepository,
    factory: ScriptedFactory,
    workspaces: PilotWorkspaceManager,
    protocol: EvaluationProtocol,
    attempt: EvaluationPilotAttempt,
):
    manifest = FormalFixtureLoader().load(TASK_ROOT)
    lease = create_lease(workspaces, protocol, attempt)
    workspace_ready = mark_workspace_ready(repository, attempt, lease)
    prepared = factory.prepare(protocol, manifest, workspace_ready, lease)
    runtime_ready = repository.mark_runtime_ready(
        attempt.attempt_id,
        expected_version=workspace_ready.record_version,
        run_id=prepared.run.run_id,
        baseline_execution_id=prepared.baseline_execution.baseline_execution_id,
    )
    running = repository.start_attempt(
        attempt.attempt_id,
        expected_version=runtime_ready.record_version,
    )
    return running, lease, prepared


@pytest.mark.asyncio
async def test_recovery_continues_claimed_slot_without_an_attempt(
    tmp_path: Path,
) -> None:
    database, factory, pilot, _, protocol = recovery_runner(
        tmp_path,
        [SUCCESS, SUCCESS, SUCCESS],
    )
    campaign = pilot.create_campaign(protocol)
    repository = EvaluationCampaignRepository(database)
    repository.start_campaign(
        campaign.campaign_id,
        expected_version=campaign.record_version,
    )
    first = repository.list_slots(campaign.campaign_id)[0]
    claimed = repository.claim_slot(
        first.slot_id,
        expected_version=first.record_version,
    )
    assert claimed is not None

    with pytest.raises(CampaignConflictError, match="explicit recovery"):
        await pilot.run_campaign(campaign.campaign_id)
    result = await pilot.recover_campaign(campaign.campaign_id)

    assert result.campaign.status is EvaluationCampaignStatus.COMPLETED
    assert factory.prepare_count == 3
    database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "orphan_status",
    [
        EvaluationAttemptStatus.CREATED,
        EvaluationAttemptStatus.WORKSPACE_READY,
        EvaluationAttemptStatus.RUNTIME_READY,
    ],
)
async def test_pre_run_orphan_is_invalidated_cleaned_and_replaced(
    tmp_path: Path,
    orphan_status: EvaluationAttemptStatus,
) -> None:
    database, factory, pilot, workspaces, protocol = recovery_runner(
        tmp_path,
        [SUCCESS, SUCCESS, SUCCESS],
    )
    campaign = pilot.create_campaign(protocol)
    repository = EvaluationCampaignRepository(database)
    attempt = claim_first_attempt(repository, campaign.campaign_id)
    lease: PilotWorkspaceLease | None = None
    lease = create_lease(workspaces, protocol, attempt)
    if orphan_status is not EvaluationAttemptStatus.CREATED:
        attempt = mark_workspace_ready(repository, attempt, lease)
    if orphan_status is EvaluationAttemptStatus.RUNTIME_READY:
        attempt = repository.mark_runtime_ready(
            attempt.attempt_id,
            expected_version=attempt.record_version,
            run_id=uuid4(),
            baseline_execution_id=uuid4(),
        )

    result = await pilot.recover_campaign(campaign.campaign_id)

    attempts = repository.list_attempts(attempt.slot_id)
    assert result.campaign.status is EvaluationCampaignStatus.COMPLETED
    assert [item.status for item in attempts] == [
        EvaluationAttemptStatus.INVALID,
        EvaluationAttemptStatus.COMPLETED,
    ]
    assert attempts[1].predecessor_attempt_id == attempts[0].attempt_id
    assert attempts[1].attempt_id != attempts[0].attempt_id
    assert attempts[0].failure_category == "PILOT_PREPARATION_ERROR"
    assert result.selection is not None
    assert result.selection.infrastructure_invalid_count == 1
    assert result.selection.replacement_count == 1
    assert factory.prepare_count == 3
    assert not lease.model_workspace.exists()
    database.close()


@pytest.mark.asyncio
async def test_persisted_result_finalizes_running_attempt_without_reexecution(
    tmp_path: Path,
) -> None:
    database, factory, pilot, workspaces, protocol = recovery_runner(
        tmp_path,
        [SUCCESS, SUCCESS, SUCCESS],
    )
    campaign = pilot.create_campaign(protocol)
    repository = EvaluationCampaignRepository(database)
    attempt = claim_first_attempt(repository, campaign.campaign_id)
    running, lease, prepared = prepare_running_attempt(
        repository,
        factory,
        workspaces,
        protocol,
        attempt,
    )
    persisted = await prepared.harness.execute(
        prepared.run.run_id,
        prepared.metadata,
    )
    EvaluationRunRepository(database).save(persisted)

    result = await pilot.recover_campaign(campaign.campaign_id)
    repeated = await pilot.recover_campaign(campaign.campaign_id)

    recovered = repository.get_attempt(running.attempt_id)
    assert repeated == result
    assert result.campaign.status is EvaluationCampaignStatus.COMPLETED
    assert recovered.status is EvaluationAttemptStatus.COMPLETED
    assert recovered.evaluation_run_id == persisted.evaluation_run_id
    assert factory.prepare_count == 3
    assert not lease.model_workspace.exists()
    database.close()


@pytest.mark.asyncio
async def test_completed_attempt_is_selected_without_reexecution(
    tmp_path: Path,
) -> None:
    database, factory, pilot, workspaces, protocol = recovery_runner(
        tmp_path,
        [SUCCESS, SUCCESS, SUCCESS],
    )
    campaign = pilot.create_campaign(protocol)
    repository = EvaluationCampaignRepository(database)
    attempt = claim_first_attempt(repository, campaign.campaign_id)
    running, lease, prepared = prepare_running_attempt(
        repository,
        factory,
        workspaces,
        protocol,
        attempt,
    )
    persisted = EvaluationRunRepository(database).save(
        await prepared.harness.execute(
            prepared.run.run_id,
            prepared.metadata,
        )
    )
    repository.finish_attempt(
        running.attempt_id,
        expected_version=running.record_version,
        status=EvaluationAttemptStatus.COMPLETED,
        evaluation_run_id=persisted.evaluation_run_id,
        infrastructure_failure=False,
    )
    telemetry = EvaluationTelemetryRepository(database)
    assert telemetry.find(persisted.evaluation_run_id) is None

    result = await pilot.recover_campaign(campaign.campaign_id)

    first_slot = repository.list_slots(campaign.campaign_id)[0]
    assert result.campaign.status is EvaluationCampaignStatus.COMPLETED
    assert first_slot.status is EvaluationSlotStatus.ACCEPTED
    assert first_slot.selected_evaluation_run_id == persisted.evaluation_run_id
    assert telemetry.get(persisted.evaluation_run_id).run_id == persisted.run_id
    assert factory.prepare_count == 3
    assert not lease.model_workspace.exists()
    database.close()


@pytest.mark.asyncio
async def test_recovery_continues_persisted_replacement_decision(
    tmp_path: Path,
) -> None:
    database, factory, pilot, _, protocol = recovery_runner(
        tmp_path,
        [SUCCESS, SUCCESS, SUCCESS],
    )
    campaign = pilot.create_campaign(protocol)
    repository = EvaluationCampaignRepository(database)
    attempt = claim_first_attempt(repository, campaign.campaign_id)
    workspace_ready = repository.mark_workspace_ready(
        attempt.attempt_id,
        expected_version=attempt.record_version,
        workspace_lease_id=uuid4(),
        workspace_root_digest=SHA,
        initial_workspace_digest=SHA,
        workspace_path=tmp_path / "interrupted-workspace",
    )
    runtime_ready = repository.mark_runtime_ready(
        attempt.attempt_id,
        expected_version=workspace_ready.record_version,
        run_id=uuid4(),
        baseline_execution_id=uuid4(),
    )
    running = repository.start_attempt(
        attempt.attempt_id,
        expected_version=runtime_ready.record_version,
    )
    invalid = repository.finish_attempt(
        attempt.attempt_id,
        expected_version=running.record_version,
        status=EvaluationAttemptStatus.INVALID,
        failure_category="MODEL_TIMEOUT",
        infrastructure_failure=True,
    )
    slot = repository.get_slot(attempt.slot_id)
    repository.request_replacement(
        slot.slot_id,
        expected_version=slot.record_version,
        predecessor_attempt_id=invalid.attempt_id,
    )

    result = await pilot.recover_campaign(campaign.campaign_id)

    attempts = repository.list_attempts(attempt.slot_id)
    assert result.campaign.status is EvaluationCampaignStatus.COMPLETED
    assert [item.status for item in attempts] == [
        EvaluationAttemptStatus.INVALID,
        EvaluationAttemptStatus.COMPLETED,
    ]
    assert attempts[1].predecessor_attempt_id == attempts[0].attempt_id
    assert factory.prepare_count == 3
    database.close()


@pytest.mark.asyncio
async def test_running_orphan_without_side_effect_fact_uses_fresh_replacement(
    tmp_path: Path,
) -> None:
    database, factory, pilot, workspaces, protocol = recovery_runner(
        tmp_path,
        [SUCCESS, SUCCESS, SUCCESS, SUCCESS],
    )
    campaign = pilot.create_campaign(protocol)
    repository = EvaluationCampaignRepository(database)
    attempt = claim_first_attempt(repository, campaign.campaign_id)
    running, lease, _ = prepare_running_attempt(
        repository,
        factory,
        workspaces,
        protocol,
        attempt,
    )

    result = await pilot.recover_campaign(campaign.campaign_id)

    attempts = repository.list_attempts(running.slot_id)
    assert result.campaign.status is EvaluationCampaignStatus.COMPLETED
    assert [item.status for item in attempts] == [
        EvaluationAttemptStatus.INVALID,
        EvaluationAttemptStatus.COMPLETED,
    ]
    assert attempts[0].failure_category == "PILOT_RECOVERY_INTERRUPTED"
    assert attempts[1].attempt_id != attempts[0].attempt_id
    assert factory.prepare_count == 4
    assert not lease.model_workspace.exists()
    database.close()


@pytest.mark.asyncio
async def test_started_baseline_makes_campaign_indeterminate_without_replacement(
    tmp_path: Path,
) -> None:
    database, factory, pilot, workspaces, protocol = recovery_runner(
        tmp_path,
        [SUCCESS],
    )
    campaign = pilot.create_campaign(protocol)
    repository = EvaluationCampaignRepository(database)
    attempt = claim_first_attempt(repository, campaign.campaign_id)
    running, lease, prepared = prepare_running_attempt(
        repository,
        factory,
        workspaces,
        protocol,
        attempt,
    )
    manifest = FormalFixtureLoader().load(TASK_ROOT)
    baseline = BaselineExecutionRepository(database).create(
        BaselineExecutionRecord(
            baseline_execution_id=prepared.baseline_execution.baseline_execution_id,
            run_id=prepared.run.run_id,
            task_id=manifest.task_id,
            workspace_baseline_id=uuid4(),
            initial_workspace_digest=lease.initial_workspace_digest,
            test_plan=TestExecutionPlan(
                profile_id="visible-self-durable-double-consumption",
                profile_version=1,
                profile_digest="1" * 64,
                executable_path=PYTHON_EXECUTABLE,
                argv_digest="2" * 64,
                cwd=str(lease.model_workspace),
                environment_digest="3" * 64,
            ),
            expected_failure=manifest.expected_baseline_failure,
        )
    )
    claimed = BaselineExecutionWorkflow(database).claim(
        prepared.run.run_id,
        expected_version=baseline.record_version,
    )
    assert claimed is not None

    result = await pilot.recover_campaign(campaign.campaign_id)

    recovered = repository.get_attempt(running.attempt_id)
    recovered_baseline = BaselineExecutionRepository(database).get_for_run(prepared.run.run_id)
    assert result.campaign.status is EvaluationCampaignStatus.INDETERMINATE
    assert recovered.status is EvaluationAttemptStatus.INDETERMINATE
    assert recovered_baseline.status is BaselineExecutionStatus.INDETERMINATE
    assert repository.list_attempts(running.slot_id) == [recovered]
    assert factory.prepare_count == 1
    assert lease.model_workspace.exists()
    database.close()


@pytest.mark.asyncio
async def test_result_rejects_every_drifted_frozen_binding(
    tmp_path: Path,
) -> None:
    database, factory, _, workspaces, protocol = recovery_runner(
        tmp_path,
        [SUCCESS],
    )
    pilot = PilotRunner(
        database,
        factory,
        workspaces,
        {protocol.task_id: TASK_ROOT},
    )
    campaign = pilot.create_campaign(protocol)
    repository = EvaluationCampaignRepository(database)
    attempt = claim_first_attempt(repository, campaign.campaign_id)
    running, _, prepared = prepare_running_attempt(
        repository,
        factory,
        workspaces,
        protocol,
        attempt,
    )
    result = await prepared.harness.execute(
        prepared.run.run_id,
        prepared.metadata,
    )
    manifest = FormalFixtureLoader().load(TASK_ROOT)
    mismatches = {
        "model_id": "wrong-model",
        "model_parameters_digest": "4" * 64,
        "system_prompt_digest": "5" * 64,
        "task_prompt_digest": "6" * 64,
        "tool_schema_digest": "7" * 64,
        "context_policy_version": 999,
        "initial_workspace_digest": "8" * 64,
        "task_policy_digest": "9" * 64,
        "budget_profile": "BASIC",
        "completion_correction_mode": "STRICT",
    }

    for field, value in mismatches.items():
        with pytest.raises(ValueError, match="does not match"):
            PilotRunner._validate_result_binding(
                protocol,
                manifest,
                running,
                result.model_copy(update={field: value}),
            )
    database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uncertain_fact",
    ["mutation_writing", "process_started", "approval_claimed"],
)
async def test_uncertain_side_effect_makes_campaign_indeterminate_without_replacement(
    tmp_path: Path,
    uncertain_fact: str,
) -> None:
    database, factory, pilot, workspaces, protocol = recovery_runner(
        tmp_path,
        [SUCCESS],
    )
    campaign = pilot.create_campaign(protocol)
    repository = EvaluationCampaignRepository(database)
    attempt = claim_first_attempt(repository, campaign.campaign_id)
    running, lease, prepared = prepare_running_attempt(
        repository,
        factory,
        workspaces,
        protocol,
        attempt,
    )
    checkpoint_lease = RunLeaseStore(database).acquire(
        prepared.run.run_id,
        owner_id="test:campaign-recovery:checkpoint",
        ttl=timedelta(seconds=30),
    )
    checkpoint = CheckpointRepository(database).save(
        prepared.run.run_id,
        1,
        {"phase": "mutation"},
        authority=checkpoint_lease.authority,
    )
    RunLeaseStore(database).release(checkpoint_lease.authority)
    approval = ApprovalRequest(
        run_id=prepared.run.run_id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_name=("run_tests" if uncertain_fact == "process_started" else "edit_file"),
        sanitized_arguments={"path": "workspace/target.py"},
        request_digest=SHA,
    )
    if uncertain_fact == "approval_claimed":
        approval = approval.model_copy(
            update={
                "status": ApprovalStatus.APPROVED,
                "consumption_state": ApprovalConsumptionState.CLAIMED,
                "decided_at": utc_now(),
            }
        )
    approval = ApprovalRepository(database).create(approval)
    if uncertain_fact == "mutation_writing":
        MutationExecutionRepository(database).create(
            MutationExecutionRecord(
                run_id=prepared.run.run_id,
                approval_id=approval.approval_id,
                tool_call_digest=SHA,
                tool_name="edit_file",
                target_path="workspace/target.py",
                before_sha256="a" * 64,
                expected_after_sha256="b" * 64,
                before_workspace_digest="c" * 64,
                expected_after_workspace_digest="d" * 64,
                status=MutationExecutionStatus.WRITING,
            )
        )
    elif uncertain_fact == "process_started":
        ProcessExecutionRepository(database).create(
            ProcessExecutionRecord(
                run_id=prepared.run.run_id,
                approval_id=approval.approval_id,
                tool_call_digest=SHA,
                attempt_number=1,
                profile_id="visible_tests",
                profile_version=1,
                profile_digest="1" * 64,
                executable_path=PYTHON_EXECUTABLE,
                argv_digest="2" * 64,
                cwd=str(lease.model_workspace),
                environment_digest="3" * 64,
                root_pid=123,
                job_id="recovery-test-job",
                status=ProcessExecutionStatus.STARTED,
            )
        )

    result = await pilot.recover_campaign(campaign.campaign_id)

    attempts = repository.list_attempts(running.slot_id)
    slots = repository.list_slots(campaign.campaign_id)
    assert result.campaign.status is EvaluationCampaignStatus.INDETERMINATE
    assert attempts == [
        repository.get_attempt(running.attempt_id),
    ]
    assert attempts[0].status is EvaluationAttemptStatus.INDETERMINATE
    assert attempts[0].failure_category == "PILOT_SIDE_EFFECT_INDETERMINATE"
    assert slots[0].status is EvaluationSlotStatus.INDETERMINATE
    assert all(slot.status is EvaluationSlotStatus.PENDING for slot in slots[1:])
    assert factory.prepare_count == 1
    assert lease.model_workspace.exists()
    database.close()
