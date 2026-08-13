from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_pilot_runtime_factory import (
    ALLOWED_ENV,
    TASK_ROOT,
    frozen_protocol,
)

from agentforge.domain.models import Run
from agentforge.domain.repair import RepairCompletionStatus
from agentforge.evaluation.campaign_persistence import EvaluationCampaignRepository
from agentforge.evaluation.formal_fixtures import FormalFixtureLoader
from agentforge.evaluation.harness import EvaluationRunMetadata
from agentforge.evaluation.models import RepairEvaluationRun
from agentforge.evaluation.persistence import EvaluationRunRepository
from agentforge.evaluation.pilot_factory import PilotRuntimeFactory
from agentforge.evaluation.pilot_runner import PilotRunner
from agentforge.evaluation.pilot_workspace import PilotWorkspaceManager
from agentforge.evaluation.protocol import EvaluationProtocol, ReplacementPolicy
from agentforge.evaluation.provider_factory import MockEvaluationProviderFactory
from agentforge.persistence.database import Database
from agentforge.persistence.repositories import RunRepository

SHA = "d" * 64


class ScriptedHarness:
    def __init__(
        self,
        metadata: EvaluationRunMetadata,
        outcome: tuple[RepairCompletionStatus, bool, str | None],
        baseline_execution_id,
    ) -> None:
        self._metadata = metadata
        self._outcome = outcome
        self._baseline_execution_id = baseline_execution_id

    async def execute(
        self,
        run_id,
        metadata: EvaluationRunMetadata,
    ) -> RepairEvaluationRun:
        assert metadata == self._metadata
        status, infrastructure_failure, failure_category = self._outcome
        success = status is RepairCompletionStatus.VERIFIED_SUCCESS
        return RepairEvaluationRun(
            **metadata.model_dump(),
            run_id=run_id,
            baseline_execution_id=self._baseline_execution_id,
            final_status=status,
            verified_success=success,
            model_calls=2,
            read_calls=1,
            edit_attempts=1,
            test_runs=1,
            completion_corrections=0,
            policy_violations=0,
            wall_time_ms=10,
            final_workspace_digest=SHA if success else None,
            final_diff_digest=SHA if success else None,
            final_verification_execution_id=uuid4() if success else None,
            failure_category=failure_category,
            infrastructure_failure=infrastructure_failure,
        )


class PersistThenRaiseHarness:
    def __init__(
        self,
        database: Database,
        delegate: ScriptedHarness,
    ) -> None:
        self._runs = EvaluationRunRepository(database)
        self._delegate = delegate

    async def execute(
        self,
        run_id,
        metadata: EvaluationRunMetadata,
    ) -> RepairEvaluationRun:
        result = await self._delegate.execute(run_id, metadata)
        self._runs.save(result)
        raise RuntimeError("simulated audit append failure after result save")


