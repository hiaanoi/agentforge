import hashlib
import shutil
import sys
from pathlib import Path

import pytest
from test_pilot_runtime_factory import ALLOWED_ENV, TASK_ROOT, frozen_protocol

from agentforge.application.contracts import (
    RuntimeTrustClass,
    VerificationCapsuleState,
)
from agentforge.domain.enums import (
    EvaluationAttemptStatus,
    EvaluationCampaignStatus,
    EvaluationSlotStatus,
)
from agentforge.domain.repair import RepairCompletionStatus
from agentforge.evaluation.baseline_models import BaselineExecutionStatus
from agentforge.evaluation.baseline_persistence import BaselineExecutionRepository
from agentforge.evaluation.campaign_persistence import EvaluationCampaignRepository
from agentforge.evaluation.persistence import EvaluationRunRepository
from agentforge.evaluation.pilot_factory import PilotRuntimeFactory
from agentforge.evaluation.pilot_runner import PilotRunner
from agentforge.evaluation.pilot_workspace import PilotWorkspaceManager
from agentforge.evaluation.protocol import EvaluationProtocol
from agentforge.evaluation.provider_factory import ProtocolBoundModelProvider
from agentforge.models.base import ModelRequest
from agentforge.models.domain import ModelErrorCode, ModelResponse
from agentforge.models.errors import ModelRequestError
from agentforge.models.mock import MockModelProvider
from agentforge.persistence.database import Database
from agentforge.persistence.repositories import EventRepository
from agentforge.persistence.test_executions import ProcessExecutionRepository


class RecordingMockEvaluationProviderFactory:
    def __init__(self, responses: list[object]) -> None:
        self._responses = responses
        self.delegates: list[MockModelProvider] = []

    def create(
        self,
        protocol: EvaluationProtocol,
    ) -> ProtocolBoundModelProvider:
        delegate = MockModelProvider(
            self._responses,
            model_id=protocol.provider_binding.model_id,
        )
        self.delegates.append(delegate)
        return ProtocolBoundModelProvider(protocol, delegate)


class TimeoutMockModelProvider:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    @property
    def name(self) -> str:
        return "mock"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request.model_copy(deep=True))
        raise ModelRequestError(
            ModelErrorCode.MODEL_TIMEOUT,
            "formal pilot timeout",
            retryable=False,
        )


class TimeoutMockEvaluationProviderFactory:
    def __init__(self) -> None:
        self.delegates: list[TimeoutMockModelProvider] = []

    def create(
        self,
        protocol: EvaluationProtocol,
    ) -> ProtocolBoundModelProvider:
        delegate = TimeoutMockModelProvider()
        self.delegates.append(delegate)
        return ProtocolBoundModelProvider(protocol, delegate)


def repair_responses() -> list[object]:
    buggy_path = TASK_ROOT / "workspace" / "parcel_flow" / "store.py"
    fixed_path = TASK_ROOT / "reference" / "fixed_files" / "parcel_flow" / "store.py"
    buggy = buggy_path.read_text(encoding="utf-8")
    fixed = fixed_path.read_text(encoding="utf-8")
    start_marker = "                    command_id TEXT NOT NULL,"
    fixed_start_marker = "                    command_id TEXT NOT NULL UNIQUE,"
    end_marker = "    def dispatches_for"
    old_text = buggy[buggy.index(start_marker) : buggy.index(end_marker)]
    new_text = fixed[
        fixed.index(fixed_start_marker) : fixed.index(end_marker)
    ]
    return [
        {
            "type": "tool_call",
            "tool": "read_file",
            "arguments": {"path": "workspace/parcel_flow/store.py"},
        },
        {
            "type": "tool_call",
            "tool": "edit_file",
            "arguments": {
                "path": "workspace/parcel_flow/store.py",
                "old_text": old_text,
                "new_text": new_text,
                "expected_sha256": hashlib.sha256(
                    buggy_path.read_bytes()
                ).hexdigest(),
            },
        },
        {
            "type": "tool_call",
            "tool": "run_tests",
            "arguments": {
                "profile_id": "visible-self-durable-double-consumption"
            },
        },
        {
            "type": "final",
            "answer": "The durable dispatch recovery behavior is repaired.",
        },
    ]


