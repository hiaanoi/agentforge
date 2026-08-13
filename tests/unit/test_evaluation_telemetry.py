from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from agentforge.domain.enums import EventType
from agentforge.domain.models import Event, Run
from agentforge.domain.repair import (
    BudgetProfile,
    CompletionCorrectionMode,
    RepairCompletionStatus,
)
from agentforge.evaluation.models import RepairEvaluationRun
from agentforge.evaluation.telemetry import EvaluationTelemetryCollector
from agentforge.models.base import ModelRequest
from agentforge.models.domain import (
    ModelAttemptRecord,
    ModelBudget,
    ModelErrorCode,
    ModelUsage,
)
from agentforge.models.errors import ModelRequestError
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import EventLog
from agentforge.persistence.model_workflow import ModelWorkflow
from agentforge.persistence.repositories import EventRepository, RunRepository
from agentforge.persistence.run_leases import RunLeaseStore

SHA = "b" * 64


def _record(run_id: UUID) -> RepairEvaluationRun:
    return RepairEvaluationRun(
        protocol_digest=SHA,
        campaign_id=uuid4(),
        slot_id=uuid4(),
        attempt_id=uuid4(),
        attempt_number=1,
        task_id="telemetry-task",
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
        final_status=RepairCompletionStatus.TESTS_FAILED,
        verified_success=False,
        model_calls=2,
        read_calls=3,
        edit_attempts=1,
        test_runs=2,
        completion_corrections=1,
        policy_violations=2,
        wall_time_ms=800,
        failure_category="DEVELOPMENT_TEST_FAILED",
    )