class ScriptedFactory:
    def __init__(
        self,
        database: Database,
        outcomes: list[tuple[RepairCompletionStatus, bool, str | None]],
        *,
        persist_then_raise_once: bool = False,
    ) -> None:
        self._database = database
        self._outcomes = list(outcomes)
        self.prepare_count = 0
        self.workspace_paths: list[Path] = []
        self._persist_then_raise_once = persist_then_raise_once

    def prepare(self, protocol, manifest, attempt, lease):  # type: ignore[no-untyped-def]
        self.prepare_count += 1
        self.workspace_paths.append(lease.model_workspace)
        if not self._outcomes:
            raise AssertionError("No scripted Pilot outcome remains")
        run = RunRepository(self._database).create(
            Run(task=protocol.task_prompt, model_provider="mock")
        )
        metadata = EvaluationRunMetadata(
            protocol_digest=protocol.protocol_digest,
            campaign_id=attempt.campaign_id,
            slot_id=attempt.slot_id,
            attempt_id=attempt.attempt_id,
            attempt_number=attempt.attempt_number,
            replacement_for_evaluation_run_id=(
                attempt.predecessor_evaluation_run_id
            ),
            task_id=manifest.task_id,
            repetition_index=attempt.repetition_index,
            model_id=protocol.provider_binding.model_id,
            model_parameters_digest=protocol.provider_binding.configuration_digest,
            system_prompt_digest=protocol.system_prompt_digest,
            task_prompt_digest=protocol.task_prompt_digest,
            tool_schema_digest=protocol.tool_schema_digest,
            context_policy_version=int(protocol.context_policy.version),
            initial_workspace_digest=lease.initial_workspace_digest,
            task_policy_digest=protocol.task_policy_digest,
            budget_profile=manifest.to_policy(
                path_case_sensitive=False
            ).budget_profile,
            completion_correction_mode=protocol.completion_correction_mode,
        )
        baseline_execution_id = uuid4()
        harness = ScriptedHarness(
            metadata,
            self._outcomes.pop(0),
            baseline_execution_id,
        )
        if self._persist_then_raise_once:
            self._persist_then_raise_once = False
            harness = PersistThenRaiseHarness(self._database, harness)  # type: ignore[assignment]
        return SimpleNamespace(
            run=run,
            metadata=metadata,
            baseline_execution=SimpleNamespace(
                baseline_execution_id=baseline_execution_id,
            ),
            harness=harness,
        )


def protocol_for(
    tmp_path: Path,
    *,
    replacements: int = 1,
) -> EvaluationProtocol:
    database = Database.from_path(tmp_path / "protocol-facts.sqlite3")
    database.create_schema()
    inspector = PilotRuntimeFactory(
        database,
        MockEvaluationProviderFactory([]),
        executable=Path(__import__("sys").executable),
        allowed_env=ALLOWED_ENV,
    )
    value = frozen_protocol(inspector)
    database.close()
    return value.model_copy(
        update={
            "replacement_policy": ReplacementPolicy(
                max_replacements_per_slot=replacements,
                replaceable_failure_categories=(
                    "MODEL_TIMEOUT",
                    "PILOT_PREPARATION_ERROR",
                    "WORKSPACE_PREPARATION_ERROR",
                ),
            ),
            "protocol_digest": "",
        }
    )


def runner(
    tmp_path: Path,
    outcomes: list[tuple[RepairCompletionStatus, bool, str | None]],
    *,
    replacements: int = 1,
):
    database = Database.from_path(tmp_path / "campaign.sqlite3")
    database.create_schema()
    factory = ScriptedFactory(database, outcomes)
    pilot = PilotRunner(
        database,
        factory,
        PilotWorkspaceManager(tmp_path / "pilot-workspaces"),
        {FormalFixtureLoader().load(TASK_ROOT).task_id: TASK_ROOT},
    )
    protocol = protocol_for(tmp_path, replacements=replacements)
    return database, factory, pilot, protocol


@pytest.mark.asyncio
async def test_campaign_runs_fixed_slots_sequentially_and_is_idempotent(
    tmp_path: Path,
) -> None:
    success = (RepairCompletionStatus.VERIFIED_SUCCESS, False, None)
    database, factory, pilot, protocol = runner(
        tmp_path,
        [success, success, success],
    )
    campaign = pilot.create_campaign(protocol)

    first = await pilot.run_campaign(campaign.campaign_id)
    repeated = await pilot.run_campaign(campaign.campaign_id)

    assert repeated == first
    assert first.campaign.status.value == "COMPLETED"
    assert first.selection is not None
    assert first.selection.summary.verified_success_count == 3
    assert factory.prepare_count == 3
    assert len(set(factory.workspace_paths)) == 3
    assert all(not path.exists() for path in factory.workspace_paths)
    assert [
        slot.status.value
        for slot in EvaluationCampaignRepository(database).list_slots(
            campaign.campaign_id
        )
    ] == ["ACCEPTED", "ACCEPTED", "ACCEPTED"]
    database.close()