@pytest.mark.asyncio
async def test_formal_mock_pilot_runs_complete_durable_evaluator_pipeline(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "formal-pilot.sqlite3")
    database.create_schema()
    providers = RecordingMockEvaluationProviderFactory(repair_responses())
    runtime_factory = PilotRuntimeFactory(
        database,
        providers,
        executable=Path(sys.executable),
        allowed_env=ALLOWED_ENV,
    )
    protocol = frozen_protocol(runtime_factory)
    runner = PilotRunner(
        database,
        runtime_factory,
        PilotWorkspaceManager(tmp_path / "pilot-workspaces"),
        {protocol.task_id: TASK_ROOT},
    )

    result = await runner.run_campaign(
        runner.create_campaign(protocol).campaign_id
    )

    assert result.campaign.status is EvaluationCampaignStatus.COMPLETED
    assert result.selection is not None
    assert result.report_json is not None
    assert len(result.selection.selected_runs) == protocol.repetition_count == 3
    assert result.selection.summary.verified_success_count == 3
    assert result.selection.summary.stable_success is True
    assert all(
        run.final_status is RepairCompletionStatus.VERIFIED_SUCCESS
        and run.model_calls == 4
        and run.read_calls == 1
        and run.edit_attempts == 1
        and run.test_runs == 2
        and run.baseline_execution_id is not None
        and run.development_test_execution_id is not None
        and run.final_verification_execution_id is not None
        for run in result.selection.selected_runs
    )

    campaigns = EvaluationCampaignRepository(database)
    slots = campaigns.list_slots(result.campaign.campaign_id)
    assert all(slot.status is EvaluationSlotStatus.ACCEPTED for slot in slots)
    assert [slot.repetition_index for slot in slots] == [0, 1, 2]
    persisted_runs = EvaluationRunRepository(database).list_for_campaign(
        result.campaign.campaign_id
    )
    assert persisted_runs == list(result.selection.selected_runs)

    runtime_event_text: list[str] = []
    for selected, slot in zip(result.selection.selected_runs, slots, strict=True):
        attempts = campaigns.list_attempts(slot.slot_id)
        assert len(attempts) == 1
        attempt = attempts[0]
        assert attempt.status is EvaluationAttemptStatus.COMPLETED
        assert attempt.evaluation_run_id == selected.evaluation_run_id
        assert attempt.run_id == selected.run_id
        assert attempt.baseline_execution_id == selected.baseline_execution_id
        baseline = BaselineExecutionRepository(database).get_for_run(
            selected.run_id
        )
        assert baseline.status is BaselineExecutionStatus.VERIFIED_EXPECTED_FAILURE
        assert baseline.baseline_execution_id == selected.baseline_execution_id
        process_records = ProcessExecutionRepository(database).list_for_run(
            selected.run_id
        )
        assert [record.profile_id for record in process_records] == [
            "visible-self-durable-double-consumption",
            "hidden-self-durable-double-consumption",
        ]
        hidden_execution = process_records[1]
        assert hidden_execution.capsule_state is VerificationCapsuleState.SEALED
        assert hidden_execution.runtime_trust_class is RuntimeTrustClass.NON_HERMETIC
        assert hidden_execution.source_revision_number == 1
        assert hidden_execution.source_revision_digest != "0" * 64
        assert (
            hidden_execution.source_snapshot_digest
            == hidden_execution.source_revision_digest
        )
        assert hidden_execution.verifier_artifact_digest is not None
        runtime_event_text.extend(
            event.model_dump_json()
            for event in EventRepository(database).list_for_run(selected.run_id)
        )

    assert len(providers.delegates) == 3
    assert all(len(delegate.requests) == 4 for delegate in providers.delegates)
    model_request_text = " ".join(
        request.model_dump_json()
        for delegate in providers.delegates
        for request in delegate.requests
    )
    event_text = " ".join(runtime_event_text)
    report_text = result.report_json
    campaign_event_text = " ".join(
        event.model_dump_json()
        for event in campaigns.list_events(result.campaign.campaign_id)
    )
    forbidden = (
        "reference/fixed_files",
        "test_recovery_invariants.py",
        "test_completed_result_is_reused_on_repeated_processing",
    )
    assert all(value not in model_request_text for value in forbidden)
    assert protocol.task_prompt not in event_text
    assert protocol.task_prompt not in campaign_event_text
    assert protocol.task_prompt not in report_text
    assert str(tmp_path) not in event_text
    assert str(tmp_path) not in campaign_event_text
    assert str(tmp_path) not in report_text
    database.close()


