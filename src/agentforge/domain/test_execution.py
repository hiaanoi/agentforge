import re
from pathlib import Path, PureWindowsPath
from typing import Self
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, field_validator, model_validator

from agentforge.application.contracts import (
    ProfilePurpose,
    RuntimeTrustClass,
    VerificationCapsuleState,
    VerificationRuntimeMode,
)
from agentforge.domain.enums import (
    ConfigSourceKind,
    ProcessExecutionStatus,
    ProcessFailureKind,
)
from agentforge.domain.models import ApprovalRequired, DomainModel, UtcDatetime, utc_now

Sha256 = str
UNBOUND_SOURCE_REVISION_DIGEST = "0" * 64

_REVIEW_SAFE_FLAGS = frozenset({"-m", "-q"})
_REVIEW_SAFE_MODULE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
_REVIEW_CAPSULE_REFERENCE = re.compile(
    r"^(?:\{SOURCE\}|\{VERIFIER\}|\{SCRATCH\})"
    r"(?:/[A-Za-z0-9_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_][A-Za-z0-9_-]*)*)*$"
)


def redact_argv_for_review(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Return a safe-by-construction, non-executable argv review representation."""
    if not argv:
        return ()
    review = [argv[0]]
    expect_module = False
    for argument in argv[1:]:
        if expect_module:
            review.append(argument if _REVIEW_SAFE_MODULE.fullmatch(argument) else "<redacted>")
            expect_module = False
        elif argument == "-m":
            review.append(argument)
            expect_module = True
        elif argument in _REVIEW_SAFE_FLAGS or _REVIEW_CAPSULE_REFERENCE.fullmatch(argument):
            review.append(argument)
        else:
            review.append("<redacted>")
    return tuple(review)


def _is_absolute_path(value: str) -> bool:
    return Path(value).is_absolute() or PureWindowsPath(value).is_absolute()


class TestProfile(DomainModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    profile_id: str = Field(pattern=r"^[a-z_][a-z0-9_-]*$", max_length=100)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    executable_path: str = Field(min_length=1, max_length=4096)
    argv: tuple[str, ...] = Field(min_length=1, max_length=100)
    cwd: str = Field(min_length=1, max_length=4096)
    allowed_env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(gt=0, le=3600)
    max_output_bytes: int = Field(gt=0, le=10_000_000)
    enabled: bool = True
    profile_version: int = Field(gt=0)
    purpose: ProfilePurpose = ProfilePurpose.DEVELOPMENT
    runtime_mode: VerificationRuntimeMode = VerificationRuntimeMode.SYSTEM_RUNTIME
    verifier_root: str | None = Field(default=None, max_length=4096)
    executable_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    argv_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    cwd_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    environment_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    config_source_kind: ConfigSourceKind
    config_source_identity: str = Field(min_length=1, max_length=500)
    config_source_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    profile_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("executable_path", "cwd")
    @classmethod
    def paths_must_be_absolute(cls, value: str) -> str:
        if not _is_absolute_path(value):
            raise ValueError("TestProfile paths must be absolute")
        return value

    @field_validator("verifier_root")
    @classmethod
    def optional_path_must_be_absolute(cls, value: str | None) -> str | None:
        if value is not None and not _is_absolute_path(value):
            raise ValueError("TestProfile verifier_root must be absolute")
        return value

    @field_validator("argv")
    @classmethod
    def argv_must_be_safe(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or "\x00" in item for item in value):
            raise ValueError("TestProfile argv items must be non-empty and contain no NUL")
        return value

    @model_validator(mode="after")
    def executable_must_match_argv(self) -> Self:
        if self.argv[0] != self.executable_path:
            raise ValueError("argv[0] must be the resolved executable path")
        return self


class TestProfileSummary(DomainModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    profile_id: str
    name: str
    description: str
    profile_version: int
    timeout_seconds: float
    max_output_bytes: int
    enabled: bool


class TestExecutionPlan(DomainModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    profile_id: str = Field(min_length=1, max_length=100)
    profile_version: int = Field(gt=0)
    profile_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    executable_path: str = Field(min_length=1, max_length=4096)
    argv_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    cwd: str = Field(min_length=1, max_length=4096)
    environment_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")


class TestApprovalBinding(TestExecutionPlan):
    approval_id: UUID
    run_id: UUID
    checkpoint_id: UUID
    tool_call_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision_number: int = Field(default=0, ge=0)
    source_revision_digest: Sha256 = Field(
        default=UNBOUND_SOURCE_REVISION_DIGEST, pattern=r"^[0-9a-f]{64}$"
    )
    created_at: UtcDatetime = Field(default_factory=utc_now)


class TestApprovalRequired(ApprovalRequired):
    test_plan: TestExecutionPlan


class PendingTestExecution(DomainModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    approval_id: UUID
    execution_id: UUID | None = None
    profile_id: str = Field(min_length=1, max_length=100)
    profile_version: int = Field(gt=0)
    profile_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    argv_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    environment_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_number: int | None = Field(default=None, gt=0)


class TestResult(DomainModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    schema_version: int = Field(default=1, gt=0)
    success: bool
    failure_kind: ProcessFailureKind | None = None
    exit_code: int | None = None
    duration_ms: int = Field(ge=0)
    stdout_summary: str = Field(default="", max_length=20_000)
    stderr_summary: str = Field(default="", max_length=20_000)
    stdout_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    stderr_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    stdout_size: int = Field(ge=0)
    stderr_size: int = Field(ge=0)
    truncated: bool
    digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    termination_reason: str | None = Field(default=None, max_length=100)


class ProcessExecutionRecord(DomainModel):
    execution_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    approval_id: UUID
    tool_call_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_number: int = Field(gt=0)
    record_version: int = Field(default=1, gt=0)
    result_schema_version: int = Field(default=1, gt=0)
    profile_id: str = Field(min_length=1, max_length=100)
    profile_version: int = Field(gt=0)
    profile_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    executable_path: str = Field(min_length=1, max_length=4096)
    argv_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    cwd: str = Field(min_length=1, max_length=4096)
    environment_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision_number: int = Field(default=0, ge=0)
    source_revision_digest: Sha256 = Field(
        default=UNBOUND_SOURCE_REVISION_DIGEST, pattern=r"^[0-9a-f]{64}$"
    )
    source_digest_at_start: Sha256 | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    verification_capsule_id: UUID | None = None
    capsule_state: VerificationCapsuleState | None = None
    source_snapshot_digest: Sha256 | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    verifier_artifact_digest: Sha256 | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    executable_artifact_digest: Sha256 | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    artifact_algorithm_version: int | None = Field(default=None, ge=1)
    runtime_trust_class: RuntimeTrustClass | None = None
    root_pid: int | None = Field(default=None, gt=0)
    process_group_id: int | None = Field(default=None, gt=0)
    job_id: str | None = Field(default=None, max_length=100)
    status: ProcessExecutionStatus = ProcessExecutionStatus.CREATED
    failure_kind: ProcessFailureKind | None = None
    exit_code: int | None = None
    stdout_digest: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stderr_digest: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stdout_size: int = Field(default=0, ge=0)
    stderr_size: int = Field(default=0, ge=0)
    stdout_summary: str = Field(default="", max_length=20_000)
    stderr_summary: str = Field(default="", max_length=20_000)
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    duration_ms: int = Field(default=0, ge=0)
    termination_reason: str | None = Field(default=None, max_length=100)
    termination_result: str | None = Field(default=None, max_length=500)
    created_at: UtcDatetime = Field(default_factory=utc_now)
    updated_at: UtcDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_terminal_state(self) -> Self:
        if self.status is ProcessExecutionStatus.COMPLETED:
            if self.failure_kind is not None or self.exit_code != 0:
                raise ValueError("Completed tests require exit code zero and no failure kind")
        if self.status is ProcessExecutionStatus.FAILED and self.failure_kind is None:
            raise ValueError("Failed process executions require a failure kind")
        if self.failure_kind is ProcessFailureKind.TEST_FAILURE and (
            self.exit_code is None or self.exit_code == 0
        ):
            raise ValueError("Test failures require a non-zero exit code")
        return self
