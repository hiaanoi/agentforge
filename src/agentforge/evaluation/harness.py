from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agentforge.domain.enums import EventType, RunStatus
from agentforge.domain.models import normalize_utc
from agentforge.domain.repair import (
    BudgetProfile,
    CompletionCorrectionMode,
    RepairCompletionStatus,
    RepairTerminationReason,
)
from agentforge.evaluation.auto_approval import (
    AutoApprovalHarness,
    EvaluationWorkspaceHandle,
)
from agentforge.evaluation.baseline import BaselineExecutionCoordinator
from agentforge.evaluation.baseline_models import BaselineExecutionStatus
from agentforge.evaluation.models import RepairEvaluationRun
from agentforge.evaluation.outcomes import classify_evaluation_outcome
from agentforge.evaluation.persistence import EvaluationRunRepository
from agentforge.evaluation.telemetry import EvaluationTelemetryCollector
from agentforge.evaluation.telemetry_persistence import (
    EvaluationTelemetryRepository,
)
from agentforge.persistence.legacy_evaluator import LegacyEvaluatorEventRepository
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.runtime.engine import AgentRuntime


class EvaluationRunMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    campaign_id: UUID
    slot_id: UUID
    attempt_id: UUID
    attempt_number: int = Field(gt=0)
    replacement_for_evaluation_run_id: UUID | None = None
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


