from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentforge.domain.enums import (
    EvaluationFailureClass,
    EvaluationOutcomeClass,
)
from agentforge.domain.repair import (
    BudgetProfile,
    CompletionCorrectionMode,
    RepairCompletionStatus,
    RepairTerminationReason,
)
from agentforge.evaluation.models import RepairEvaluationRun
from agentforge.evaluation.outcomes import classify_evaluation_outcome

SHA = "a" * 64


@pytest.mark.parametrize(
    ("status", "reason", "failure_class"),
    [
        (
            RepairCompletionStatus.TESTS_FAILED,
            RepairTerminationReason.DEVELOPMENT_TEST_FAILED,
            EvaluationFailureClass.MODEL_QUALITY,
        ),
        (
            RepairCompletionStatus.FINAL_VERIFICATION_FAILED,
            RepairTerminationReason.FINAL_HIDDEN_TEST_FAILED,
            EvaluationFailureClass.MODEL_QUALITY,
        ),
        (
            RepairCompletionStatus.UNVERIFIED_FINAL,
            RepairTerminationReason.LATEST_MUTATION_NOT_VERIFIED,
            EvaluationFailureClass.MODEL_QUALITY,
        ),
        (
            RepairCompletionStatus.BUDGET_EXHAUSTED,
            RepairTerminationReason.MODEL_CALL_LIMIT,
            EvaluationFailureClass.MODEL_QUALITY,
        ),
        (
            RepairCompletionStatus.POLICY_BLOCKED,
            RepairTerminationReason.UNSUPPORTED_CAPABILITY,
            EvaluationFailureClass.MODEL_QUALITY,
        ),
        (
            RepairCompletionStatus.DIFF_POLICY_VIOLATION,
            RepairTerminationReason.DIFF_POLICY_VIOLATION,
            EvaluationFailureClass.MODEL_QUALITY,
        ),
        (
            RepairCompletionStatus.LOOP_DETECTED,
            RepairTerminationReason.LOOP_DETECTED,
            EvaluationFailureClass.MODEL_QUALITY,
        ),
        (
            RepairCompletionStatus.MODEL_PROTOCOL_ERROR,
            RepairTerminationReason.MODEL_PROTOCOL_ERROR,
            EvaluationFailureClass.MODEL_QUALITY,
        ),
    ],
)
def test_model_quality_failures_are_scored_and_never_replaced(
    status: RepairCompletionStatus,
    reason: RepairTerminationReason,
    failure_class: EvaluationFailureClass,
) -> None:
    outcome = classify_evaluation_outcome(status=status, failure_reason=reason)

    assert outcome.outcome_class is EvaluationOutcomeClass.SCORED
    assert outcome.failure_class is failure_class
    assert outcome.infrastructure_failure is False
    assert outcome.replaceable is False
    assert outcome.abort_study is False


def test_verified_success_is_scored_without_failure() -> None:
    outcome = classify_evaluation_outcome(
        status=RepairCompletionStatus.VERIFIED_SUCCESS,
        failure_reason=None,
    )

    assert outcome.outcome_class is EvaluationOutcomeClass.SCORED
    assert outcome.failure_class is EvaluationFailureClass.NONE
    assert outcome.infrastructure_failure is False
    assert outcome.replaceable is False
    assert outcome.abort_study is False


@pytest.mark.parametrize(
    "reason",
    [
        RepairTerminationReason.MODEL_RATE_LIMITED,
        RepairTerminationReason.MODEL_TIMEOUT,
        RepairTerminationReason.MODEL_TRANSPORT_ERROR,
        RepairTerminationReason.MODEL_PROVIDER_ERROR,
    ],
)
def test_allowlisted_provider_failures_are_replaceable_infrastructure(
    reason: RepairTerminationReason,
) -> None:
    outcome = classify_evaluation_outcome(
        status=RepairCompletionStatus.RUNTIME_FAILURE,
        failure_reason=reason,
    )

    assert outcome.outcome_class is EvaluationOutcomeClass.INFRASTRUCTURE_INVALID
    assert outcome.failure_class is EvaluationFailureClass.INFRASTRUCTURE
    assert outcome.infrastructure_failure is True
    assert outcome.replaceable is True
    assert outcome.abort_study is False


@pytest.mark.parametrize(
    "reason",
    [
        RepairTerminationReason.MODEL_AUTH_ERROR,
        RepairTerminationReason.MODEL_BAD_REQUEST,
    ],
)
def test_configuration_failures_abort_without_replacement(
    reason: RepairTerminationReason,
) -> None:
    outcome = classify_evaluation_outcome(
        status=RepairCompletionStatus.RUNTIME_FAILURE,
        failure_reason=reason,
    )

    assert outcome.outcome_class is EvaluationOutcomeClass.INFRASTRUCTURE_INVALID
    assert outcome.failure_class is EvaluationFailureClass.CONFIGURATION
    assert outcome.infrastructure_failure is True
    assert outcome.replaceable is False
    assert outcome.abort_study is True


def test_runtime_failure_is_nonreplaceable_infrastructure_by_default() -> None:
    outcome = classify_evaluation_outcome(
        status=RepairCompletionStatus.RUNTIME_FAILURE,
        failure_reason=RepairTerminationReason.RUNTIME_FAILURE,
    )

    assert outcome.outcome_class is EvaluationOutcomeClass.INFRASTRUCTURE_INVALID
    assert outcome.failure_class is EvaluationFailureClass.INFRASTRUCTURE
    assert outcome.infrastructure_failure is True
    assert outcome.replaceable is False
    assert outcome.abort_study is False


