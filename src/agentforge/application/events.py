from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self, TypeAlias
from uuid import UUID

from pydantic import Field, TypeAdapter, field_validator, model_validator

from agentforge.application.contracts import LifecycleStatus, OutcomeStatus, ProfilePurpose
from agentforge.application.dto import PublicDto
from agentforge.domain.enums import ApprovalStatus


class ProductEventName(StrEnum):
    RUN_STATE = "run_state"
    RUN_FINISHED = "run_finished"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"
    PROFILE_TRUSTED = "profile_trusted"
    VERIFICATION_COMPLETED = "verification_completed"
    PROGRESS = "progress"


_ALLOWED_SCOPES_BY_EVENT: dict[ProductEventName, frozenset[str]] = {
    ProductEventName.RUN_STATE: frozenset({"RUN"}),
    ProductEventName.RUN_FINISHED: frozenset({"RUN"}),
    ProductEventName.APPROVAL_REQUESTED: frozenset({"RUN"}),
    ProductEventName.APPROVAL_DECIDED: frozenset({"RUN"}),
    ProductEventName.PROFILE_TRUSTED: frozenset({"WORKSPACE"}),
    ProductEventName.VERIFICATION_COMPLETED: frozenset({"RUN"}),
    ProductEventName.PROGRESS: frozenset({"RUN"}),
}


class ProductProgressStage(StrEnum):
    MODEL = "model"
    TOOL = "tool"
    MUTATION = "mutation"
    TEST = "test"
    POLICY = "policy"
    RUNTIME = "runtime"


class _Payload(PublicDto):
    pass


class RunStatePayload(_Payload):
    type: Literal["run_state"] = "run_state"
    lifecycle_status: LifecycleStatus
    outcome_status: OutcomeStatus | None

    @model_validator(mode="after")
    def valid_status_pair(self) -> Self:
        if self.lifecycle_status is LifecycleStatus.TERMINAL:
            if self.outcome_status is None:
                raise ValueError("terminal lifecycle requires an outcome")
        elif self.lifecycle_status in {LifecycleStatus.CREATED, LifecycleStatus.RUNNING}:
            if self.outcome_status is not None:
                raise ValueError("active lifecycle cannot claim an outcome")
        elif self.outcome_status is not OutcomeStatus.UNVERIFIED:
            raise ValueError("paused lifecycle is unverified")
        return self


class RunFinishedPayload(_Payload):
    type: Literal["run_finished"] = "run_finished"
    lifecycle_status: LifecycleStatus
    outcome_status: OutcomeStatus | None

    @model_validator(mode="after")
    def terminal_status_pair(self) -> Self:
        if self.lifecycle_status is not LifecycleStatus.TERMINAL or self.outcome_status is None:
            raise ValueError("finished Run requires a terminal outcome")
        return self


class ApprovalRequestedPayload(_Payload):
    type: Literal["approval_requested"] = "approval_requested"
    approval_id: UUID
    tool_name: str = Field(pattern=r"^[a-z_][a-z0-9_]*$", max_length=100)


class ApprovalDecidedPayload(_Payload):
    type: Literal["approval_decided"] = "approval_decided"
    approval_id: UUID
    status: ApprovalStatus

    @field_validator("status")
    @classmethod
    def terminal_status(cls, value: ApprovalStatus) -> ApprovalStatus:
        if value not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
            raise ValueError("approval projection must be terminal")
        return value


class ProfileTrustedPayload(_Payload):
    type: Literal["profile_trusted"] = "profile_trusted"
    profile_id: str = Field(pattern=r"^[a-z_][a-z0-9_-]*$", max_length=100)
    profile_version: int = Field(gt=0)
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    purpose: ProfilePurpose

    @field_validator("profile_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("profile_version must be an exact integer")
        return value


class VerificationCompletedPayload(_Payload):
    type: Literal["verification_completed"] = "verification_completed"
    outcome_status: Literal[OutcomeStatus.VERIFIED, OutcomeStatus.FAILED]


class ProgressPayload(_Payload):
    type: Literal["progress"] = "progress"
    stage: ProductProgressStage


ProductEventPayload: TypeAlias = Annotated[
    RunStatePayload
    | RunFinishedPayload
    | ApprovalRequestedPayload
    | ApprovalDecidedPayload
    | ProfileTrustedPayload
    | VerificationCompletedPayload
    | ProgressPayload,
    Field(discriminator="type"),
]


class ProductEvent(PublicDto):

    schema_version: Literal[1] = 1
    event_id: UUID
    event: ProductEventName | None = None
    scope_type: Literal["RUN", "CONVERSATION", "WORKSPACE"]
    scope_id: str = Field(min_length=1, max_length=200)
    cursor: int = Field(gt=0)
    run_id: UUID | None = None
    sequence_number: int | None = Field(default=None, gt=0)
    occurred_at: datetime
    payload: ProductEventPayload

    @field_validator("schema_version", "cursor", "sequence_number", mode="before")
    @classmethod
    def exact_integers(cls, value: object) -> object:
        if value is not None and type(value) is not int:
            raise ValueError("integer fields require exact integers")
        return value

    @model_validator(mode="after")
    def closed_topology(self) -> Self:
        expected_event = ProductEventName(self.payload.type)
        if self.event is None:
            object.__setattr__(self, "event", expected_event)
        elif self.event is not expected_event:
            raise ValueError("event and payload disagree")
        if self.scope_type not in _ALLOWED_SCOPES_BY_EVENT[expected_event]:
            raise ValueError("event kind is not allowed in this scope")
        if self.scope_type == "RUN":
            if (
                self.run_id is None
                or self.sequence_number is None
                or self.scope_id != str(self.run_id)
            ):
                raise ValueError("invalid Run event scope")
        elif self.run_id is not None or self.sequence_number is not None:
            raise ValueError("non-Run scope cannot carry Run ordering")
        if self.scope_type == "WORKSPACE" and (
            len(self.scope_id) != 64
            or any(character not in "0123456789abcdef" for character in self.scope_id)
        ):
            raise ValueError("workspace scope must be a digest identity")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("event time must be timezone-aware")
        return self


PRODUCT_EVENT_ADAPTER = TypeAdapter(ProductEvent)