class EvaluationHarness:
    def __init__(
        self,
        runtime: AgentRuntime,
        repairs: RepairWorkflow,
        events: LegacyEvaluatorEventRepository,
        workspace: EvaluationWorkspaceHandle,
        evaluation_runs: EvaluationRunRepository,
        *,
        baseline_coordinator: BaselineExecutionCoordinator | None = None,
        telemetry_collector: EvaluationTelemetryCollector | None = None,
        telemetry_repository: EvaluationTelemetryRepository | None = None,
    ) -> None:
        self._runtime = runtime
        self._repairs = repairs
        self._events = events
        self._workspace = workspace
        self._evaluation_runs = evaluation_runs
        self._baseline_coordinator = baseline_coordinator
        if (telemetry_collector is None) is not (telemetry_repository is None):
            raise ValueError(
                "Evaluation telemetry collector and repository must be configured together"
            )
        self._telemetry_collector = telemetry_collector
        self._telemetry_repository = telemetry_repository

    @property
    def baseline_coordinator(self) -> BaselineExecutionCoordinator:
        if self._baseline_coordinator is None:
            raise RuntimeError("Evaluation baseline support is not configured")
        return self._baseline_coordinator

    async def execute(
        self,
        run_id: UUID,
        metadata: EvaluationRunMetadata,
    ) -> RepairEvaluationRun:
        state = self._repairs.get_state(run_id)
        if (
            metadata.task_id != state.task_id
            or metadata.task_policy_digest != state.policy_digest
            or metadata.initial_workspace_digest != state.baseline_digest
        ):
            raise ValueError("Evaluation metadata does not match persisted repair state")
        self._events.append(
            run_id,
            EventType.EVALUATION_RUN_STARTED,
            {
                "task_id": metadata.task_id,
                "repetition_index": metadata.repetition_index,
                "model_id": metadata.model_id,
                "task_policy_digest": metadata.task_policy_digest,
            },
        )
        initial_context_items = None
        run = None
        baseline_execution_id = None
        if self._baseline_coordinator is not None:
            baseline = await self._baseline_coordinator.execute(run_id)
            baseline_execution_id = baseline.baseline_execution_id
            if baseline.status is not BaselineExecutionStatus.VERIFIED_EXPECTED_FAILURE:
                if baseline.failure_reason is None:
                    raise RuntimeError("Unverified baseline is missing a failure reason")
                terminal_status = (
                    RepairCompletionStatus.INDETERMINATE
                    if baseline.status is BaselineExecutionStatus.INDETERMINATE
                    else RepairCompletionStatus.RUNTIME_FAILURE
                )
                terminal_reason = (
                    RepairTerminationReason.INDETERMINATE_SIDE_EFFECT
                    if terminal_status is RepairCompletionStatus.INDETERMINATE
                    else RepairTerminationReason.RUNTIME_FAILURE
                )
                self._repairs._evaluator_only_transition_terminal(
                    run_id,
                    expected_version=state.state_version,
                    status=terminal_status,
                    reason=terminal_reason,
                )
                run = self._runtime._evaluator_only_fail_created(
                    run_id,
                    f"Evaluation baseline blocked: {baseline.failure_reason.value}",
                )
            else:
                if (
                    baseline.task_id != metadata.task_id
                    or baseline.workspace_baseline_id != state.baseline_id
                    or baseline.initial_workspace_digest != metadata.initial_workspace_digest
                ):
                    raise RuntimeError("Evaluation baseline binding does not match repair state")
                initial_context_items = [self._baseline_coordinator.context_item(run_id)]
                self._events.append(
                    run_id,
                    EventType.EVALUATION_BASELINE_CONTEXT_INJECTED,
                    {
                        "baseline_execution_id": str(baseline.baseline_execution_id),
                        "safe_failure_summary_digest": baseline.safe_failure_summary_digest,
                    },
                )
        if run is None:
            run = await self._runtime.execute(
                run_id,
                initial_context_items=initial_context_items,
            )
        if run.status is RunStatus.WAITING_APPROVAL:
            run = await AutoApprovalHarness(
                self._runtime,
                self._events,
                self._workspace,
                self._repairs,
            ).process(run_id)
        state = self._repairs.get_state(run_id)
        outcome = classify_evaluation_outcome(
            status=state.status,
            failure_reason=state.failure_reason,
        )
        record = RepairEvaluationRun(
            **metadata.model_dump(),
            run_id=run_id,
            baseline_execution_id=baseline_execution_id,
            final_status=state.status,
            verified_success=state.status is RepairCompletionStatus.VERIFIED_SUCCESS,
            model_calls=state.model_calls_used,
            read_calls=state.read_calls_used,
            edit_attempts=state.edit_attempts_used,
            test_runs=state.test_runs_used,
            completion_corrections=state.completion_corrections_used,
            policy_violations=state.policy_violations,
            wall_time_ms=max(
                0,
                int(
                    (
                        normalize_utc(state.updated_at) - normalize_utc(state.started_at)
                    ).total_seconds()
                    * 1000
                ),
            ),
            token_usage=run.total_token_usage,
            final_workspace_digest=state.final_workspace_digest,
            final_diff_digest=state.final_diff_digest,
            development_test_execution_id=state.last_development_test_execution_id,
            final_verification_execution_id=state.final_verification_execution_id,
            failure_category=(
                state.failure_reason.value
                if state.failure_reason is not None
                else (
                    None
                    if state.status is RepairCompletionStatus.VERIFIED_SUCCESS
                    else state.status.value
                )
            ),
            outcome_class=outcome.outcome_class,
            failure_class=outcome.failure_class,
            infrastructure_failure=outcome.infrastructure_failure,
            created_at=state.started_at,
            completed_at=state.updated_at,
        )
        record = self._evaluation_runs.save(record)
        if (
            self._telemetry_collector is not None
            and self._telemetry_repository is not None
        ):
            self._telemetry_repository.save(
                self._telemetry_collector.collect(record)
            )
        self._events.append(
            run_id,
            EventType.EVALUATION_RUN_COMPLETED,
            {
                "evaluation_run_id": str(record.evaluation_run_id),
                "final_status": record.final_status.value,
                "verified_success": record.verified_success,
                "outcome_class": record.outcome_class.value,
                "failure_class": record.failure_class.value,
                "infrastructure_failure": record.infrastructure_failure,
            },
        )
        return record
