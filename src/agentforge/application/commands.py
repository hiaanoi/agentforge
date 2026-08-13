from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel, Field, TypeAdapter, field_validator

from agentforge.application.contracts import ProfilePurpose
from agentforge.application.dto import PublicDto
from agentforge.application.run_commands import ResumeRecoveryChoice
from agentforge.domain.enums import ApprovalStatus, RejectionStrategy
from agentforge.persistence.profile_trust import TrustedProfileIdentity


class _ApplicationCommand(PublicDto):

    schema_version: Literal[1] = 1
    command_id: UUID

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the exact integer 1")
        return value


class StartRun(_ApplicationCommand):
    """Small public intent; Task 4 assembles the complete A1 Run bundle."""

    type: Literal["start_run"] = "start_run"
    task: str = Field(min_length=1, max_length=100_000)
    workspace: Path


class ResumeRun(_ApplicationCommand):
    type: Literal["resume_run"] = "resume_run"
    run_id: UUID
    recovery_choice: ResumeRecoveryChoice = ResumeRecoveryChoice.AUTO


class DecideApproval(_ApplicationCommand):
    type: Literal["decide_approval"] = "decide_approval"
    approval_id: UUID
    status: ApprovalStatus
    strategy: RejectionStrategy = RejectionStrategy.CONTINUE
    note: str | None = Field(default=None, max_length=500)

    @field_validator("status")
    @classmethod
    def terminal_decision(cls, value: ApprovalStatus) -> ApprovalStatus:
        if value not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
            raise ValueError("approval decision must be terminal")
        return value


class TrustProfile(_ApplicationCommand):
    type: Literal["trust_profile"] = "trust_profile"
    workspace_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    purpose: ProfilePurpose
    identity: TrustedProfileIdentity

    @field_validator("identity", mode="before")
    @classmethod
    def revalidate_identity(cls, value: object) -> object:
        if isinstance(value, TrustedProfileIdentity):
            return BaseModel.model_dump(value, mode="python")
        return value


ApplicationCommand: TypeAlias = Annotated[
    StartRun | ResumeRun | DecideApproval | TrustProfile,
    Field(discriminator="type"),
]
APPLICATION_COMMAND_ADAPTER: TypeAdapter[ApplicationCommand] = TypeAdapter(
    ApplicationCommand
)
