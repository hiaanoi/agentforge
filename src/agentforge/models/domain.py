from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, model_validator

from agentforge.domain.enums import (
    ModelAttemptStatus,
    ModelRecoveryAction,
    MultiToolResponsePolicy,
)
from agentforge.domain.models import UtcDatetime
from agentforge.models.base import ModelOutput, ToolCall


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)


class MultiToolResponseInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    provider_request_id: str | None = Field(default=None, max_length=200)
    returned_call_count: int = Field(ge=2)
    selected_call_count: int = Field(ge=0, le=1)
    discarded_call_count: int = Field(ge=1)
    selected_tool_name: str | None = Field(default=None, max_length=100)
    discarded_tool_names: list[str] = Field(default_factory=list, max_length=10)
    policy: MultiToolResponsePolicy
    reason: str = Field(min_length=1, max_length=100)
    provider_contract_deviation: Literal[True] = True

    @model_validator(mode="after")
    def counts_must_balance(self) -> "MultiToolResponseInfo":
        if self.selected_call_count + self.discarded_call_count != self.returned_call_count:
            raise ValueError("Selected and discarded call counts must equal returned calls")
        if (self.selected_call_count == 1) != (self.selected_tool_name is not None):
            raise ValueError("Selected tool name must match selected call count")
        return self

    def audit_payload(self) -> dict[str, JsonValue]:
        discarded_names: list[JsonValue] = list(self.discarded_tool_names)
        return {
            "provider": self.provider,
            "model": self.model,
            "returned_call_count": self.returned_call_count,
            "selected_call_count": self.selected_call_count,
            "discarded_call_count": self.discarded_call_count,
            "selected_tool_name": self.selected_tool_name,
            "discarded_tool_names": discarded_names,
            "policy": self.policy.value,
            "reason": self.reason,
            "provider_request_id": self.provider_request_id,
        }


class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ModelOutput
    model_call_id: UUID | None = None
    usage: ModelUsage | None = None
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    provider_request_id: str | None = None
    duration_ms: int = Field(ge=0)
    attempt_count: int = Field(ge=1)
    sanitized_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    multi_tool_response: MultiToolResponseInfo | None = None

    @model_validator(mode="after")
    def normalized_multi_tool_response_must_match_action(self) -> "ModelResponse":
        info = self.multi_tool_response
        if info is None:
            return self
        if (
            info.policy is not MultiToolResponsePolicy.SEQUENTIAL_READ_ONLY
            or info.selected_call_count != 1
            or not isinstance(self.action, ToolCall)
            or self.action.tool != info.selected_tool_name
            or self.provider != info.provider
            or self.model != info.model
            or self.provider_request_id != info.provider_request_id
        ):
            raise ValueError("Normalized multi-tool response does not match selected action")
        return self


class ModelProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr = Field(exclude=True, repr=False)
    model: str = Field(min_length=1)
    base_url: str | None = Field(default=None, min_length=1)
    timeout_seconds: float = Field(default=30.0, gt=0)
    temperature: float | None = Field(default=None, ge=0.0, le=0.0)
    max_retries: int = Field(default=2, ge=0, le=10)
    store: Literal[False] = False
    max_output_tokens: int | None = Field(default=None, gt=0)
    multi_tool_response_policy: MultiToolResponsePolicy = (
        MultiToolResponsePolicy.SEQUENTIAL_READ_ONLY
    )
    max_function_calls_per_response: int = Field(default=8, ge=1, le=32)


class ModelErrorCode(StrEnum):
    MODEL_AUTH_ERROR = "MODEL_AUTH_ERROR"
    MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_TRANSPORT_ERROR = "MODEL_TRANSPORT_ERROR"
    MODEL_BAD_REQUEST = "MODEL_BAD_REQUEST"
    MODEL_PROVIDER_ERROR = "MODEL_PROVIDER_ERROR"
    MODEL_PROTOCOL_ERROR = "MODEL_PROTOCOL_ERROR"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    MODEL_BUDGET_EXCEEDED = "MODEL_BUDGET_EXCEEDED"


class ModelAttemptRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: UUID
    run_id: UUID
    logical_call_id: UUID
    attempt_number: int = Field(gt=0)
    status: ModelAttemptStatus
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_identity: str = Field(min_length=1, max_length=200)
    budget_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    error_type: ModelErrorCode | None = None
    retryable: bool | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    usage: ModelUsage | None = None
    created_at: UtcDatetime
    dispatched_at: UtcDatetime | None = None
    completed_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def lifecycle_facts_must_match_status(self) -> "ModelAttemptRecord":
        status = self.status
        if status is ModelAttemptStatus.PREPARED:
            valid = self.dispatched_at is None and self.completed_at is None
        elif status is ModelAttemptStatus.DISPATCHING:
            valid = self.dispatched_at is not None and self.completed_at is None
        else:
            valid = self.dispatched_at is not None and self.completed_at is not None
        if not valid:
            raise ValueError("model attempt timestamps do not match status")
        if status in {
            ModelAttemptStatus.PREPARED,
            ModelAttemptStatus.DISPATCHING,
            ModelAttemptStatus.INDETERMINATE,
        } and any(
            value is not None
            for value in (self.error_type, self.retryable, self.duration_ms, self.usage)
        ):
            raise ValueError("non-terminal model attempt contains terminal facts")
        if status is ModelAttemptStatus.COMPLETED and (
            self.error_type is not None
            or self.retryable is not None
            or self.duration_ms is None
        ):
            raise ValueError("completed model attempt facts are inconsistent")
        if status is ModelAttemptStatus.FAILED and (
            self.error_type is None or self.retryable is None or self.usage is not None
        ):
            raise ValueError("failed model attempt facts are inconsistent")
        return self


class ModelRecoveryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: ModelRecoveryAction
    attempt_number: int = Field(gt=0)


class ModelBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_model_requests: int = Field(default=20, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)
    max_output_tokens_per_request: int | None = Field(default=None, gt=0)
    max_total_input_tokens: int | None = Field(default=None, gt=0)
    max_total_output_tokens: int | None = Field(default=None, gt=0)
    max_total_tokens: int | None = Field(default=100_000, gt=0)


class ModelBudgetState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    model_request_count: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    budget: ModelBudget
