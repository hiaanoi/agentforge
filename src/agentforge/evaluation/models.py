from typing import Any, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentforge.domain.enums import (
    EvaluationFailureClass,
    EvaluationOutcomeClass,
)
from agentforge.domain.models import UtcDatetime, utc_now
from agentforge.domain.repair import (
    BudgetProfile,
    CompletionCorrectionMode,
    RepairCompletionStatus,
    RepairTerminationReason,
)
from agentforge.evaluation.outcomes import classify_evaluation_outcome


class RepairEvaluationRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_run_id: UUID = Field(default_factory=uuid4)
    protocol_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    campaign_id: UUID
    slot_id: UUID
    attempt_id: UUID
    attempt_number: int = Field(gt=0)
    task_id: str = Field(min_length=1, max_length=200)
    repetition_index: int = Field(ge=0)
    model_id: str = Field(min_length=1, max_length=200)
    model_parameters_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    system_prompt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_prompt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_policy_version: int = Field(gt=0)
    initial_workspace_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    budget_profile: BudgetProfile
    completion_correction_mode: CompletionCorrectionMode
    run_id: UUID
    baseline_execution_id: UUID | None = None
    final_status: RepairCompletionStatus
    verified_success: bool
    model_calls: int = Field(ge=0)
    read_calls: int = Field(ge=0)
    edit_attempts: int = Field(ge=0)
    test_runs: int = Field(ge=0)
    completion_corrections: int = Field(ge=0)
    policy_violations: int = Field(ge=0)
    wall_time_ms: int = Field(ge=0)
    token_usage: int | None = Field(default=None, ge=0)
    final_workspace_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    final_diff_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    development_test_execution_id: UUID | None = None
    final_verification_execution_id: UUID | None = None
    failure_category: str | None = Field(default=None, max_length=100)
    outcome_class: EvaluationOutcomeClass
    failure_class: EvaluationFailureClass
    infrastructure_failure: bool = False
    replacement_for_evaluation_run_id: UUID | None = None
    created_at: UtcDatetime = Field(default_factory=utc_now)
    completed_at: UtcDatetime = Field(default_factory=utc_now)
    result_schema_version: int = Field(default=2, gt=0)

    @model_validator(mode="before")
    @classmethod
    def populate_outcome(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        status_value = value.get("final_status")
        if not isinstance(status_value, (str, RepairCompletionStatus)):
            return value
        try:
            status = RepairCompletionStatus(status_value)
        except (TypeError, ValueError):
            return value
        reason_value = value.get("failure_category")
        try:
            reason = (
                RepairTerminationReason(reason_value)
                if reason_value is not None
                else None
            )
        except ValueError:
            reason = None
        outcome = classify_evaluation_outcome(
            status=status,
            failure_reason=reason,
        )
        populated = dict(value)
        populated.setdefault("outcome_class", outcome.outcome_class)
        populated.setdefault("failure_class", outcome.failure_class)
        populated.setdefault(
            "infrastructure_failure",
            outcome.infrastructure_failure,
        )
        return populated

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.final_status is RepairCompletionStatus.RUNNING:
            raise ValueError("Evaluation result requires a terminal status")
        if self.replacement_for_evaluation_run_id == self.evaluation_run_id:
            raise ValueError("Evaluation run cannot replace itself")
        if (
            self.replacement_for_evaluation_run_id is not None
            and self.attempt_number <= 1
        ):
            raise ValueError("Replacement evaluation run requires a later attempt")
        if self.verified_success != (
            self.final_status is RepairCompletionStatus.VERIFIED_SUCCESS
        ):
            raise ValueError("verified_success must match final_status")
        if self.verified_success and self.infrastructure_failure:
            raise ValueError("Verified success cannot be an infrastructure failure")
        if self.verified_success and self.failure_category is not None:
            raise ValueError("Verified success cannot carry a failure category")
        try:
            failure_reason = (
                RepairTerminationReason(self.failure_category)
                if self.failure_category is not None
                else None
            )
        except ValueError:
            failure_reason = None
        expected_outcome = classify_evaluation_outcome(
            status=self.final_status,
            failure_reason=failure_reason,
        )
        if (
            self.outcome_class is not expected_outcome.outcome_class
            or self.failure_class is not expected_outcome.failure_class
            or self.infrastructure_failure
            is not expected_outcome.infrastructure_failure
        ):
            raise ValueError(
                "Persisted evaluation outcome does not match final status "
                "and failure category"
            )
        if self.verified_success and (
            self.final_workspace_digest is None
            or self.final_diff_digest is None
            or self.final_verification_execution_id is None
        ):
            raise ValueError("Verified success requires final verification evidence")
        return self


class TaskEvaluationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    repetitions: int = Field(gt=0)
    verified_success_count: int = Field(ge=0)
    run_level_success_rate: float = Field(ge=0, le=1)
    majority_success: bool
    stable_success: bool
    any_success: bool
    mean_model_calls: float = Field(ge=0)
    mean_edit_attempts: float = Field(ge=0)
    mean_test_runs: float = Field(ge=0)
    median_wall_time_ms: float = Field(ge=0)
    completion_correction_rate: float = Field(ge=0, le=1)
    policy_block_rate: float = Field(ge=0, le=1)
    budget_exhaustion_rate: float = Field(ge=0, le=1)
    infrastructure_failure_rate: float = Field(ge=0, le=1)
    failure_distribution: dict[str, int]
