from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentforge.evaluation.protocol import canonical_digest


class EvaluationRunTelemetry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    evaluation_run_id: UUID
    run_id: UUID
    campaign_id: UUID
    attempt_id: UUID
    protocol_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str = Field(min_length=1, max_length=200)
    logical_model_calls: int = Field(ge=0)
    physical_model_requests: int = Field(ge=0)
    completed_model_requests: int = Field(ge=0)
    failed_model_requests: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    usage_complete: bool
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)
    successful_provider_duration_ms: int = Field(ge=0)
    maximum_provider_duration_ms: int = Field(ge=0)
    model_attempt_elapsed_ms: int = Field(ge=0)
    provider_deviation_count: int = Field(ge=0)
    normalized_multi_tool_response_count: int = Field(ge=0)
    returned_function_call_count: int = Field(ge=0)
    discarded_function_call_count: int = Field(ge=0)
    model_protocol_failure_count: int = Field(ge=0)
    tool_requested_count: int = Field(ge=0)
    tool_completed_count: int = Field(ge=0)
    tool_failed_count: int = Field(ge=0)
    read_call_count: int = Field(ge=0)
    mutation_requested_count: int = Field(ge=0)
    mutation_committed_count: int = Field(ge=0)
    mutation_failed_count: int = Field(ge=0)
    managed_test_requested_count: int = Field(ge=0)
    managed_test_completed_count: int = Field(ge=0)
    managed_test_failed_count: int = Field(ge=0)
    managed_test_timeout_count: int = Field(ge=0)
    approval_requested_count: int = Field(ge=0)
    approval_granted_count: int = Field(ge=0)
    approval_rejected_count: int = Field(ge=0)
    context_compaction_count: int = Field(ge=0)
    completion_correction_count: int = Field(ge=0)
    policy_violation_count: int = Field(ge=0)
    telemetry_digest: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_counts_and_digest(self) -> Self:
        terminal_requests = (
            self.completed_model_requests + self.failed_model_requests
        )
        if terminal_requests > self.physical_model_requests:
            raise ValueError("Terminal model request counts exceed physical requests")
        if self.retry_count > self.physical_model_requests:
            raise ValueError("Retry count exceeds physical model requests")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("Cached input tokens exceed input tokens")
        if (
            self.maximum_provider_duration_ms
            > self.successful_provider_duration_ms
        ):
            raise ValueError("Maximum Provider duration exceeds total duration")
        expected = canonical_digest(
            self.model_dump(mode="json", exclude={"telemetry_digest"})
        )
        if self.telemetry_digest and self.telemetry_digest != expected:
            raise ValueError("Telemetry digest does not match immutable facts")
        object.__setattr__(self, "telemetry_digest", expected)
        return self
