import hashlib
import json
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentforge.domain.models import UtcDatetime, utc_now
from agentforge.domain.test_execution import TestExecutionPlan


class BaselineExecutionStatus(StrEnum):
    CREATED = "CREATED"
    STARTED = "STARTED"
    VERIFIED_EXPECTED_FAILURE = "VERIFIED_EXPECTED_FAILURE"
    BLOCKED = "BLOCKED"
    INDETERMINATE = "INDETERMINATE"


class BaselineFailureReason(StrEnum):
    UNEXPECTED_PASS = "UNEXPECTED_PASS"
    FAILURE_FINGERPRINT_MISMATCH = "FAILURE_FINGERPRINT_MISMATCH"
    FAILURE_OUTPUT_UNPARSABLE = "FAILURE_OUTPUT_UNPARSABLE"
    COLLECTION_OR_IMPORT_ERROR = "COLLECTION_OR_IMPORT_ERROR"
    TIMEOUT = "TIMEOUT"
    LAUNCH_FAILURE = "LAUNCH_FAILURE"
    PROFILE_BINDING_MISMATCH = "PROFILE_BINDING_MISMATCH"
    CANCELLED = "CANCELLED"
    PROCESS_OUTCOME_INDETERMINATE = "PROCESS_OUTCOME_INDETERMINATE"
    PERSISTED_STATE_INVALID = "PERSISTED_STATE_INVALID"


def normalize_node_id(value: str) -> str:
    return value.strip().replace("\\", "/")


class ExpectedBaselineFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runner: Literal["pytest"] = "pytest"
    expected_exit_class: Literal["NON_ZERO"] = "NON_ZERO"
    failed_node_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    match_mode: Literal["EXACT_SET"] = "EXACT_SET"
    fingerprint_version: Literal[1] = 1

    @field_validator("failed_node_ids")
    @classmethod
    def validate_node_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(normalize_node_id(value) for value in values)
        if any(
            not value
            or len(value) > 500
            or not value.startswith("tests/visible/")
            or "::" not in value
            for value in normalized
        ):
            raise ValueError("Baseline node IDs must name bounded visible tests")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Baseline node IDs must be unique")
        return normalized

    @property
    def fingerprint_digest(self) -> str:
        payload = {
            "expected_exit_class": self.expected_exit_class,
            "failed_node_ids": sorted(self.failed_node_ids),
            "fingerprint_version": self.fingerprint_version,
            "match_mode": self.match_mode,
            "runner": self.runner,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class BaselineFailureSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    exit_code: int
    failed_node_ids: tuple[str, ...]
    failure_count: int = Field(ge=0)
    diagnostic_summary: str = Field(max_length=8_000)
    stdout_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    stderr_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    truncated: bool


class BaselineEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: BaselineExecutionStatus
    failure_reason: BaselineFailureReason | None = None
    failed_node_ids: tuple[str, ...] = ()
    actual_fingerprint_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    safe_summary: BaselineFailureSummary

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        verified = self.status is BaselineExecutionStatus.VERIFIED_EXPECTED_FAILURE
        if verified != (self.failure_reason is None):
            raise ValueError("Only verified baseline evaluations omit a failure reason")
        return self


class BaselineExecutionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline_execution_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    task_id: str = Field(min_length=1, max_length=200)
    workspace_baseline_id: UUID
    initial_workspace_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_plan: TestExecutionPlan
    expected_failure: ExpectedBaselineFailure
    status: BaselineExecutionStatus = BaselineExecutionStatus.CREATED
    failure_reason: BaselineFailureReason | None = None
    record_version: int = Field(default=1, gt=0)
    result_schema_version: int = Field(default=1, gt=0)
    actual_fingerprint_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    failed_node_ids: tuple[str, ...] = ()
    exit_code: int | None = None
    root_pid: int | None = Field(default=None, gt=0)
    process_group_id: int | None = Field(default=None, gt=0)
    job_id: str | None = Field(default=None, max_length=100)
    duration_ms: int = Field(default=0, ge=0)
    termination_reason: str | None = Field(default=None, max_length=100)
    termination_result: str | None = Field(default=None, max_length=500)
    stdout_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stderr_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stdout_size: int = Field(default=0, ge=0)
    stderr_size: int = Field(default=0, ge=0)
    output_truncated: bool = False
    safe_failure_summary: BaselineFailureSummary | None = None
    safe_failure_summary_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: UtcDatetime = Field(default_factory=utc_now)
    started_at: UtcDatetime | None = None
    completed_at: UtcDatetime | None = None

    @classmethod
    def create(
        cls,
        *,
        run_id: UUID,
        task_id: str,
        workspace_baseline_id: UUID,
        initial_workspace_digest: str,
        test_plan: TestExecutionPlan,
        expected_failure: ExpectedBaselineFailure,
    ) -> "BaselineExecutionRecord":
        return cls(
            run_id=run_id,
            task_id=task_id,
            workspace_baseline_id=workspace_baseline_id,
            initial_workspace_digest=initial_workspace_digest,
            test_plan=test_plan,
            expected_failure=expected_failure,
        )

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        terminal = self.status in {
            BaselineExecutionStatus.VERIFIED_EXPECTED_FAILURE,
            BaselineExecutionStatus.BLOCKED,
            BaselineExecutionStatus.INDETERMINATE,
        }
        if self.status is BaselineExecutionStatus.VERIFIED_EXPECTED_FAILURE:
            if self.failure_reason is not None or self.safe_failure_summary is None:
                raise ValueError("Verified baseline requires a result and no failure reason")
        if (
            self.status
            in {
                BaselineExecutionStatus.BLOCKED,
                BaselineExecutionStatus.INDETERMINATE,
            }
            and self.failure_reason is None
        ):
            raise ValueError("Unverified terminal baseline requires a failure reason")
        if terminal != (self.completed_at is not None):
            raise ValueError("Terminal baseline timestamp does not match status")
        return self
