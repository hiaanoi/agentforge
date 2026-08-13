from datetime import UTC, datetime
from typing import Annotated, Literal, Self, TypeAlias
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from agentforge.domain.enums import (
    ALLOWED_RUN_TRANSITIONS,
    ApprovalConsumptionState,
    ApprovalStatus,
    EventType,
    PathKind,
    RejectionStrategy,
    ResumePhase,
    RunStatus,
    ToolCapability,
    ToolErrorCode,
    ToolRisk,
    ToolSource,
)
from agentforge.domain.errors import InvalidStateTransitionError


def utc_now() -> datetime:
    return datetime.now(UTC)


def normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


UtcDatetime: TypeAlias = Annotated[datetime, AfterValidator(normalize_utc)]


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Run(DomainModel):
    run_id: UUID = Field(default_factory=uuid4)
    task: str = Field(min_length=1)
    status: RunStatus = RunStatus.CREATED
    current_step: int = Field(default=0, ge=0)
    max_steps: int = Field(default=10, gt=0)
    tool_call_count: int = Field(default=0, ge=0)
    max_tool_calls: int = Field(default=10, ge=0)
    model_provider: str = Field(default="mock", min_length=1)
    created_at: UtcDatetime = Field(default_factory=utc_now)
    updated_at: UtcDatetime = Field(default_factory=utc_now)
    total_token_usage: int = Field(default=0, ge=0)
    estimated_cost: float = Field(default=0.0, ge=0)
    error_message: str | None = None
    final_output: str | None = None

    def transition_to(self, target: RunStatus) -> Self:
        if target not in ALLOWED_RUN_TRANSITIONS[self.status]:
            raise InvalidStateTransitionError(
                f"Run cannot transition from {self.status.value} to {target.value}"
            )
        self.status = target
        self.updated_at = utc_now()
        return self


class Event(DomainModel):
    event_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    event_type: EventType
    sequence_number: int = Field(gt=0)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: UtcDatetime = Field(default_factory=utc_now)


class PersistedEvent(DomainModel):
    schema_version: int = Field(default=1, strict=True)
    event_id: UUID
    global_cursor: int = Field(gt=0, strict=True)
    scope_type: Literal["RUN", "CONVERSATION", "WORKSPACE"]
    scope_id: str = Field(min_length=1)
    run_id: UUID | None = None
    sequence_number: int | None = Field(default=None, gt=0, strict=True)
    event_type: str = Field(min_length=1)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: UtcDatetime

    @field_validator("schema_version")
    @classmethod
    def event_schema_version_is_one(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported Event schema")
        return value

    @model_validator(mode="after")
    def scope_topology_is_closed(self) -> Self:
        if self.scope_type == "RUN":
            if (
                self.run_id is None
                or self.sequence_number is None
                or self.scope_id != str(self.run_id)
            ):
                raise ValueError("invalid Event scope topology")
        elif self.run_id is not None or self.sequence_number is not None:
            raise ValueError("invalid Event scope topology")
        return self


class Checkpoint(DomainModel):
    checkpoint_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    step_number: int = Field(ge=0)
    runtime_state: dict[str, JsonValue]
    created_at: UtcDatetime = Field(default_factory=utc_now)


class PendingToolCall(DomainModel):
    tool: str = Field(min_length=1)
    call_id: str | None = None
    arguments: dict[str, JsonValue]
    reason: str | None = None


class RuntimeSnapshot(DomainModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    step_number: int = Field(ge=0)
    history: list[JsonValue] = Field(default_factory=list)
    pending_tool_call: PendingToolCall | None = None
    pending_approval_id: UUID | None = None
    tool_call_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    resume_phase: ResumePhase


class ApprovalRequest(DomainModel):
    approval_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    checkpoint_id: UUID
    tool_name: str = Field(min_length=1)
    sanitized_arguments: dict[str, JsonValue]
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: ApprovalStatus = ApprovalStatus.PENDING
    rejection_strategy: RejectionStrategy = RejectionStrategy.CONTINUE
    consumption_state: ApprovalConsumptionState = ApprovalConsumptionState.NOT_STARTED
    decision_note: str | None = Field(default=None, max_length=500)
    result_status: str | None = Field(default=None, max_length=100)
    result_summary: str | None = Field(default=None, max_length=500)
    requested_at: UtcDatetime = Field(default_factory=utc_now)
    decided_at: UtcDatetime | None = None
    consumed_at: UtcDatetime | None = None


class ApprovalRequired(DomainModel):
    tool_name: str
    validated_arguments: dict[str, JsonValue]
    sanitized_arguments: dict[str, JsonValue]


class ApprovalAuthorization(DomainModel):
    approval_id: UUID
    checkpoint_id: UUID
    step_number: int = Field(ge=0)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ToolSpec(DomainModel):
    name: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    description: str = Field(min_length=1)
    input_schema: dict[str, JsonValue]
    risk_level: ToolRisk
    source: ToolSource = ToolSource.LOCAL
    capability: ToolCapability = ToolCapability.NONE
    timeout_seconds: float = Field(default=10.0, gt=0)
    requires_approval: bool = False
    allowed_paths: list[str] = Field(default_factory=list)
    allowed_commands: list[str] = Field(default_factory=list)
    path_argument: str | None = None
    path_kind: PathKind = PathKind.ANY
    protect_sensitive_path: bool = False

    @field_validator("description")
    @classmethod
    def description_must_contain_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Tool description must not be blank")
        return stripped


class ToolResult(DomainModel):
    success: bool
    output: JsonValue | None = None
    error_type: ToolErrorCode | None = None
    error_message: str | None = None
    duration_ms: int = Field(default=0, ge=0)
    truncated: bool = False
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class RunBudget(DomainModel):
    max_steps: int = Field(default=10, gt=0)
    max_model_calls: int = Field(default=10, gt=0)
    max_tool_calls: int = Field(default=10, ge=0)
    max_total_tokens: int = Field(default=100_000, gt=0)
    max_estimated_cost: float = Field(default=10.0, ge=0)
    max_wall_time_seconds: float = Field(default=300.0, gt=0)