@pytest.mark.asyncio
async def test_unexpected_baseline_pass_stops_before_first_model_request(
    tmp_path: Path,
) -> None:
    fixture_root = tmp_path / "fixtures"
    shutil.copytree(TASK_ROOT.parent.parent, fixture_root)
    task_root = fixture_root / "tasks" / TASK_ROOT.name
    shutil.copyfile(
        task_root / "reference" / "fixed_files" / "parcel_flow" / "store.py",
        task_root / "workspace" / "parcel_flow" / "store.py",
    )
    database = Database.from_path(tmp_path / "unexpected-pass.sqlite3")
    database.create_schema()
    providers = RecordingMockEvaluationProviderFactory([])
    runtime_factory = PilotRuntimeFactory(
        database,
        providers,
        executable=Path(sys.executable),
        allowed_env=ALLOWED_ENV,
    )
    protocol = frozen_protocol(
        runtime_factory,
        task_root=task_root,
    ).model_copy(update={"repetition_count": 1, "protocol_digest": ""})
    protocol = EvaluationProtocol.model_validate(
        protocol.model_dump(mode="json")
    )
    runner = PilotRunner(
        database,
        runtime_factory,
        PilotWorkspaceManager(tmp_path / "pilot-workspaces"),
        {protocol.task_id: task_root},
    )

    result = await runner.run_campaign(
        runner.create_campaign(protocol).campaign_id
    )

    assert result.campaign.status is EvaluationCampaignStatus.COMPLETED_WITH_INVALID
    assert result.selection is None
    assert len(providers.delegates) == 1
    assert providers.delegates[0].requests == []
    slot = EvaluationCampaignRepository(database).list_slots(
        result.campaign.campaign_id
    )[0]
    attempt = EvaluationCampaignRepository(database).list_attempts(
        slot.slot_id
    )[0]
    assert attempt.status is EvaluationAttemptStatus.INVALID
    assert attempt.run_id is not None
    baseline = BaselineExecutionRepository(database).get_for_run(
        attempt.run_id
    )
    assert baseline.status is BaselineExecutionStatus.BLOCKED
    assert baseline.failure_reason is not None
    assert baseline.failure_reason.value == "UNEXPECTED_PASS"
    database.close()


@pytest.mark.asyncio
async def test_model_timeout_is_persisted_as_invalid_and_replaced_not_selected(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "timeout-pilot.sqlite3")
    database.create_schema()
    providers = TimeoutMockEvaluationProviderFactory()
    runtime_factory = PilotRuntimeFactory(
        database,
        providers,
        executable=Path(sys.executable),
        allowed_env=ALLOWED_ENV,
    )
    draft = frozen_protocol(runtime_factory).model_copy(
        update={"repetition_count": 1, "protocol_digest": ""}
    )
    protocol = EvaluationProtocol.model_validate(draft.model_dump(mode="json"))
    runner = PilotRunner(
        database,
        runtime_factory,
        PilotWorkspaceManager(tmp_path / "pilot-workspaces"),
        {protocol.task_id: TASK_ROOT},
    )

    result = await runner.run_campaign(
        runner.create_campaign(protocol).campaign_id
    )

    assert result.campaign.status is EvaluationCampaignStatus.COMPLETED_WITH_INVALID
    assert result.selection is None
    slots = EvaluationCampaignRepository(database).list_slots(
        result.campaign.campaign_id
    )
    assert len(slots) == 1
    assert slots[0].status is EvaluationSlotStatus.INVALID
    attempts = EvaluationCampaignRepository(database).list_attempts(slots[0].slot_id)
    assert [attempt.status for attempt in attempts] == [
        EvaluationAttemptStatus.INVALID,
        EvaluationAttemptStatus.INVALID,
    ]
    records = EvaluationRunRepository(database).list_for_campaign(
        result.campaign.campaign_id
    )
    assert len(records) == 2
    assert all(
        record.final_status is RepairCompletionStatus.RUNTIME_FAILURE
        and record.infrastructure_failure
        and record.failure_category == ModelErrorCode.MODEL_TIMEOUT.value
        for record in records
    )
    assert len(providers.delegates) == 2
    assert all(len(delegate.requests) == 1 for delegate in providers.delegates)
    database.close()
