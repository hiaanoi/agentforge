from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentforge.domain.enums import ApprovalStatus, RejectionStrategy


class DecideApprovalCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    command_id: UUID
    approval_id: UUID
    status: ApprovalStatus
    strategy: RejectionStrategy = RejectionStrategy.CONTINUE
    note: str | None = Field(default=None, max_length=500)

    @field_validator("status")
    @classmethod
    def decision_is_terminal(cls, value: ApprovalStatus) -> ApprovalStatus:
        if value not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
            raise ValueError("approval decision must be approved or rejected")
        return value

    @property
    def command_type(self) -> str:
        return "DECIDE_APPROVAL"