def test_deterministic_model_tool_failure_is_scored_quality_failure() -> None:
    outcome = classify_evaluation_outcome(
        status=RepairCompletionStatus.MODEL_TOOL_FAILED,
        failure_reason=RepairTerminationReason.MODEL_TOOL_FAILED,
    )

    assert outcome.outcome_class is EvaluationOutcomeClass.SCORED
    assert outcome.failure_class is EvaluationFailureClass.MODEL_QUALITY
    assert outcome.infrastructure_failure is False
    assert outcome.abort_study is False


def test_indeterminate_side_effect_aborts_and_is_never_replaceable() -> None:
    outcome = classify_evaluation_outcome(
        status=RepairCompletionStatus.INDETERMINATE,
        failure_reason=RepairTerminationReason.INDETERMINATE_SIDE_EFFECT,
    )

    assert outcome.outcome_class is EvaluationOutcomeClass.INDETERMINATE
    assert outcome.failure_class is EvaluationFailureClass.SAFETY_INDETERMINATE
    assert outcome.infrastructure_failure is False
    assert outcome.replaceable is False
    assert outcome.abort_study is True


def _evaluation_run_data() -> dict[str, object]:
    return {
        "protocol_digest": SHA,
        "campaign_id": uuid4(),
        "slot_id": uuid4(),
        "attempt_id": uuid4(),
        "attempt_number": 1,
        "task_id": "scored-failure",
        "repetition_index": 0,
        "model_id": "test-model",
        "model_parameters_digest": SHA,
        "system_prompt_digest": SHA,
        "task_prompt_digest": SHA,
        "tool_schema_digest": SHA,
        "context_policy_version": 1,
        "initial_workspace_digest": SHA,
        "task_policy_digest": SHA,
        "budget_profile": BudgetProfile.BASIC,
        "completion_correction_mode": CompletionCorrectionMode.DEFAULT,
        "run_id": uuid4(),
        "final_status": RepairCompletionStatus.TESTS_FAILED,
        "verified_success": False,
        "model_calls": 2,
        "read_calls": 1,
        "edit_attempts": 1,
        "test_runs": 1,
        "completion_corrections": 0,
        "policy_violations": 0,
        "wall_time_ms": 100,
        "failure_category": RepairTerminationReason.DEVELOPMENT_TEST_FAILED.value,
        "outcome_class": EvaluationOutcomeClass.SCORED,
        "failure_class": EvaluationFailureClass.MODEL_QUALITY,
        "infrastructure_failure": False,
    }


def test_evaluation_run_accepts_consistent_scored_failure_outcome() -> None:
    record = RepairEvaluationRun.model_validate(_evaluation_run_data())

    assert record.outcome_class is EvaluationOutcomeClass.SCORED
    assert record.failure_class is EvaluationFailureClass.MODEL_QUALITY
    assert record.infrastructure_failure is False


def test_evaluation_run_rejects_nonterminal_status() -> None:
    data = _evaluation_run_data()
    data.update(
        {
            "final_status": RepairCompletionStatus.RUNNING,
            "failure_category": None,
            "outcome_class": EvaluationOutcomeClass.SCORED,
            "failure_class": EvaluationFailureClass.MODEL_QUALITY,
        }
    )

    with pytest.raises(ValidationError, match="terminal"):
        RepairEvaluationRun.model_validate(data)


def test_verified_success_rejects_failure_category() -> None:
    data = _evaluation_run_data()
    data.update(
        {
            "final_status": RepairCompletionStatus.VERIFIED_SUCCESS,
            "verified_success": True,
            "failure_category": RepairTerminationReason.MODEL_TIMEOUT.value,
            "outcome_class": EvaluationOutcomeClass.SCORED,
            "failure_class": EvaluationFailureClass.NONE,
            "final_workspace_digest": SHA,
            "final_diff_digest": SHA,
            "final_verification_execution_id": uuid4(),
        }
    )

    with pytest.raises(ValidationError, match="failure category"):
        RepairEvaluationRun.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("outcome_class", EvaluationOutcomeClass.INFRASTRUCTURE_INVALID),
        ("failure_class", EvaluationFailureClass.INFRASTRUCTURE),
        ("infrastructure_failure", True),
    ],
)
def test_evaluation_run_rejects_outcome_fields_that_conflict_with_classification(
    field: str,
    value: object,
) -> None:
    data = _evaluation_run_data()
    data[field] = value

    with pytest.raises(ValidationError, match="evaluation outcome"):
        RepairEvaluationRun.model_validate(data)


def test_evaluation_run_rejects_indeterminate_as_infrastructure() -> None:
    data = _evaluation_run_data()
    data.update(
        {
            "final_status": RepairCompletionStatus.INDETERMINATE,
            "failure_category": RepairTerminationReason.INDETERMINATE_SIDE_EFFECT.value,
            "outcome_class": EvaluationOutcomeClass.INDETERMINATE,
            "failure_class": EvaluationFailureClass.SAFETY_INDETERMINATE,
            "infrastructure_failure": True,
        }
    )

    with pytest.raises(ValidationError, match="evaluation outcome"):
        RepairEvaluationRun.model_validate(data)