@pytest.mark.asyncio
async def test_normal_model_failure_is_accepted_without_replacement(
    tmp_path: Path,
) -> None:
    normal_failure = (
        RepairCompletionStatus.BUDGET_EXHAUSTED,
        False,
        "BUDGET_EXHAUSTED",
    )
    success = (RepairCompletionStatus.VERIFIED_SUCCESS, False, None)
    database, factory, pilot, protocol = runner(
        tmp_path,
        [normal_failure, success, success],
    )

    result = await pilot.run_campaign(
        pilot.create_campaign(protocol).campaign_id
    )

    assert result.selection is not None
    assert result.selection.summary.verified_success_count == 2
    assert result.selection.replacement_count == 0
    assert factory.prepare_count == 3
    database.close()


@pytest.mark.asyncio
async def test_allowlisted_infrastructure_failure_gets_one_fresh_replacement(
    tmp_path: Path,
) -> None:
    infrastructure_failure = (
        RepairCompletionStatus.RUNTIME_FAILURE,
        True,
        "MODEL_TIMEOUT",
    )
    success = (RepairCompletionStatus.VERIFIED_SUCCESS, False, None)
    database, factory, pilot, protocol = runner(
        tmp_path,
        [infrastructure_failure, success, success, success],
    )
    campaign = pilot.create_campaign(protocol)

    result = await pilot.run_campaign(campaign.campaign_id)

    assert result.selection is not None
    assert result.selection.infrastructure_invalid_count == 1
    assert result.selection.replacement_count == 1
    first_slot = EvaluationCampaignRepository(database).list_slots(
        campaign.campaign_id
    )[0]
    attempts = EvaluationCampaignRepository(database).list_attempts(
        first_slot.slot_id
    )
    assert [attempt.attempt_number for attempt in attempts] == [1, 2]
    assert attempts[0].status.value == "INVALID"
    assert attempts[1].status.value == "COMPLETED"
    assert attempts[1].predecessor_attempt_id == attempts[0].attempt_id
    assert len(set(factory.workspace_paths)) == 4
    database.close()


@pytest.mark.asyncio
async def test_exhausted_replacement_allowance_closes_slot_invalid(
    tmp_path: Path,
) -> None:
    infrastructure_failure = (
        RepairCompletionStatus.RUNTIME_FAILURE,
        True,
        "MODEL_TIMEOUT",
    )
    success = (RepairCompletionStatus.VERIFIED_SUCCESS, False, None)
    database, factory, pilot, protocol = runner(
        tmp_path,
        [infrastructure_failure, infrastructure_failure, success, success],
    )
    campaign = pilot.create_campaign(protocol)

    result = await pilot.run_campaign(campaign.campaign_id)

    assert result.campaign.status.value == "COMPLETED_WITH_INVALID"
    assert result.selection is not None
    assert len(result.selection.selected_runs) == 2
    slots = EvaluationCampaignRepository(database).list_slots(
        campaign.campaign_id
    )
    assert slots[0].status.value == "INVALID"
    assert [slot.status.value for slot in slots[1:]] == ["ACCEPTED", "ACCEPTED"]
    assert factory.prepare_count == 4
    database.close()


@pytest.mark.asyncio
async def test_durable_result_wins_when_post_save_audit_append_fails(
    tmp_path: Path,
) -> None:
    success = (RepairCompletionStatus.VERIFIED_SUCCESS, False, None)
    database = Database.from_path(tmp_path / "campaign.sqlite3")
    database.create_schema()
    factory = ScriptedFactory(
        database,
        [success, success, success],
        persist_then_raise_once=True,
    )
    protocol = protocol_for(tmp_path)
    pilot = PilotRunner(
        database,
        factory,
        PilotWorkspaceManager(tmp_path / "pilot-workspaces"),
        {FormalFixtureLoader().load(TASK_ROOT).task_id: TASK_ROOT},
    )

    result = await pilot.run_campaign(
        pilot.create_campaign(protocol).campaign_id
    )

    assert result.campaign.status.value == "COMPLETED"
    assert result.selection is not None
    assert len(result.selection.selected_runs) == 3
    assert factory.prepare_count == 3
    database.close()
