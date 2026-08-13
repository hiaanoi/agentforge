from pydantic import BaseModel, ConfigDict

from agentforge.domain.enums import (
    EvaluationFailureClass,
    EvaluationOutcomeClass,
)
from agentforge.domain.repair import (
    RepairCompletionStatus,
    RepairTerminationReason,
)


class EvaluationOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome_class: EvaluationOutcomeClass
    failure_class: EvaluationFailureClass
    infrastructure_failure: bool
    replaceable: bool
    abort_study: bool


_REPLACEABLE_INFRASTRUCTURE = {
    RepairTerminationReason.MODEL_RATE_LIMITED,
    RepairTerminationReason.MODEL_TIMEOUT,
    RepairTerminationReason.MODEL_TRANSPORT_ERROR,
    RepairTerminationReason.MODEL_PROVIDER_ERROR,
}

_CONFIGURATION_FAILURES = {
    RepairTerminationReason.MODEL_AUTH_ERROR,
    RepairTerminationReason.MODEL_BAD_REQUEST,
}


def classify_evaluation_outcome(
    *,
    status: RepairCompletionStatus,
    failure_reason: RepairTerminationReason | None,
) -> EvaluationOutcome:
    if status is RepairCompletionStatus.VERIFIED_SUCCESS:
        return EvaluationOutcome(
            outcome_class=EvaluationOutcomeClass.SCORED,
            failure_class=EvaluationFailureClass.NONE,
            infrastructure_failure=False,
            replaceable=False,
            abort_study=False,
        )
    if status is RepairCompletionStatus.INDETERMINATE:
        return EvaluationOutcome(
            outcome_class=EvaluationOutcomeClass.INDETERMINATE,
            failure_class=EvaluationFailureClass.SAFETY_INDETERMINATE,
            infrastructure_failure=False,
            replaceable=False,
            abort_study=True,
        )
    if failure_reason in _CONFIGURATION_FAILURES:
        return EvaluationOutcome(
            outcome_class=EvaluationOutcomeClass.INFRASTRUCTURE_INVALID,
            failure_class=EvaluationFailureClass.CONFIGURATION,
            infrastructure_failure=True,
            replaceable=False,
            abort_study=True,
        )
    if failure_reason in _REPLACEABLE_INFRASTRUCTURE:
        return EvaluationOutcome(
            outcome_class=EvaluationOutcomeClass.INFRASTRUCTURE_INVALID,
            failure_class=EvaluationFailureClass.INFRASTRUCTURE,
            infrastructure_failure=True,
            replaceable=True,
            abort_study=False,
        )
    if status is RepairCompletionStatus.RUNTIME_FAILURE:
        return EvaluationOutcome(
            outcome_class=EvaluationOutcomeClass.INFRASTRUCTURE_INVALID,
            failure_class=EvaluationFailureClass.INFRASTRUCTURE,
            infrastructure_failure=True,
            replaceable=False,
            abort_study=False,
        )
    return EvaluationOutcome(
        outcome_class=EvaluationOutcomeClass.SCORED,
        failure_class=EvaluationFailureClass.MODEL_QUALITY,
        infrastructure_failure=False,
        replaceable=False,
        abort_study=False,
    )
