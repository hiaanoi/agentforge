from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agentforge.domain.models import Run
from agentforge.domain.repair import (
    BudgetProfile,
    CompletionCorrectionMode,
    RepairCompletionStatus,
)
from agentforge.evaluation.models import RepairEvaluationRun
from agentforge.evaluation.persistence import EvaluationRunRepository
from agentforge.evaluation.telemetry_models import EvaluationRunTelemetry
from agentforge.evaluation.telemetry_persistence import (
    EvaluationTelemetryRepository,
    TelemetryConflictError,
)
from agentforge.persistence.database import Database
from agentforge.persistence.repositories import RunRepository

SHA = "c" * 64


def _evaluation_run(run_id: UUID) -> RepairEvaluationRun:
    return RepairEvaluationRun(
        protocol_digest=SHA,
        campaign_id=uuid4(),
        slot_id=uuid4(),
        attempt_id=uuid4(),
        attempt_number=1,
        task_id="telemetry-persistence",
        repetition_index=0,
        model_id="gpt-test",
        model_parameters_digest=SHA,
        system_prompt_digest=SHA,
        task_prompt_digest=SHA,
        tool_schema_digest=SHA,
        context_policy_version=1,
        initial_workspace_digest=SHA,
        task_policy_digest=SHA,
        budget_profile=BudgetProfile.BASIC,
        completion_correction_mode=CompletionCorrectionMode.DEFAULT,
        run_id=run_id,
        final_status=RepairCompletionStatus.BUDGET_EXHAUSTED,
        verified_success=False,
        model_calls=2,
        read_calls=1,
        edit_attempts=0,
        test_runs=0,
        completion_corrections=0,
        policy_violations=0,
        wall_time_ms=100,
        failure_category="MODEL_CALL_LIMIT",
    )


def _telemetry(record: RepairEvaluationRun) -> EvaluationRunTelemetry:
    return EvaluationRunTelemetry(
        evaluation_run_id=record.evaluation_run_id,
        run_id=record.run_id,
        campaign_id=record.campaign_id,
        attempt_id=record.attempt_id,
        protocol_digest=record.protocol_digest,
        model_id=record.model_id,
        logical_model_calls=2,
        physical_model_requests=2,
        completed_model_requests=2,
        failed_model_requests=0,
        retry_count=0,
        usage_complete=True,
        input_tokens=20,
        output_tokens=8,
        total_tokens=28,
        cached_input_tokens=2,
        reasoning_tokens=1,
        successful_provider_duration_ms=50,
        maximum_provider_duration_ms=30,
        model_attempt_elapsed_ms=50,
        provider_deviation_count=0,
        normalized_multi_tool_response_count=0,
        returned_function_call_count=0,
        discarded_function_call_count=0,
        model_protocol_failure_count=0,
        tool_requested_count=1,
        tool_completed_count=1,
        tool_failed_count=0,
        read_call_count=1,
        mutation_requested_count=0,
        mutation_committed_count=0,
        mutation_failed_count=0,
        managed_test_requested_count=0,
        managed_test_completed_count=0,
        managed_test_failed_count=0,
        managed_test_timeout_count=0,
        approval_requested_count=0,
        approval_granted_count=0,
        approval_rejected_count=0,
        context_compaction_count=0,
        completion_correction_count=0,
        policy_violation_count=0,
    )


def _setup(
    path: Path,
) -> tuple[Database, RepairEvaluationRun, EvaluationRunTelemetry]:
    database = Database.from_path(path)
    database.create_schema()
    run = RunRepository(database).create(Run(task="telemetry persistence"))
    record = _evaluation_run(run.run_id)
    EvaluationRunRepository(database).save(record)
    return database, record, _telemetry(record)


def _changed(
    telemetry: EvaluationRunTelemetry,
    **updates: object,
) -> EvaluationRunTelemetry:
    data = telemetry.model_dump(mode="json")
    data.update(updates)
    data["telemetry_digest"] = ""
    return EvaluationRunTelemetry.model_validate(data)


def test_repository_round_trips_lists_and_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.sqlite3"
    database, record, telemetry = _setup(path)
    repository = EvaluationTelemetryRepository(database)

    assert repository.find(record.evaluation_run_id) is None
    assert repository.save(telemetry) == telemetry
    assert repository.get(record.evaluation_run_id) == telemetry
    assert repository.find(record.evaluation_run_id) == telemetry
    assert repository.list_for_campaign(record.campaign_id) == [telemetry]
    database.close()

    reopened = Database.from_path(path)
    reopened.create_schema()
    assert EvaluationTelemetryRepository(reopened).get(
        record.evaluation_run_id
    ) == telemetry
    reopened.close()


def test_exact_duplicate_save_is_idempotent_and_conflict_is_rejected(
    tmp_path: Path,
) -> None:
    database, _, telemetry = _setup(tmp_path / "telemetry.sqlite3")
    repository = EvaluationTelemetryRepository(database)

    assert repository.save(telemetry) == telemetry
    assert repository.save(telemetry) == telemetry
    with pytest.raises(TelemetryConflictError, match="identity conflict"):
        repository.save(_changed(telemetry, input_tokens=21))
    database.close()


@pytest.mark.parametrize(
    "updates",
    [
        {"run_id": str(uuid4())},
        {"campaign_id": str(uuid4())},
        {"attempt_id": str(uuid4())},
        {"protocol_digest": "d" * 64},
        {"model_id": "other-model"},
    ],
)
def test_repository_rejects_result_binding_drift(
    tmp_path: Path,
    updates: dict[str, object],
) -> None:
    database, _, telemetry = _setup(tmp_path / "telemetry.sqlite3")
    repository = EvaluationTelemetryRepository(database)

    with pytest.raises(TelemetryConflictError, match="binding"):
        repository.save(_changed(telemetry, **updates))
    database.close()


def test_missing_evaluation_result_cannot_receive_telemetry(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "telemetry.sqlite3")
    database.create_schema()
    run = RunRepository(database).create(Run(task="orphan telemetry"))
    telemetry = _telemetry(_evaluation_run(run.run_id))

    with pytest.raises(TelemetryConflictError, match="result is missing"):
        EvaluationTelemetryRepository(database).save(telemetry)
    database.close()
