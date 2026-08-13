from pathlib import Path

import pytest
from test_pilot_campaign_runner import TASK_ROOT, protocol_for, runner

from agentforge.domain.enums import (
    EvaluationCampaignStatus,
    EvaluationSlotStatus,
)
from agentforge.domain.repair import RepairCompletionStatus
from agentforge.evaluation.campaign_persistence import (
    EvaluationCampaignRepository,
)
from agentforge.evaluation.formal_fixtures import FormalFixtureLoader
from agentforge.evaluation.pilot_factory import PilotBindingMismatchError
from agentforge.evaluation.pilot_runner import PilotRunner
from agentforge.evaluation.pilot_workspace import PilotWorkspaceManager
from agentforge.persistence.database import Database


class BindingFailureFactory:
    def __init__(self) -> None:
        self.prepare_count = 0

    def prepare(self, *args: object) -> None:
        del args
        self.prepare_count += 1
        raise PilotBindingMismatchError("frozen Provider binding drift")


@pytest.mark.asyncio
async def test_exhausted_model_protocol_error_is_scored_without_replacement(
    tmp_path: Path,
) -> None:
    protocol_error = (
        RepairCompletionStatus.MODEL_PROTOCOL_ERROR,
        False,
        "MODEL_PROTOCOL_ERROR",
    )
    success = (RepairCompletionStatus.VERIFIED_SUCCESS, False, None)
    database, factory, pilot, protocol = runner(
        tmp_path,
        [protocol_error, success, success],
    )

    result = await pilot.run_campaign(
        pilot.create_campaign(protocol).campaign_id
    )

    assert result.campaign.status is EvaluationCampaignStatus.COMPLETED
    assert result.selection is not None
    assert result.selection.summary.repetitions == 3
    assert result.selection.summary.verified_success_count == 2
    assert result.selection.replacement_count == 0
    assert result.selection.selected_runs[0].failure_class.value == "MODEL_QUALITY"
    assert factory.prepare_count == 3
    database.close()


@pytest.mark.asyncio
async def test_configuration_error_blocks_campaign_before_remaining_slots(
    tmp_path: Path,
) -> None:
    auth_error = (
        RepairCompletionStatus.RUNTIME_FAILURE,
        True,
        "MODEL_AUTH_ERROR",
    )
    database, factory, pilot, protocol = runner(tmp_path, [auth_error])
    campaign = pilot.create_campaign(protocol)

    result = await pilot.run_campaign(campaign.campaign_id)

    assert result.campaign.status is EvaluationCampaignStatus.BLOCKED
    assert result.selection is None
    assert factory.prepare_count == 1
    slots = EvaluationCampaignRepository(database).list_slots(
        campaign.campaign_id
    )
    assert [slot.status for slot in slots] == [
        EvaluationSlotStatus.INVALID,
        EvaluationSlotStatus.PENDING,
        EvaluationSlotStatus.PENDING,
    ]
    database.close()


@pytest.mark.asyncio
async def test_preparation_binding_error_is_not_replaced_and_blocks_campaign(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "campaign.sqlite3")
    database.create_schema()
    factory = BindingFailureFactory()
    pilot = PilotRunner(
        database,
        factory,
        PilotWorkspaceManager(tmp_path / "pilot-workspaces"),
        {FormalFixtureLoader().load(TASK_ROOT).task_id: TASK_ROOT},
    )
    campaign = pilot.create_campaign(protocol_for(tmp_path))

    result = await pilot.run_campaign(campaign.campaign_id)

    assert result.campaign.status is EvaluationCampaignStatus.BLOCKED
    assert result.selection is None
    assert factory.prepare_count == 1
    slots = EvaluationCampaignRepository(database).list_slots(
        campaign.campaign_id
    )
    assert [slot.status for slot in slots] == [
        EvaluationSlotStatus.INVALID,
        EvaluationSlotStatus.PENDING,
        EvaluationSlotStatus.PENDING,
    ]
    attempts = EvaluationCampaignRepository(database).list_attempts(
        slots[0].slot_id
    )
    assert len(attempts) == 1
    assert attempts[0].failure_category == "PILOT_CONFIGURATION_ERROR"
    database.close()


@pytest.mark.asyncio
async def test_indeterminate_result_stops_campaign_and_retains_workspace(
    tmp_path: Path,
) -> None:
    indeterminate = (
        RepairCompletionStatus.INDETERMINATE,
        False,
        "INDETERMINATE_SIDE_EFFECT",
    )
    database, factory, pilot, protocol = runner(tmp_path, [indeterminate])
    campaign = pilot.create_campaign(protocol)

    result = await pilot.run_campaign(campaign.campaign_id)

    assert result.campaign.status is EvaluationCampaignStatus.INDETERMINATE
    assert result.selection is None
    assert factory.prepare_count == 1
    assert len(factory.workspace_paths) == 1
    assert factory.workspace_paths[0].exists()
    slots = EvaluationCampaignRepository(database).list_slots(
        campaign.campaign_id
    )
    assert [slot.status for slot in slots] == [
        EvaluationSlotStatus.INDETERMINATE,
        EvaluationSlotStatus.PENDING,
        EvaluationSlotStatus.PENDING,
    ]
    database.close()


@pytest.mark.asyncio
async def test_infrastructure_gap_returns_available_scored_selection(
    tmp_path: Path,
) -> None:
    timeout = (
        RepairCompletionStatus.RUNTIME_FAILURE,
        True,
        "MODEL_TIMEOUT",
    )
    success = (RepairCompletionStatus.VERIFIED_SUCCESS, False, None)
    database, factory, pilot, protocol = runner(
        tmp_path,
        [timeout, timeout, success, success],
    )

    result = await pilot.run_campaign(
        pilot.create_campaign(protocol).campaign_id
    )

    assert (
        result.campaign.status
        is EvaluationCampaignStatus.COMPLETED_WITH_INVALID
    )
    assert result.selection is not None
    assert len(result.selection.selected_runs) == 2
    assert result.selection.summary.repetitions == 2
    assert result.selection.infrastructure_invalid_count == 2
    assert result.selection.replacement_count == 1
    assert factory.prepare_count == 4
    database.close()
