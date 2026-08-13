from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Literal, Self
from unicodedata import category
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentforge.application.contracts import LifecycleStatus, OutcomeStatus, ProfilePurpose
from agentforge.application.dto import PublicDto
from agentforge.domain.enums import ApprovalStatus, ConfigSourceKind
from agentforge.domain.models import Run
from agentforge.domain.repair import RepairCompletionStatus
from agentforge.domain.test_execution import redact_argv_for_review
from agentforge.persistence.profile_trust import TrustedProfileIdentity


def _unsafe_display_text(value: str) -> bool:
    return any(
        category(character) in {"Cc", "Cf"}
        or 0xFDD0 <= ord(character) <= 0xFDEF
        or (ord(character) & 0xFFFF) in {0xFFFE, 0xFFFF}
        for character in value
    )


class _View(PublicDto):

    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the exact integer 1")
        return value


class RunProjectionFacts(BaseModel):
    """Validated durable inputs; never serialized as a public artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run: Run
    repair_status: RepairCompletionStatus
    event_count: int = Field(ge=0)
    last_cursor: int | None = Field(default=None, gt=0)
    source_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    config_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    profile_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("event_count", "last_cursor", mode="before")
    @classmethod
    def exact_integers(cls, value: object) -> object:
        if value is not None and type(value) is not int:
            raise ValueError("projection counters require exact integers")
        return value

    @model_validator(mode="after")
    def cursor_count_topology(self) -> Self:
        if (self.event_count == 0) != (self.last_cursor is None):
            raise ValueError("event count and cursor disagree")
        return self


class LocalRunDetailsView(_View):
    run_id: UUID
    lifecycle_status: LifecycleStatus
    outcome_status: OutcomeStatus | None
    current_step: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    event_count: int = Field(ge=0)
    last_cursor: int | None = Field(default=None, gt=0)
    created_at: datetime
    updated_at: datetime


class ExportRunDetailsView(LocalRunDetailsView):
    source_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    config_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    profile_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class PendingApprovalView(_View):
    approval_id: UUID
    run_id: UUID
    tool_name: str = Field(pattern=r"^[a-z_][a-z0-9_]*$", max_length=100)
    status: Literal[ApprovalStatus.PENDING] = ApprovalStatus.PENDING
    requested_at: datetime


class PendingApprovalsView(_View):
    approvals: tuple[PendingApprovalView, ...]


class DoctorCheckStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class DoctorCheckView(_View):
    check: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    status: DoctorCheckStatus
    safe_message: str = Field(min_length=1, max_length=500)


class DoctorReportView(_View):
    ready: bool
    checks: tuple[DoctorCheckView, ...]


class ProfileTrustDetailsView(_View):
    workspace_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_id: str = Field(pattern=r"^[a-z_][a-z0-9_-]*$", max_length=100)
    profile_version: int = Field(gt=0)
    purpose: ProfilePurpose
    trusted: bool
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    executable_path: str = Field(min_length=1, max_length=4096)
    executable_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    argv_review: tuple[str, ...] = Field(min_length=1, max_length=100)
    argv_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    cwd: str = Field(min_length=1, max_length=4096)
    cwd_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_source_kind: ConfigSourceKind
    config_source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("argv_review", mode="before")
    @classmethod
    def redact_argv(cls, value: object) -> object:
        if not isinstance(value, tuple) or any(type(item) is not str for item in value):
            raise ValueError("profile review argv must be a tuple of strings")
        return tuple(
            "<redacted>" if _unsafe_display_text(item) else item
            for item in redact_argv_for_review(value)
        )

    @field_validator("executable_path", "cwd")
    @classmethod
    def resolved_paths_are_absolute(cls, value: str) -> str:
        if _unsafe_display_text(value) or not (
            Path(value).is_absolute() or PureWindowsPath(value).is_absolute()
        ):
            raise ValueError("profile review paths must be resolved absolute paths")
        return value

    @model_validator(mode="after")
    def launch_identity_is_exact(self) -> Self:
        if self.argv_review[0] != self.executable_path or any(
            not argument or "\x00" in argument for argument in self.argv_review
        ):
            raise ValueError("profile review argv does not match the executable")
        return self

    def trusted_identity(self) -> TrustedProfileIdentity:
        return TrustedProfileIdentity(
            profile_id=self.profile_id,
            profile_version=self.profile_version,
            profile_digest=self.profile_digest,
            executable_digest=self.executable_digest,
            argv_digest=self.argv_digest,
            cwd_identity=self.cwd_identity,
            config_source_digest=self.config_source_digest,
        )
