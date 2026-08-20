import hashlib
import json
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue

from agentforge.domain.enums import MutationExecutionStatus, ToolErrorCode
from agentforge.domain.mutations import MutationExecutionRecord
from agentforge.domain.repair import (
    BudgetConsumptionDecision,
    BudgetKind,
    CompletionAction,
    RepairCompletionStatus,
    RepairState,
    RepairTerminationReason,
)
from agentforge.domain.test_execution import ProcessExecutionRecord
from agentforge.evaluation.validators import WorkspaceDiffValidator
from agentforge.evaluation.workspace import WorkspaceBaseline
from agentforge.models.domain import ModelErrorCode
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import RunLeaseAuthority
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.runtime.snapshots import RepairSnapshotState

_MODEL_FAILURE_TERMINALS: dict[
    ModelErrorCode,
    tuple[RepairCompletionStatus, RepairTerminationReason],
] = {
    ModelErrorCode.MODEL_AUTH_ERROR: (
        RepairCompletionStatus.RUNTIME_FAILURE,
        RepairTerminationReason.MODEL_AUTH_ERROR,
    ),
    ModelErrorCode.MODEL_RATE_LIMITED: (
        RepairCompletionStatus.RUNTIME_FAILURE,
        RepairTerminationReason.MODEL_RATE_LIMITED,
    ),
    ModelErrorCode.MODEL_TIMEOUT: (
        RepairCompletionStatus.RUNTIME_FAILURE,
        RepairTerminationReason.MODEL_TIMEOUT,
    ),
    ModelErrorCode.MODEL_TRANSPORT_ERROR: (
        RepairCompletionStatus.RUNTIME_FAILURE,
        RepairTerminationReason.MODEL_TRANSPORT_ERROR,
    ),
    ModelErrorCode.MODEL_BAD_REQUEST: (
        RepairCompletionStatus.RUNTIME_FAILURE,
        RepairTerminationReason.MODEL_BAD_REQUEST,
    ),
    ModelErrorCode.MODEL_PROVIDER_ERROR: (
        RepairCompletionStatus.RUNTIME_FAILURE,
        RepairTerminationReason.MODEL_PROVIDER_ERROR,
    ),
    ModelErrorCode.MODEL_PROTOCOL_ERROR: (
        RepairCompletionStatus.MODEL_PROTOCOL_ERROR,
        RepairTerminationReason.MODEL_PROTOCOL_ERROR,
    ),
    ModelErrorCode.MODEL_OUTPUT_INVALID: (
        RepairCompletionStatus.MODEL_PROTOCOL_ERROR,
        RepairTerminationReason.MODEL_PROTOCOL_ERROR,
    ),
    ModelErrorCode.MODEL_BUDGET_EXCEEDED: (
        RepairCompletionStatus.BUDGET_EXHAUSTED,
        RepairTerminationReason.MODEL_CALL_LIMIT,
    ),
}


class CompletionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pending_approval: bool = False
    pending_side_effect: bool = False
    indeterminate_side_effect: bool = False


class CompletionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: CompletionAction
    state: RepairState
    feedback: str | None = None


class WorkspaceBaselineLoader(Protocol):
    def get_baseline(self, baseline_id: UUID) -> WorkspaceBaseline: ...


