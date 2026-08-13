from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import Field, TypeAdapter, field_validator

from agentforge.application.contracts import ProfilePurpose
from agentforge.application.dto import PublicDto


class _ApplicationQuery(PublicDto):

    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the exact integer 1")
        return value


class RunDetails(_ApplicationQuery):
    type: Literal["run_details"] = "run_details"
    run_id: UUID


class PendingApprovals(_ApplicationQuery):
    type: Literal["pending_approvals"] = "pending_approvals"
    run_id: UUID | None = None


class DoctorReport(_ApplicationQuery):
    type: Literal["doctor_report"] = "doctor_report"
    workspace: Path


class ProfileTrustDetails(_ApplicationQuery):
    type: Literal["profile_trust_details"] = "profile_trust_details"
    workspace: Path
    profile_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    purpose: ProfilePurpose | None = None


class ExportRunDetails(_ApplicationQuery):
    type: Literal["export_run_details"] = "export_run_details"
    run_id: UUID


ApplicationQuery: TypeAlias = Annotated[
    RunDetails
    | PendingApprovals
    | DoctorReport
    | ProfileTrustDetails
    | ExportRunDetails,
    Field(discriminator="type"),
]
APPLICATION_QUERY_ADAPTER: TypeAdapter[ApplicationQuery] = TypeAdapter(ApplicationQuery)