def _collector_facts(
    tmp_path: Path,
) -> tuple[
    Database,
    EvaluationTelemetryCollector,
    RepairEvaluationRun,
    EventRepository,
]:
    database = Database.from_path(tmp_path / "telemetry.sqlite3")
    database.create_schema()
    run = RunRepository(database).create(Run(task="telemetry"))
    model_workflow = ModelWorkflow(database)
    model_workflow._evaluator_only_ensure_state(
        run.run_id, ModelBudget(max_model_requests=8)
    )
    authority = RunLeaseStore(database).acquire(
        run.run_id, owner_id="telemetry", ttl=timedelta(seconds=30)
    ).authority
    request = ModelRequest(task=run.task, step_number=1)
    first_call = uuid4()
    failed = model_workflow.prepare_attempt(
        run.run_id,
        first_call,
        1,
        request=request,
        provider_identity="telemetry/model",
        authority=authority,
    )
    assert model_workflow.claim_dispatch(
        run.run_id,
        failed,
        request=request,
        provider_identity="telemetry/model",
        authority=authority,
    )
    model_workflow.fail_attempt(
        run.run_id,
        failed,
        ModelRequestError(
            ModelErrorCode.MODEL_TIMEOUT,
            "sensitive provider diagnostic",
            retryable=True,
        ),
        authority=authority,
    )
    retry = model_workflow.prepare_attempt(
        run.run_id,
        first_call,
        2,
        request=request,
        provider_identity="telemetry/model",
        authority=authority,
    )
    assert model_workflow.claim_dispatch(
        run.run_id,
        retry,
        request=request,
        provider_identity="telemetry/model",
        authority=authority,
    )
    model_workflow.complete_attempt(
        run.run_id,
        retry,
        ModelUsage(
            input_tokens=10,
            output_tokens=4,
            total_tokens=14,
            cached_input_tokens=3,
            reasoning_tokens=2,
        ),
        duration_ms=40,
        authority=authority,
    )
    second = model_workflow.prepare_attempt(
        run.run_id,
        uuid4(),
        1,
        request=request,
        provider_identity="telemetry/model",
        authority=authority,
    )
    assert model_workflow.claim_dispatch(
        run.run_id,
        second,
        request=request,
        provider_identity="telemetry/model",
        authority=authority,
    )
    model_workflow.complete_attempt(
        run.run_id,
        second,
        ModelUsage(
            input_tokens=20,
            output_tokens=6,
            total_tokens=26,
            cached_input_tokens=0,
            reasoning_tokens=1,
        ),
        duration_ms=60,
        authority=authority,
    )
    events = EventRepository(database)
    event_payloads = [
        (
            EventType.MODEL_PROVIDER_DEVIATION,
            {
                "returned_call_count": 3,
                "discarded_call_count": 2,
                "provider_request_id": "must-not-escape",
                "arguments": {"path": "C:/private/workspace/secret.py"},
            },
        ),
        (
            EventType.MULTI_TOOL_RESPONSE_NORMALIZED,
            {
                "returned_call_count": 3,
                "discarded_call_count": 2,
                "provider_request_id": "must-not-escape",
            },
        ),
        (
            EventType.MODEL_FAILED,
            {"error_type": ModelErrorCode.MODEL_PROTOCOL_ERROR.value},
        ),
        (EventType.TOOL_REQUESTED, {"sanitized_arguments": {"path": "private.py"}}),
        (EventType.TOOL_REQUESTED, {"tool_name": "read_file"}),
        (EventType.TOOL_COMPLETED, {"output": "private source"}),
        (EventType.TOOL_FAILED, {"error": "private diagnostic"}),
        (EventType.MUTATION_REQUESTED, {"relative_path": "src/private.py"}),
        (EventType.MUTATION_COMMITTED, {"relative_path": "src/private.py"}),
        (EventType.MUTATION_FAILED, {"relative_path": "src/private.py"}),
        (EventType.TEST_REQUESTED, {"profile_id": "visible"}),
        (EventType.TEST_REQUESTED, {"profile_id": "visible"}),
        (EventType.TEST_COMPLETED, {"stdout": "private test output"}),
        (EventType.TEST_FAILED, {"stderr": "private test output"}),
        (EventType.TEST_TIMEOUT, {"stderr": "private test output"}),
        (EventType.APPROVAL_REQUESTED, {"sanitized_arguments": {"content": "x"}}),
        (EventType.APPROVAL_GRANTED, {"decision_note": "private"}),
        (EventType.APPROVAL_REJECTED, {"decision_note": "private"}),
        (EventType.CONTEXT_COMPACTED, {"removed_pair_count": 2}),
        (EventType.REPAIR_COMPLETION_CORRECTED, {"feedback": "private"}),
        (EventType.REPAIR_POLICY_VIOLATION, {"rule": "private"}),
    ]
    for event_type, payload in event_payloads:
        with database.session() as session:
            EventLog().append(session, authority, event_type, payload)
    record = _record(run.run_id)
    collector = EvaluationTelemetryCollector(model_workflow, events)
    return database, collector, record, events


def test_collector_aggregates_attempt_usage_latency_and_retry_facts(
    tmp_path: Path,
) -> None:
    database, collector, record, _ = _collector_facts(tmp_path)

    telemetry = collector.collect(record)

    assert telemetry.logical_model_calls == 2
    assert telemetry.physical_model_requests == 3
    assert telemetry.completed_model_requests == 2
    assert telemetry.failed_model_requests == 1
    assert telemetry.retry_count == 1
    assert telemetry.usage_complete is False
    assert telemetry.input_tokens == 30
    assert telemetry.output_tokens == 10
    assert telemetry.total_tokens == 40
    assert telemetry.cached_input_tokens == 3
    assert telemetry.reasoning_tokens == 3
    assert telemetry.successful_provider_duration_ms == 100
    assert telemetry.maximum_provider_duration_ms == 60
    assert telemetry.model_attempt_elapsed_ms >= 100
    database.close()