class RepairCoordinator:
    def __init__(
        self,
        workflow: RepairWorkflow,
        *,
        workspace_repository: WorkspaceBaselineLoader | None = None,
        diff_validator: WorkspaceDiffValidator | None = None,
    ) -> None:
        self._workflow = workflow
        self._workspace_repository = workspace_repository
        self._diff_validator = diff_validator

    @property
    def workflow(self) -> RepairWorkflow:
        return self._workflow

    @property
    def database(self) -> Database:
        """The immutable transaction domain inherited from the workflow."""

        return self._workflow.database

    def state(self, run_id: object) -> RepairState:
        from uuid import UUID

        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID")
        return self._workflow.get_state(run_id)

    def final_profile_id(self, run_id: object) -> str:
        from uuid import UUID

        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID")
        return self._workflow.get_policy(run_id).final_verification_profile_id

    def is_final_profile(self, run_id: object, profile_id: str) -> bool:
        return profile_id == self.final_profile_id(run_id)

    def observe_mutation(
        self,
        record: MutationExecutionRecord,
        *,
        authority: RunLeaseAuthority,
    ) -> RepairState:
        if record.status not in {
            MutationExecutionStatus.PREPARED,
            MutationExecutionStatus.WRITING,
            MutationExecutionStatus.COMMITTED,
            MutationExecutionStatus.FAILED,
            MutationExecutionStatus.INDETERMINATE,
        }:
            raise ValueError("Unsupported mutation state")
        return self._workflow.observe_mutation(record, authority=authority)

    def consume_model_call(
        self,
        run_id: object,
        *,
        fact_id: str,
        authority: RunLeaseAuthority,
    ) -> BudgetConsumptionDecision:
        from uuid import UUID

        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID")
        return self._workflow.consume_budget(
            run_id, BudgetKind.MODEL, fact_id, authority=authority
        )

    def terminalize_model_failure(
        self,
        run_id: object,
        *,
        error_code: ModelErrorCode,
        authority: RunLeaseAuthority,
    ) -> RepairState:
        from uuid import UUID

        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID")
        status, reason = _MODEL_FAILURE_TERMINALS.get(
            error_code,
            (
                RepairCompletionStatus.RUNTIME_FAILURE,
                RepairTerminationReason.RUNTIME_FAILURE,
            ),
        )
        for _ in range(3):
            state = self._workflow.get_state(run_id)
            if state.terminal:
                return state
            try:
                return self._workflow.transition_terminal(
                    run_id,
                    expected_version=state.state_version,
                    status=status,
                    reason=reason,
                    authority=authority,
                )
            except RuntimeError as exc:
                if str(exc) != "Repair state version conflict":
                    raise
        state = self._workflow.get_state(run_id)
        if state.terminal:
            return state
        raise RuntimeError("Repair state changed during model failure handling")

    def terminalize_tool_failure(
        self, run_id: object, *, authority: RunLeaseAuthority
    ) -> RepairState:
        from uuid import UUID

        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID")
        for _ in range(3):
            state = self._workflow.get_state(run_id)
            if state.terminal:
                return state
            try:
                return self._workflow.transition_terminal(
                    run_id,
                    expected_version=state.state_version,
                    status=RepairCompletionStatus.MODEL_TOOL_FAILED,
                    reason=RepairTerminationReason.MODEL_TOOL_FAILED,
                    authority=authority,
                )
            except RuntimeError as exc:
                if str(exc) != "Repair state version conflict":
                    raise
        state = self._workflow.get_state(run_id)
        if state.terminal:
            return state
        raise RuntimeError("Repair state changed during tool failure handling")

    def terminalize_runtime_failure(
        self, run_id: object, *, authority: RunLeaseAuthority
    ) -> RepairState:
        from uuid import UUID

        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID")
        return self._workflow.terminalize_runtime_failure(
            run_id, authority=authority
        )

    def record_model_tool_failure(
        self,
        run_id: object,
        *,
        step_number: int,
        tool_name: str,
        error_type: ToolErrorCode,
        authority: RunLeaseAuthority,
    ) -> RepairState:
        """Count a recoverable model-authored tool mistake without persisting details."""

        from uuid import UUID

        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID")
        if step_number <= 0:
            raise ValueError("step_number must be positive")
        payload = json.dumps(
            {
                "error_type": error_type.value,
                "run_id": str(run_id),
                "step_number": step_number,
                "tool_name": tool_name,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        fact_id = f"model-tool:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"
        return self._workflow.record_policy_violation(
            run_id,
            fact_id=fact_id,
            rule=f"model_tool_{error_type.value.casefold()}",
            severe=False,
            authority=authority,
        )

    def runtime_context(self, run_id: object) -> dict[str, JsonValue]:
        from uuid import UUID

        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID")
        policy = self._workflow.get_policy(run_id)
        state = self._workflow.get_state(run_id)
        return {
            "remaining_model_calls": max(
                0, policy.max_model_calls - state.model_calls_used
            ),
            "remaining_read_calls": max(
                0, policy.max_read_calls - state.read_calls_used
            ),
            "remaining_edit_attempts": max(
                0, policy.max_edit_attempts - state.edit_attempts_used
            ),
            "remaining_test_runs": max(
                0, policy.max_test_runs - state.test_runs_used
            ),
            "remaining_completion_corrections": max(
                0,
                policy.max_completion_corrections
                - state.completion_corrections_used,
            ),
            "latest_development_test_success": state.last_development_test_success,
            "latest_source_verified": state.latest_source_verified,
            "pending_final_verification": state.pending_final_verification,
            "repair_status": state.status.value,
        }

    def snapshot(self, run_id: object) -> RepairSnapshotState:
        from uuid import UUID

        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID")
        state = self._workflow.get_state(run_id)
        return RepairSnapshotState(
            task_id=state.task_id,
            policy_digest=state.policy_digest,
            status=state.status,
            state_version=state.state_version,
            model_calls_used=state.model_calls_used,
            read_calls_used=state.read_calls_used,
            edit_attempts_used=state.edit_attempts_used,
            test_runs_used=state.test_runs_used,
            completion_corrections_used=state.completion_corrections_used,
            policy_violations=state.policy_violations,
            baseline_id=state.baseline_id,
            baseline_digest=state.baseline_digest,
            last_mutation_execution_id=state.last_mutation_execution_id,
            last_development_test_execution_id=state.last_development_test_execution_id,
            latest_source_verified=state.latest_source_verified,
            pending_final_verification=state.pending_final_verification,
            final_verification_execution_id=state.final_verification_execution_id,
            final_workspace_digest=state.final_workspace_digest,
            final_diff_digest=state.final_diff_digest,
        )

    def observe_test(
        self,
        record: ProcessExecutionRecord,
        *,
        authority: RunLeaseAuthority,
    ) -> RepairState:
        policy = self._workflow.get_policy(record.run_id)
        if record.profile_id == policy.final_verification_profile_id:
            return self._workflow.observe_test(
                record, final_verification=True, authority=authority
            )
        if not policy.allows_development_profile(record.profile_id):
            raise ValueError("Test profile is not allowed by RepairTaskPolicy")
        return self._workflow.observe_test(
            record, final_verification=False, authority=authority
        )

    def evaluate_completion(
        self,
        run_id: object,
        *,
        answer_digest: str,
        context: CompletionContext,
        max_completion_corrections: int | None = None,
        authority: RunLeaseAuthority,
    ) -> CompletionDecision:
        from uuid import UUID

        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID")
        state = self._workflow.get_state(run_id)
        if state.terminal:
            return CompletionDecision(action=CompletionAction.TERMINAL, state=state)
        if context.indeterminate_side_effect:
            terminal = self._workflow.transition_terminal(
                run_id,
                expected_version=state.state_version,
                status=RepairCompletionStatus.INDETERMINATE,
                reason=RepairTerminationReason.INDETERMINATE_SIDE_EFFECT,
                authority=authority,
            )
            return CompletionDecision(action=CompletionAction.TERMINAL, state=terminal)

        feedback = self._completion_feedback(state, context)
        if feedback is not None:
            policy = self._workflow.get_policy(run_id)
            maximum = (
                policy.max_completion_corrections
                if max_completion_corrections is None
                else min(max_completion_corrections, policy.max_completion_corrections)
            )
            correction = self._workflow.record_completion_correction(
                run_id,
                answer_digest=answer_digest,
                feedback=feedback,
                maximum=maximum,
                authority=authority,
            )
            return CompletionDecision(
                action=(
                    CompletionAction.TERMINAL
                    if correction.state.terminal
                    else CompletionAction.CORRECT
                ),
                state=correction.state,
                feedback=None if correction.state.terminal else feedback,
            )

        if self._workspace_repository is not None or self._diff_validator is not None:
            if self._workspace_repository is None or self._diff_validator is None:
                raise RuntimeError("Repair diff validation configuration is incomplete")
            baseline = self._workspace_repository.get_baseline(state.baseline_id)
            if baseline.root_digest != state.baseline_digest:
                raise RuntimeError("Repair baseline digest does not match persisted baseline")
            policy = self._workflow.get_policy(run_id)
            result = self._diff_validator.validate(baseline, policy)
            state = self._workflow.record_diff_validation(
                run_id, result, authority=authority, persist_result=True
            )
            if state.terminal:
                return CompletionDecision(action=CompletionAction.TERMINAL, state=state)
        pending = self._workflow.mark_pending_final_verification(
            run_id, state.state_version, authority=authority
        )
        return CompletionDecision(
            action=CompletionAction.REQUEST_FINAL_VERIFICATION,
            state=pending,
        )

    @staticmethod
    def _completion_feedback(state: RepairState, context: CompletionContext) -> str | None:
        if context.pending_approval:
            return (
                "The repair cannot be marked complete because an approval decision "
                "is still pending."
            )
        if context.pending_side_effect:
            return (
                "The repair cannot be marked complete because a pending side effect "
                "has not reached a durable terminal state."
            )
        if state.last_development_test_success is not True:
            return (
                "The repair cannot be marked complete because no allowed development "
                "test has completed successfully for the latest source state."
            )
        if not state.latest_source_verified:
            return (
                "The repair cannot be marked complete because the latest source mutation "
                "has not been followed by a successful allowed development test."
            )
        return None
