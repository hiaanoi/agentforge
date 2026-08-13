from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agentforge.domain.enums import (
    EvaluationAttemptStatus,
    EvaluationCampaignStatus,
    EvaluationSlotStatus,
    EventType,
)
from agentforge.domain.models import UtcDatetime, utc_now


class EvaluationCampaign(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_id: UUID = Field(default_factory=uuid4)
    protocol_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_id: str = Field(min_length=1, max_length=200)
    repetition_count: int = Field(gt=0)
    status: EvaluationCampaignStatus = EvaluationCampaignStatus.CREATED
    record_version: int = Field(default=1, gt=0)
    created_at: UtcDatetime = Field(default_factory=utc_now)
    updated_at: UtcDatetime = Field(default_factory=utc_now)
    completed_at: UtcDatetime | None = None


class EvaluationSlot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_id: UUID = Field(default_factory=uuid4)
    campaign_id: UUID
    protocol_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_id: str = Field(min_length=1, max_length=200)
    repetition_index: int = Field(ge=0)
    status: EvaluationSlotStatus = EvaluationSlotStatus.PENDING
    selected_attempt_id: UUID | None = None
    selected_evaluation_run_id: UUID | None = None
    record_version: int = Field(default=1, gt=0)
    created_at: UtcDatetime = Field(default_factory=utc_now)
    updated_at: UtcDatetime = Field(default_factory=utc_now)
    completed_at: UtcDatetime | None = None


class EvaluationPilotAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: UUID = Field(default_factory=uuid4)
    campaign_id: UUID
    slot_id: UUID
    protocol_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_id: str = Field(min_length=1, max_length=200)
    repetition_index: int = Field(ge=0)
    attempt_number: int = Field(gt=0)
    status: EvaluationAttemptStatus = EvaluationAttemptStatus.CREATED
    predecessor_attempt_id: UUID | None = None
    predecessor_evaluation_run_id: UUID | None = None
    workspace_lease_id: UUID | None = None
    workspace_root_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    initial_workspace_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    workspace_path: Path | None = None
    run_id: UUID | None = None
    baseline_execution_id: UUID | None = None
    evaluation_run_id: UUID | None = None
    failure_category: str | None = Field(default=None, max_length=100)
    infrastructure_failure: bool = False
    record_version: int = Field(default=1, gt=0)
    created_at: UtcDatetime = Field(default_factory=utc_now)
    updated_at: UtcDatetime = Field(default_factory=utc_now)
    started_at: UtcDatetime | None = None
    completed_at: UtcDatetime | None = None


class EvaluationCampaignEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID = Field(default_factory=uuid4)
    campaign_id: UUID
    event_type: EventType
    sequence_number: int = Field(gt=0)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: UtcDatetime = Field(default_factory=utc_now)