def test_collector_aggregates_safe_event_counts(tmp_path: Path) -> None:
    database, collector, record, _ = _collector_facts(tmp_path)

    telemetry = collector.collect(record)

    assert telemetry.provider_deviation_count == 1
    assert telemetry.normalized_multi_tool_response_count == 1
    assert telemetry.returned_function_call_count == 3
    assert telemetry.discarded_function_call_count == 2
    assert telemetry.model_protocol_failure_count == 1
    assert telemetry.tool_requested_count == 2
    assert telemetry.tool_completed_count == 1
    assert telemetry.tool_failed_count == 1
    assert telemetry.read_call_count == 3
    assert telemetry.mutation_requested_count == 1
    assert telemetry.mutation_committed_count == 1
    assert telemetry.mutation_failed_count == 1
    assert telemetry.managed_test_requested_count == 2
    assert telemetry.managed_test_completed_count == 1
    assert telemetry.managed_test_failed_count == 1
    assert telemetry.managed_test_timeout_count == 1
    assert telemetry.approval_requested_count == 1
    assert telemetry.approval_granted_count == 1
    assert telemetry.approval_rejected_count == 1
    assert telemetry.context_compaction_count == 1
    assert telemetry.completion_correction_count == 1
    assert telemetry.policy_violation_count == 2
    database.close()


def test_collector_is_deterministic_and_does_not_propagate_sensitive_payloads(
    tmp_path: Path,
) -> None:
    database, collector, record, _ = _collector_facts(tmp_path)

    first = collector.collect(record)
    second = collector.collect(record)
    serialized = first.model_dump_json()

    assert first == second
    assert len(first.telemetry_digest) == 64
    assert first.telemetry_digest == second.telemetry_digest
    for forbidden in (
        "must-not-escape",
        "C:/private",
        "private source",
        "private test output",
        "sensitive provider diagnostic",
        "provider_request_id",
        "arguments",
    ):
        assert forbidden not in serialized
    database.close()


class _CrossRunAttempts:
    def __init__(self, attempt: ModelAttemptRecord) -> None:
        self._attempt = attempt

    def list_attempts(self, run_id: UUID) -> list[ModelAttemptRecord]:
        del run_id
        return [self._attempt]


class _NoEvents:
    def list_for_run(self, run_id: UUID) -> list[Event]:
        del run_id
        return []


class _NoAttempts:
    def list_attempts(self, run_id: UUID) -> list[ModelAttemptRecord]:
        del run_id
        return []


def test_collector_rejects_cross_run_attempt_facts(tmp_path: Path) -> None:
    database, _, record, _ = _collector_facts(tmp_path)
    foreign_attempt = ModelAttemptRecord(
        attempt_id=uuid4(),
        run_id=uuid4(),
        logical_call_id=uuid4(),
        attempt_number=1,
        status="COMPLETED",
        request_digest="1" * 64,
        provider_identity="foreign/model",
        budget_digest="2" * 64,
        duration_ms=1,
        usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        created_at=record.created_at,
        dispatched_at=record.created_at,
        completed_at=record.completed_at,
    )
    collector = EvaluationTelemetryCollector(
        _CrossRunAttempts(foreign_attempt),  # type: ignore[arg-type]
        _NoEvents(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="cross-Run"):
        collector.collect(record)
    database.close()


def test_collector_rejects_cross_run_events(tmp_path: Path) -> None:
    database, _, record, _ = _collector_facts(tmp_path)

    class CrossRunEvents:
        def list_for_run(self, run_id: UUID) -> list[Event]:
            del run_id
            return [
                Event(
                    run_id=uuid4(),
                    event_type=EventType.TOOL_REQUESTED,
                    sequence_number=1,
                )
            ]

    bad_collector = EvaluationTelemetryCollector(
        _NoAttempts(),  # type: ignore[arg-type]
        CrossRunEvents(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="cross-Run"):
        bad_collector.collect(record)
    database.close()


def test_telemetry_model_forbids_unknown_fields(tmp_path: Path) -> None:
    database, collector, record, _ = _collector_facts(tmp_path)
    telemetry = collector.collect(record)
    data: dict[str, Any] = telemetry.model_dump(mode="json")
    data["raw_prompt"] = "must fail closed"

    with pytest.raises(ValueError):
        type(telemetry).model_validate(data)
    database.close()
