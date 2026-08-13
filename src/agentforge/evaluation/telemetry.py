from collections import Counter
from typing import Protocol
from uuid import UUID

from agentforge.domain.enums import EventType
from agentforge.domain.models import Event
from agentforge.evaluation.models import RepairEvaluationRun
from agentforge.evaluation.telemetry_models import EvaluationRunTelemetry
from agentforge.models.domain import ModelAttemptRecord, ModelErrorCode


class ModelAttemptSource(Protocol):
    def list_attempts(self, run_id: UUID) -> list[ModelAttemptRecord]: ...


class EvaluationEventSource(Protocol):
    def list_for_run(self, run_id: UUID) -> list[Event]: ...


class EvaluationTelemetryCollector:
    def __init__(
        self,
        model_attempts: ModelAttemptSource,
        events: EvaluationEventSource,
    ) -> None:
        self._model_attempts = model_attempts
        self._events = events

    def collect(self, record: RepairEvaluationRun) -> EvaluationRunTelemetry:
        attempts = self._model_attempts.list_attempts(record.run_id)
        events = self._events.list_for_run(record.run_id)
        if any(attempt.run_id != record.run_id for attempt in attempts):
            raise ValueError("Telemetry source contains cross-Run model attempts")
        if any(event.run_id != record.run_id for event in events):
            raise ValueError("Telemetry source contains cross-Run events")
        attempt_ids = [attempt.attempt_id for attempt in attempts]
        if len(set(attempt_ids)) != len(attempt_ids):
            raise ValueError("Telemetry source contains duplicate model attempts")

        logical_call_ids = {attempt.logical_call_id for attempt in attempts}
        if attempts and len(logical_call_ids) != record.model_calls:
            raise ValueError(
                "Persisted logical model calls do not match evaluation result"
            )
        completed = [
            attempt for attempt in attempts if attempt.status == "COMPLETED"
        ]
        failed = [attempt for attempt in attempts if attempt.status == "FAILED"]
        usages = [
            attempt.usage for attempt in attempts if attempt.usage is not None
        ]
        successful_durations = [
            attempt.duration_ms or 0 for attempt in completed
        ]
        event_counts = Counter(event.event_type for event in events)
        deviation_events = [
            event
            for event in events
            if event.event_type is EventType.MODEL_PROVIDER_DEVIATION
        ]

        return EvaluationRunTelemetry(
            evaluation_run_id=record.evaluation_run_id,
            run_id=record.run_id,
            campaign_id=record.campaign_id,
            attempt_id=record.attempt_id,
            protocol_digest=record.protocol_digest,
            model_id=record.model_id,
            logical_model_calls=record.model_calls,
            physical_model_requests=len(attempts),
            completed_model_requests=len(completed),
            failed_model_requests=len(failed),
            retry_count=max(0, len(attempts) - len(logical_call_ids)),
            usage_complete=bool(attempts)
            and all(self._has_complete_usage(attempt) for attempt in attempts),
            input_tokens=sum(usage.input_tokens or 0 for usage in usages),
            output_tokens=sum(usage.output_tokens or 0 for usage in usages),
            total_tokens=sum(usage.total_tokens or 0 for usage in usages),
            cached_input_tokens=sum(
                usage.cached_input_tokens or 0 for usage in usages
            ),
            reasoning_tokens=sum(usage.reasoning_tokens or 0 for usage in usages),
            successful_provider_duration_ms=sum(successful_durations),
            maximum_provider_duration_ms=max(successful_durations, default=0),
            model_attempt_elapsed_ms=sum(
                attempt.duration_ms or 0 for attempt in attempts
            ),
            provider_deviation_count=event_counts[
                EventType.MODEL_PROVIDER_DEVIATION
            ],
            normalized_multi_tool_response_count=event_counts[
                EventType.MULTI_TOOL_RESPONSE_NORMALIZED
            ],
            returned_function_call_count=sum(
                self._count_payload(event, "returned_call_count")
                for event in deviation_events
            ),
            discarded_function_call_count=sum(
                self._count_payload(event, "discarded_call_count")
                for event in deviation_events
            ),
            model_protocol_failure_count=sum(
                event.event_type is EventType.MODEL_FAILED
                and event.payload.get("error_type")
                in {
                    ModelErrorCode.MODEL_PROTOCOL_ERROR.value,
                    ModelErrorCode.MODEL_OUTPUT_INVALID.value,
                }
                for event in events
            ),
            tool_requested_count=event_counts[EventType.TOOL_REQUESTED],
            tool_completed_count=event_counts[EventType.TOOL_COMPLETED],
            tool_failed_count=event_counts[EventType.TOOL_FAILED],
            read_call_count=record.read_calls,
            mutation_requested_count=event_counts[EventType.MUTATION_REQUESTED],
            mutation_committed_count=event_counts[EventType.MUTATION_COMMITTED],
            mutation_failed_count=event_counts[EventType.MUTATION_FAILED],
            managed_test_requested_count=event_counts[EventType.TEST_REQUESTED],
            managed_test_completed_count=event_counts[EventType.TEST_COMPLETED],
            managed_test_failed_count=event_counts[EventType.TEST_FAILED],
            managed_test_timeout_count=event_counts[EventType.TEST_TIMEOUT],
            approval_requested_count=event_counts[EventType.APPROVAL_REQUESTED],
            approval_granted_count=event_counts[EventType.APPROVAL_GRANTED],
            approval_rejected_count=event_counts[EventType.APPROVAL_REJECTED],
            context_compaction_count=event_counts[EventType.CONTEXT_COMPACTED],
            completion_correction_count=record.completion_corrections,
            policy_violation_count=record.policy_violations,
        )

    @staticmethod
    def _has_complete_usage(attempt: ModelAttemptRecord) -> bool:
        usage = attempt.usage
        return (
            usage is not None
            and usage.input_tokens is not None
            and usage.output_tokens is not None
            and usage.total_tokens is not None
        )

    @staticmethod
    def _count_payload(event: Event, key: str) -> int:
        value = event.payload.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Telemetry event contains invalid {key}")
        return value
