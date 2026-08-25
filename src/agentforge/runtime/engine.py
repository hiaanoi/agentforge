import hashlib
import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4

from pydantic import JsonValue, ValidationError

from agentforge.application.contracts import OutcomeStatus, ProfilePurpose, ReceiptStatus
from agentforge.application.kernel_errors import StaleFenceError
from agentforge.application.run_commands import CancelRun, ResumeRecoveryChoice, ResumeRun
from agentforge.application.run_driver import DriverOutcome, RunDriver, RunOwnership
from agentforge.context.builder import ContextBuilder
from agentforge.context.loop import LoopDetector
from agentforge.context.models import (
    ContextItem,
    ContextItemKind,
    LoopState,
    ResumeContextState,
)
from agentforge.context.renderers import ToolResultRenderer
from agentforge.domain.digests import compute_tool_call_digest
from agentforge.domain.enums import (
    ApprovalConsumptionState,
    ApprovalStatus,
    EventType,
    ProcessExecutionStatus,
    RejectionStrategy,
    ResumePhase,
    RunFailureCode,
    RunStatus,
    ToolErrorCode,
)
from agentforge.domain.errors import (
    CheckpointNotFoundError,
    ModelOutputError,
    ModelProviderError,
    ResumeNotAllowedError,
)
from agentforge.domain.models import (
    ApprovalAuthorization,
    ApprovalRequest,
    ApprovalRequired,
    Checkpoint,
    PendingToolCall,
    Run,
    RunBudget,
    ToolResult,
)
from agentforge.domain.mutations import (
    MutationApprovalBinding,
    MutationApprovalRequired,
    MutationExecutionRecord,
)
from agentforge.domain.repair import CompletionAction, RepairCompletionStatus
from agentforge.domain.test_execution import (
    PendingTestExecution,
    ProcessExecutionRecord,
    TestApprovalBinding,
    TestApprovalRequired,
    TestProfileSummary,
    TestResult,
)
from agentforge.models.base import (
    FinalAnswer,
    ModelProvider,
    ModelRequest,
    ToolCall,
    parse_model_output,
)
from agentforge.models.domain import (
    ModelBudget,
    ModelErrorCode,
    ModelResponse,
    ModelUsage,
    MultiToolResponseInfo,
)
from agentforge.models.errors import ModelRequestError
from agentforge.models.executor import ModelExecutor
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.event_log import EventLog, RunLeaseAuthority
from agentforge.persistence.model_workflow import ModelWorkflow
from agentforge.persistence.repositories import (
    ApprovalRepository,
    CheckpointRepository,
    EventRepository,
    RunRepository,
)
from agentforge.persistence.resume_workflow import (
    ResumePreparation,
    ResumeRunWorkflow,
)
from agentforge.persistence.run_control import RunControlRequest, RunControlWorkflow
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.repair_engines.mini_linear import (
    CandidateShell,
    MiniLinearRepairEngine,
    SubprocessCandidateShell,
)
from agentforge.repair_engines.models import RepairEngineKind
from agentforge.runtime.candidate_patch import CandidatePatchPublisher, CandidatePatchStore
from agentforge.runtime.candidate_workspace import CandidateWorkspace
from agentforge.runtime.mutations import (
    MutationCoordinator,
    MutationOutcomeIndeterminateError,
)
from agentforge.runtime.repair import CompletionContext, RepairCoordinator
from agentforge.runtime.snapshots import RuntimeSnapshotV4, load_runtime_snapshot
from agentforge.runtime.test_execution import (
    TestExecutionCancelledError,
    TestExecutionCoordinator,
    TestExecutionOutcomeIndeterminateError,
    TestExecutionPreflightFailedError,
)
from agentforge.tools.executor import ToolExecutor

_RECOVERABLE_MODEL_TOOL_ERRORS = frozenset(
    {
        ToolErrorCode.TOOL_NOT_FOUND,
        ToolErrorCode.INVALID_ARGUMENTS,
        ToolErrorCode.INVALID_PATH,
        ToolErrorCode.PATH_NOT_FOUND,
        ToolErrorCode.PATH_TYPE_MISMATCH,
        ToolErrorCode.BINARY_FILE,
        ToolErrorCode.ENCODING_ERROR,
        ToolErrorCode.FILE_TOO_LARGE,
    }
)


class _MiniLinearExecutorProvider:
    def __init__(
        self,
        executor: ModelExecutor,
        run: Run,
        ownership: RunOwnership,
    ) -> None:
        self._executor = executor
        self._run = run
        self._ownership = ownership

    @property
    def name(self) -> str:
        return self._executor.provider.name

    @property
    def journal_identity(self) -> str:
        return self._executor.provider.journal_identity

    async def generate(self, request: ModelRequest) -> ModelResponse:
        return await self._executor.generate(self._run, request, ownership=self._ownership)


class AgentRuntime:
    def __init__(
        self,
        run_repository: RunRepository,
        event_repository: EventRepository,
        checkpoint_repository: CheckpointRepository,
        model_provider: ModelProvider,
        tool_executor: ToolExecutor,
        approval_repository: ApprovalRepository | None = None,
        approval_workflow: ApprovalWorkflow | None = None,
        model_executor: ModelExecutor | None = None,
        model_workflow: ModelWorkflow | None = None,
        model_budget: ModelBudget | None = None,
        context_builder: ContextBuilder | None = None,
        result_renderer: ToolResultRenderer | None = None,
        loop_detector: LoopDetector | None = None,
        mutation_coordinator: MutationCoordinator | None = None,
        test_execution_coordinator: TestExecutionCoordinator | None = None,
        repair_coordinator: RepairCoordinator | None = None,
        run_lease_store: RunLeaseStore | None = None,
        lease_ttl: timedelta = timedelta(seconds=30),
        heartbeat_interval: timedelta = timedelta(seconds=5),
        owner_id: str | None = None,
        repair_engine: RepairEngineKind = RepairEngineKind.NATIVE,
        workspace: Path | None = None,
        candidate_shell_factory: Callable[[Path], CandidateShell] | None = None,
        candidate_publisher: CandidatePatchPublisher | None = None,
        candidate_store: CandidatePatchStore | None = None,
    ) -> None:
        self._runs = run_repository
        self._events = event_repository
        self._checkpoints = checkpoint_repository
        self._model = model_provider
        self._tools = tool_executor
        self._approvals = approval_repository
        self._approval_workflow = approval_workflow
        self._model_executor = model_executor
        self._model_workflow = model_workflow
        self._model_budget = model_budget or ModelBudget()
        self._context_builder = context_builder or ContextBuilder()
        self._result_renderer = result_renderer or ToolResultRenderer()
        self._loop_detector = loop_detector or LoopDetector()
        self._mutations = mutation_coordinator
        self._test_executions = test_execution_coordinator
        self._repairs = repair_coordinator
        self._active_runs: set[UUID] = set()
        self._evaluator_only_runs: set[UUID] = set()
        self._run_leases = run_lease_store or RunLeaseStore(run_repository.database)
        self._lease_ttl = lease_ttl
        self._heartbeat_interval = heartbeat_interval
        self._owner_id = owner_id or f"runtime:{uuid4()}"
        self._repair_engine = RepairEngineKind(repair_engine)
        self._workspace = workspace.resolve(strict=True) if workspace is not None else None
        self._candidate_shell_factory = candidate_shell_factory or SubprocessCandidateShell
        self._candidate_publisher = candidate_publisher
        self._candidate_store = candidate_store

    def create_run(
        self,
        task: str,
        max_steps: int = 10,
        max_tool_calls: int = 10,
        budget: RunBudget | None = None,
    ) -> Run:
        """Legacy Runtime-only creation seam.

        Product callers use ``RunCreationWorkflow`` so Receipt, bindings, initial
        state and the first Event share one transaction.  This adapter remains for
        evaluator/runtime compatibility until the A2 application boundary lands.
        """
        return self._create_run_legacy(
            task,
            max_steps=max_steps,
            max_tool_calls=max_tool_calls,
            budget=budget,
        )

    def _create_run_legacy(
        self,
        task: str,
        *,
        max_steps: int,
        max_tool_calls: int,
        budget: RunBudget | None,
    ) -> Run:
        if budget is not None:
            max_steps = budget.max_steps
            max_tool_calls = budget.max_tool_calls
        run = self._runs.create(
            Run(
                task=task,
                max_steps=max_steps,
                max_tool_calls=max_tool_calls,
                model_provider=self._model.name,
            )
        )
        self._events.append_created(
            run.run_id,
            {"task_digest": hashlib.sha256(task.encode("utf-8")).hexdigest()},
        )
        if self._model_workflow is not None:
            self._model_workflow._evaluator_only_ensure_state(
                run.run_id, self._model_budget
            )
        self._evaluator_only_runs.add(run.run_id)
        return run

    async def execute(
        self,
        run_id: UUID,
        *,
        initial_context_items: list[ContextItem] | None = None,
        authority: RunLeaseAuthority | None = None,
    ) -> Run:
        driver = (
            self._driver(run_id)
            if authority is None
            else RunDriver(
                self._run_leases,
                run_id=run_id,
                owner_id=authority.owner_id,
                ttl=self._lease_ttl,
                heartbeat_interval=self._heartbeat_interval,
            )
        )

        async def owned(ownership: RunOwnership) -> Run:
            return await self._execute_owned(
                run_id,
                ownership=ownership,
                initial_context_items=initial_context_items,
            )

        if authority is None:
            return await driver.run(owned)
        outcome = await driver.run_outcome(owned, authority=authority)
        if outcome.value is None:
            raise StaleFenceError()
        return outcome.value

    async def _execute_owned(
        self,
        run_id: UUID,
        *,
        ownership: RunOwnership,
        initial_context_items: list[ContextItem] | None = None,
    ) -> Run:
        run = self._runs.get(run_id)
        if run.status is not RunStatus.CREATED:
            raise ResumeNotAllowedError("Only a CREATED Run can execute from the beginning")
        if self._repair_engine is RepairEngineKind.MINI_NATIVE:
            raise RuntimeError(
                "Mini native runtime is registered but not wired yet"
            )
        run.transition_to(RunStatus.RUNNING)
        authority = self._authority(ownership, run.run_id)
        self._runs.save(run, authority=authority)
        self._append_event(ownership, run.run_id, EventType.RUN_STARTED)
        if self._repair_engine is RepairEngineKind.MINI_LINEAR:
            return await self._run_mini_linear(ownership, run)
        return await self._run_loop(
            ownership,
            run,
            [],
            context_items=(
                [item.model_copy(deep=True) for item in initial_context_items]
                if initial_context_items is not None
                else None
            ),
        )

    async def _run_mini_linear(self, ownership: RunOwnership, run: Run) -> Run:
        if (
            self._workspace is None
            or self._candidate_publisher is None
            or self._candidate_store is None
        ):
            raise RuntimeError("Mini linear runtime is missing candidate workspace components")
        candidate = CandidateWorkspace.create(self._workspace, run_id=run.run_id)
        model = (
            _MiniLinearExecutorProvider(self._model_executor, run, ownership)
            if self._model_executor is not None
            else self._model
        )
        engine = MiniLinearRepairEngine(model, self._candidate_shell_factory(candidate.root))
        result = await engine.run(
            run_id=run.run_id,
            task=run.task,
            max_steps=run.max_steps,
        )
        run.current_step = result.model_calls
        self._runs.save(run, authority=self._authority(ownership, run.run_id))
        patch = self._candidate_publisher.capture(candidate.root)
        if not result.submitted and not patch.entries:
            return self._fail(
                ownership,
                run,
                f"Maximum step count of {run.max_steps} exhausted before candidate submission",
            )
        self._candidate_store.save(str(run.run_id), patch)
        history = json.loads(json.dumps(result.history))
        context = self._context_builder.build(
            task=run.task,
            items=[],
            step_number=run.current_step,
        )
        final_call = ToolCall(
            type="tool_call",
            call_id=f"candidate-patch-{run.run_id}",
            tool="publish_candidate_patch",
            arguments={"run_id": str(run.run_id)},
            reason="Publish the submitted candidate patch after final approval",
        )
        return self._pause_for_approval(
            ownership,
            run,
            final_call,
            ApprovalRequired(
                tool_name=final_call.tool,
                validated_arguments=final_call.arguments,
                sanitized_arguments=final_call.arguments,
            ),
            history,
            [],
            LoopState(),
            context.state,
            None,
            None,
            None,
        )

    def _evaluator_only_fail_created(self, run_id: UUID, reason: str) -> Run:
        lease = self._run_leases.acquire(
            run_id,
            owner_id=f"legacy-evaluator:fail-created:{uuid4()}",
            ttl=self._lease_ttl,
        )
        try:
            ownership = RunOwnership(lambda: lease.authority)
            run = self._runs.get(run_id)
            if run.status is RunStatus.FAILED:
                return run
            if run.status is not RunStatus.CREATED:
                raise ResumeNotAllowedError(
                    "Only a CREATED Run can fail before execution"
                )
            return self._fail(ownership, run, reason)
        finally:
            self._run_leases.release(lease.authority)

    def list_pending_approvals(self, run_id: UUID | None = None) -> list[ApprovalRequest]:
        approvals, _ = self._require_approval_support()
        return approvals.list_pending(run_id)

    def approve(self, approval_id: UUID, note: str | None = None) -> ApprovalRequest:
        _, workflow = self._require_approval_support()
        approval = workflow._evaluator_only_resolve(
            approval_id,
            ApprovalStatus.APPROVED,
            RejectionStrategy.CONTINUE,
            note,
        )
        if (
            self._mutations is not None
            and self._mutations.binding_for_approval(approval_id) is not None
        ):
            self._mutations._evaluator_only_ensure_prepared(approval_id)
        if (
            self._test_executions is not None
            and self._test_executions.binding_for_approval(approval_id) is not None
        ):
            self._test_executions._evaluator_only_ensure_created(approval_id)
        return approval

    def reject(
        self,
        approval_id: UUID,
        strategy: RejectionStrategy = RejectionStrategy.CONTINUE,
        note: str | None = None,
    ) -> ApprovalRequest:
        _, workflow = self._require_approval_support()
        return workflow._evaluator_only_resolve(
            approval_id, ApprovalStatus.REJECTED, strategy, note
        )

    def cancel(
        self, command: CancelRun | UUID, reason: str | None = None
    ) -> RunControlRequest | Run:
        if isinstance(command, CancelRun):
            request = RunControlWorkflow(self._runs.database).request_cancel(command)
            if self._test_executions is not None:
                self._test_executions.cancel_active(command.run_id, command.reason)
            return request
        assert isinstance(command, UUID)
        return self.cancel_evaluator(command, reason)

    def cancel_evaluator(self, run_id: UUID, reason: str | None = None) -> Run:
        """Legacy evaluator-only immediate terminal cancellation seam."""
        _, workflow = self._require_approval_support()
        if self._test_executions is not None:
            self._test_executions.cancel_active(run_id, reason)
        lease = self._run_leases.acquire(
            run_id,
            owner_id=f"legacy-evaluator:cancel:{uuid4()}",
            ttl=self._lease_ttl,
        )
        try:
            return workflow.cancel(run_id, reason, authority=lease.authority)
        finally:
            self._run_leases.release(lease.authority)

    def get_mutation_execution(self, execution_id: UUID) -> MutationExecutionRecord:
        if self._mutations is None:
            raise ResumeNotAllowedError("Runtime was not configured for mutations")
        return self._mutations.get_execution(execution_id)

    def list_mutation_executions(self, run_id: UUID) -> list[MutationExecutionRecord]:
        if self._mutations is None:
            raise ResumeNotAllowedError("Runtime was not configured for mutations")
        return self._mutations.list_executions(run_id)

    def get_process_execution(self, execution_id: UUID) -> ProcessExecutionRecord:
        if self._test_executions is None:
            raise ResumeNotAllowedError("Runtime was not configured for test execution")
        return self._test_executions.get_execution(execution_id)

    def list_process_executions(self, run_id: UUID) -> list[ProcessExecutionRecord]:
        if self._test_executions is None:
            raise ResumeNotAllowedError("Runtime was not configured for test execution")
        return self._test_executions.list_executions(run_id)

    def list_test_profiles(self) -> list[TestProfileSummary]:
        if self._test_executions is None:
            raise ResumeNotAllowedError("Runtime was not configured for test execution")
        return self._test_executions.list_profiles()

    async def resume(self, command: ResumeRun | UUID) -> DriverOutcome[Run] | Run:
        if isinstance(command, ResumeRun):
            return await self._resume_command(command)
        assert isinstance(command, UUID)
        return await self.resume_evaluator(command)

    async def resume_evaluator(self, run_id: UUID) -> Run:
        """Legacy evaluator-only recovery seam without application Receipt semantics."""
        driver = self._driver(run_id)

        async def owned(ownership: RunOwnership) -> Run:
            return await self._resume_owned(run_id, ownership=ownership)

        return await driver.run(owned)

    async def _resume_command(self, command: ResumeRun) -> DriverOutcome[Run]:
        approvals, _ = self._require_approval_support()
        requests = approvals.list_for_run(command.run_id)
        approval = requests[-1] if requests else None
        workflow = ResumeRunWorkflow(
            self._runs.database,
            mutation_claimer=(
                self._mutations.claim_resume_in_session
                if self._mutations is not None
                else None
            ),
            test_execution_claimer=(
                self._test_executions.claim_resume_in_session
                if self._test_executions is not None
                else None
            ),
        )
        replay = workflow.finalize_observed_run(command) or workflow.replay(command)
        if replay is not None:
            replay_outcome = {
                ReceiptStatus.COMPLETED: OutcomeStatus.UNVERIFIED,
                ReceiptStatus.FAILED: OutcomeStatus.FAILED,
                ReceiptStatus.INDETERMINATE: OutcomeStatus.UNKNOWN,
            }[replay.status]
            return DriverOutcome(replay_outcome, self._runs.get(command.run_id))
        driver = self._driver(command.run_id)
        prepared = workflow.prepare(
            command,
            owner_id=driver.owner_id,
            ttl=self._lease_ttl,
        )
        if not prepared.owns_command:
            return DriverOutcome(OutcomeStatus.UNKNOWN, None, prepared.disposition.value)
        assert prepared.authority is not None
        prepared_authority = prepared.authority
        assert prepared.approval_id is not None
        assert approval is not None
        assert approval.approval_id == prepared.approval_id

        async def owned(ownership: RunOwnership) -> Run:
            try:
                result = await self._resume_owned(
                    command.run_id,
                    ownership=ownership,
                    prepared=prepared,
                )
            except BaseException:
                try:
                    workflow.terminalize(
                        command, prepared_authority, ReceiptStatus.FAILED
                    )
                except Exception:
                    workflow.mark_indeterminate(command)
                raise
            if (
                approvals.get(approval.approval_id).consumption_state
                is ApprovalConsumptionState.INDETERMINATE
            ):
                workflow.mark_indeterminate(command)
                return result
            workflow.terminalize(
                command, prepared_authority, ReceiptStatus.COMPLETED
            )
            return result

        outcome = await driver.run_outcome(owned, authority=prepared.authority)
        if outcome.outcome is OutcomeStatus.UNKNOWN:
            workflow.mark_indeterminate(command)
        replayed = workflow.replay(command)
        if replayed is not None and replayed.status is ReceiptStatus.INDETERMINATE:
            return DriverOutcome(OutcomeStatus.UNKNOWN, None)
        return outcome

    async def _resume_owned(
        self,
        run_id: UUID,
        *,
        ownership: RunOwnership,
        prepared: ResumePreparation | None = None,
    ) -> Run:
        approvals, workflow = self._require_approval_support()
        if run_id in self._active_runs:
            raise ResumeNotAllowedError("Run is already active in this Runtime")
        run = self._runs.get(run_id)
        if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            raise ResumeNotAllowedError(f"{run.status.value} Run cannot resume")
        requests = approvals.list_for_run(run_id)
        if not requests:
            return self._recovery_fail(
                ownership, run, "Approval recovery record is missing"
            )
        approval = requests[-1]
        mutation_binding = (
            self._mutations.binding_for_approval(approval.approval_id)
            if self._mutations is not None
            else None
        )
        test_binding = (
            self._test_executions.binding_for_approval(approval.approval_id)
            if self._test_executions is not None
            else None
        )
        recovered_result: ToolResult | None = None
        self._active_runs.add(run_id)
        try:
            if (
                prepared is not None
                and prepared.side_effect_claimed_now
                and prepared.phase
                in {
                ResumeRecoveryChoice.DECISION,
                ResumeRecoveryChoice.MUTATION,
                ResumeRecoveryChoice.TEST_EXECUTION,
                }
            ):
                snapshot = self._load_snapshot(approval, ResumePhase.AWAITING_APPROVAL)
                result = await self._consume_decision(
                    run_id,
                    approval,
                    snapshot,
                    ownership=ownership,
                    mutation_binding=mutation_binding,
                    test_binding=test_binding,
                    already_claimed=True,
                )
                if not result.success and approval.status is ApprovalStatus.APPROVED:
                    return self._fail(
                        ownership,
                        self._runs.get(run_id),
                        result.error_message or "Approved tool failed",
                    )
                if not workflow.claim_consumed_continuation(
                    run_id,
                    approval.approval_id,
                    authority=self._authority(ownership, run_id),
                ):
                    raise ResumeNotAllowedError("Consumed decision could not continue")
                run = self._runs.get(run_id)
                if approval.tool_name == "publish_candidate_patch":
                    return self._complete_mini_linear_publish(ownership, run)
                ready = self._load_snapshot(
                    approvals.get(approval.approval_id), ResumePhase.READY_FOR_MODEL
                )
                return await self._run_loop(
                    ownership,
                    run,
                    list(ready.history),
                    ready.loop_state,
                    list(ready.context_items),
                    ready.last_test_result,
                    ready.test_execution_state,
                )
            if (
                approval.consumption_state is ApprovalConsumptionState.CLAIMED
                and approval.status is ApprovalStatus.APPROVED
            ):
                if mutation_binding is not None and self._mutations is not None:
                    snapshot = self._load_snapshot(
                        approval,
                        ResumePhase.AWAITING_APPROVAL,
                    )
                    recovered_result = self._mutations.recover_claimed(
                        run_id,
                        approval.approval_id,
                        authority=self._authority(ownership, run_id),
                    )
                elif test_binding is not None and self._test_executions is not None:
                    snapshot = self._load_snapshot(
                        approval,
                        ResumePhase.AWAITING_APPROVAL,
                    )
                    recovered_result = self._test_executions.recover_claimed(
                        run_id,
                        approval.approval_id,
                        authority=self._authority(ownership, run_id),
                    )
                else:
                    workflow.mark_indeterminate(
                        run_id,
                        approval.approval_id,
                        authority=self._authority(ownership, run_id),
                    )
                    return self._runs.get(run_id)
                if recovered_result is None:
                    if self._repairs is not None:
                        if mutation_binding is not None and self._mutations is not None:
                            self._repairs.observe_mutation(
                                self._mutations.get_for_approval(approval.approval_id),
                                authority=self._authority(ownership, run_id),
                            )
                        elif test_binding is not None and self._test_executions is not None:
                            self._repairs.observe_test(
                                self._test_executions.get_for_approval(approval.approval_id),
                                authority=self._authority(ownership, run_id),
                            )
                    return self._runs.get(run_id)
            if approval.consumption_state is ApprovalConsumptionState.CONSUMED:
                snapshot = self._load_snapshot(approval, ResumePhase.READY_FOR_MODEL)
                if run.status is RunStatus.PAUSED:
                    if not workflow.claim_consumed_continuation(
                        run_id,
                        approval.approval_id,
                        authority=self._authority(ownership, run_id),
                    ):
                        raise ResumeNotAllowedError("Consumed approval could not resume")
                elif run.status is not RunStatus.RUNNING:
                    raise ResumeNotAllowedError("Consumed approval Run is not recoverable")
                run = self._runs.get(run_id)
                if approval.tool_name == "publish_candidate_patch":
                    return self._complete_mini_linear_publish(ownership, run)
                if self._repairs is not None:
                    repair_state = self._repairs.state(run_id)
                    if repair_state.terminal:
                        return self._finish_repair_run(
                            ownership, run, repair_state.status
                        )
                return await self._run_loop(
                    ownership,
                    run,
                    list(snapshot.history),
                    snapshot.loop_state,
                    list(snapshot.context_items),
                    snapshot.last_test_result,
                    snapshot.test_execution_state,
                )

            if recovered_result is None:
                snapshot = self._load_snapshot(approval, ResumePhase.AWAITING_APPROVAL)
            if approval.status is ApprovalStatus.PENDING:
                raise ResumeNotAllowedError("Approval decision is still pending")
            if approval.status is ApprovalStatus.CANCELLED:
                raise ResumeNotAllowedError("Cancelled approval cannot resume")
            if approval.consumption_state is ApprovalConsumptionState.NOT_STARTED:
                if (
                    approval.status is ApprovalStatus.APPROVED
                    and mutation_binding is not None
                    and self._mutations is not None
                ):
                    self._mutations.ensure_prepared(
                        approval.approval_id,
                        authority=self._authority(ownership, run_id),
                    )
                    claimed = self._mutations.claim_resume(
                        run_id,
                        approval.approval_id,
                        authority=self._authority(ownership, run_id),
                    )
                elif (
                    approval.status is ApprovalStatus.APPROVED
                    and test_binding is not None
                    and self._test_executions is not None
                ):
                    self._test_executions.ensure_created(
                        approval.approval_id,
                        authority=self._authority(ownership, run_id),
                    )
                    claimed = True
                else:
                    claimed = workflow.claim_resume(
                        run_id,
                        approval.approval_id,
                        authority=self._authority(ownership, run_id),
                    )
                if not claimed:
                    raise ResumeNotAllowedError("Approval could not be claimed")
                if test_binding is None or approval.status is not ApprovalStatus.APPROVED:
                    approval = approvals.get(approval.approval_id)
            elif approval.consumption_state is not ApprovalConsumptionState.CLAIMED or (
                approval.status is ApprovalStatus.APPROVED and recovered_result is None
            ):
                raise ResumeNotAllowedError("Approval consumption state is not recoverable")

            result = await self._consume_decision(
                run_id,
                approval,
                snapshot,
                ownership=ownership,
                mutation_binding=mutation_binding,
                test_binding=test_binding,
                recovered_result=recovered_result,
            )
            if not result.success and approval.status is ApprovalStatus.APPROVED:
                run = self._runs.get(run_id)
                return self._fail(
                    ownership, run, result.error_message or "Approved tool failed"
                )
            if not workflow.claim_consumed_continuation(
                run_id,
                approval.approval_id,
                authority=self._authority(ownership, run_id),
            ):
                raise ResumeNotAllowedError("Consumed decision could not continue")
            run = self._runs.get(run_id)
            if approval.tool_name == "publish_candidate_patch":
                return self._complete_mini_linear_publish(ownership, run)
            if self._repairs is not None:
                repair_state = self._repairs.state(run_id)
                if repair_state.terminal:
                    return self._finish_repair_run(ownership, run, repair_state.status)
            ready = self._load_snapshot(
                approvals.get(approval.approval_id),
                ResumePhase.READY_FOR_MODEL,
            )
            return await self._run_loop(
                ownership,
                run,
                list(ready.history),
                ready.loop_state,
                list(ready.context_items),
                ready.last_test_result,
                ready.test_execution_state,
            )
        except (CheckpointNotFoundError, ValidationError, ValueError) as exc:
            current = self._runs.get(run_id)
            return self._recovery_fail(
                ownership,
                current,
                f"Approval checkpoint is invalid: {type(exc).__name__}",
            )
        except MutationOutcomeIndeterminateError:
            return self._runs.get(run_id)
        except TestExecutionOutcomeIndeterminateError:
            if self._repairs is not None and self._test_executions is not None:
                self._repairs.observe_test(
                    self._test_executions.get_for_approval(approval.approval_id),
                    authority=self._authority(ownership, run_id),
                )
            return self._runs.get(run_id)
        except TestExecutionPreflightFailedError:
            if self._repairs is not None and self._test_executions is not None:
                self._repairs.observe_test(
                    self._test_executions.get_for_approval(approval.approval_id),
                    authority=self._authority(ownership, run_id),
                )
            return self._runs.get(run_id)
        except TestExecutionCancelledError:
            return self._runs.get(run_id)
        finally:
            self._active_runs.discard(run_id)

    def _complete_mini_linear_publish(
        self, ownership: RunOwnership, run: Run
    ) -> Run:
        if self._repairs is not None:
            self._repairs.mark_unverified_final(
                run.run_id,
                authority=self._authority(ownership, run.run_id),
            )
        run.final_output = "Candidate patch published"
        run.transition_to(RunStatus.COMPLETED)
        self._runs.save(run, authority=self._authority(ownership, run.run_id))
        self._append_event(
            ownership,
            run.run_id,
            EventType.RUN_COMPLETED,
            {"final_output": run.final_output},
        )
        return run

    async def _consume_decision(
        self,
        run_id: UUID,
        approval: ApprovalRequest,
        snapshot: RuntimeSnapshotV4,
        *,
        ownership: RunOwnership,
        mutation_binding: MutationApprovalBinding | None = None,
        test_binding: TestApprovalBinding | None = None,
        recovered_result: ToolResult | None = None,
        already_claimed: bool = False,
    ) -> ToolResult:
        _, workflow = self._require_approval_support()
        call = snapshot.pending_tool_call
        if call is None:
            raise ValueError("Approval snapshot has no pending tool call")
        final_verification = False
        if approval.status is ApprovalStatus.APPROVED:
            if recovered_result is not None:
                result = recovered_result
            elif test_binding is not None:
                test_coordinator = self._test_executions
                if test_coordinator is None:
                    raise ValueError("Runtime has no test execution coordinator")
                trusted_final_verification = (
                    self._repairs is not None
                    and self._repairs.is_final_profile(run_id, test_binding.profile_id)
                )
                final_verification = trusted_final_verification
                result = await self._tools.execute_managed(
                    self._runs.get(run_id),
                    call.tool,
                    call.arguments,
                    approval=ApprovalAuthorization(
                        approval_id=approval.approval_id,
                        checkpoint_id=approval.checkpoint_id,
                        step_number=snapshot.step_number,
                        request_digest=approval.request_digest,
                    ),
                    operation=lambda _: test_coordinator.execute_approved(
                        run_id,
                        approval.approval_id,
                        already_claimed=already_claimed,
                        expected_purpose=(
                            ProfilePurpose.VERIFICATION
                            if trusted_final_verification
                            else ProfilePurpose.DEVELOPMENT
                        ),
                        ownership=ownership,
                    ),
                    trusted_final_verification=trusted_final_verification,
                    ownership=ownership,
                )
            else:
                started = perf_counter()
                outcome = await self._tools.execute(
                    self._runs.get(run_id),
                    call.tool,
                    call.arguments,
                    approval=ApprovalAuthorization(
                        approval_id=approval.approval_id,
                        checkpoint_id=approval.checkpoint_id,
                        step_number=snapshot.step_number,
                        request_digest=approval.request_digest,
                    ),
                    ownership=ownership,
                )
                if isinstance(outcome, ApprovalRequired):
                    raise ValueError("Approved tool unexpectedly requested approval again")
                result = outcome
                if mutation_binding is not None:
                    if self._mutations is None:
                        raise ValueError("Runtime has no mutation coordinator")
                    mutation_execution = self._mutations.record_result(
                        run_id,
                        approval.approval_id,
                        result,
                        duration_ms=max(0, int((perf_counter() - started) * 1000)),
                        authority=self._authority(ownership, run_id),
                    )
                    if self._repairs is not None:
                        self._repairs.observe_mutation(
                            mutation_execution,
                            authority=self._authority(ownership, run_id),
                        )
        else:
            result = ToolResult(
                success=False,
                error_type=ToolErrorCode.APPROVAL_REJECTED,
                error_message="Tool call was rejected by approval policy",
                output={
                    "approval_id": str(approval.approval_id),
                    "decision": "REJECTED",
                },
            )
        test_execution: ProcessExecutionRecord | None = None
        if (
            test_binding is not None
            and approval.status is ApprovalStatus.APPROVED
            and self._test_executions is not None
        ):
            test_execution = self._test_executions.get_for_approval(approval.approval_id)
            if self._repairs is not None:
                self._repairs.observe_test(
                    test_execution,
                    authority=self._authority(ownership, run_id),
                )
            if (
                isinstance(result.output, dict)
                and self._repairs is not None
                and self._repairs.is_final_profile(run_id, test_binding.profile_id)
            ):
                private_result = TestResult.model_validate(result.output)
                private_result = private_result.model_copy(
                    update={"stdout_summary": "", "stderr_summary": ""}
                )
                result = result.model_copy(
                    update={"output": private_result.model_dump(mode="json")}
                )
        if (
            call.tool == "run_tests"
            and approval.status is ApprovalStatus.APPROVED
            and not final_verification
        ):
            rendered = self._result_renderer.render(call.tool, result)
            result = result.model_copy(
                update={
                    "output": rendered.output,
                    "truncated": rendered.truncated,
                }
            )
        if (
            mutation_binding is not None
            and approval.status is ApprovalStatus.APPROVED
            and recovered_result is not None
            and self._mutations is not None
            and self._repairs is not None
        ):
            self._repairs.observe_mutation(
                self._mutations.get_for_approval(approval.approval_id),
                authority=self._authority(ownership, run_id),
            )
        history = list(snapshot.history)
        history.extend(
            [
                {
                    "type": "tool_call",
                    "call_id": call.call_id,
                    "tool": call.tool,
                    "arguments": call.arguments,
                    "reason": call.reason,
                },
                {
                    "tool_result": result.model_dump(mode="json"),
                    "call_id": call.call_id,
                },
            ]
        )
        context_items = list(snapshot.context_items) or self._context_items_from_history(
            list(snapshot.history)
        )
        context_items.extend(
            [
                ContextItem(
                    kind=ContextItemKind.TOOL_CALL,
                    payload={
                        "type": "tool_call",
                        "call_id": call.call_id,
                        "tool": call.tool,
                        "arguments": call.arguments,
                        "reason": call.reason,
                    },
                    call_id=call.call_id,
                ),
                ContextItem(
                    kind=(
                        ContextItemKind.TOOL_RESULT
                        if approval.status is ApprovalStatus.APPROVED
                        else ContextItemKind.APPROVAL_RESULT
                    ),
                    payload=result.model_dump(mode="json"),
                    call_id=call.call_id,
                ),
            ]
        )
        run = self._runs.get(run_id)
        built = self._context_builder.build(
            task=run.task,
            items=context_items,
            step_number=snapshot.step_number,
        )
        self._record_context_compaction(ownership, run_id, built.removed_summaries)
        pending_test_execution = snapshot.pending_test_execution
        last_test_result = snapshot.last_test_result
        test_execution_state = snapshot.test_execution_state
        if (
            test_binding is not None
            and approval.status is ApprovalStatus.APPROVED
            and self._test_executions is not None
        ):
            if test_execution is None:
                raise ValueError("Approved test execution record is missing")
            pending_test_execution = None
            if isinstance(result.output, dict):
                last_test_result = TestResult.model_validate(
                    {
                        key: value
                        for key, value in result.output.items()
                        if key in TestResult.model_fields
                    }
                )
            test_execution_state = test_execution.status
        ready_snapshot = RuntimeSnapshotV4(
            run_id=run_id,
            step_number=snapshot.step_number,
            history=history,
            context_items=built.items,
            pending_tool_call=None,
            pending_approval_id=approval.approval_id,
            tool_call_digest=approval.request_digest,
            resume_phase=ResumePhase.READY_FOR_MODEL,
            model_usage=snapshot.model_usage,
            model_request_count=snapshot.model_request_count,
            loop_state=snapshot.loop_state,
            context_state=built.state,
            last_provider_metadata=snapshot.last_provider_metadata,
            last_model_error=snapshot.last_model_error,
            context_policy_version=snapshot.context_policy_version,
            system_prompt_version=snapshot.system_prompt_version,
            pending_test_execution=pending_test_execution,
            last_test_result=last_test_result,
            test_execution_state=test_execution_state,
            repair=(self._repairs.snapshot(run_id) if self._repairs is not None else None),
        )
        checkpoint = Checkpoint(
            run_id=run_id,
            step_number=snapshot.step_number,
            runtime_state=ready_snapshot.model_dump(mode="json"),
        )
        workflow.persist_consumed(
            run_id,
            approval.approval_id,
            checkpoint,
            result_status="success" if result.success else "rejected",
            result_summary=(result.error_message or "Tool completed")[:500],
            authority=self._authority(ownership, run_id),
        )
        return result

    async def _run_loop(
        self,
        ownership: RunOwnership,
        run: Run,
        history: list[JsonValue],
        loop_state: LoopState | None = None,
        context_items: list[ContextItem] | None = None,
        last_test_result: TestResult | None = None,
        test_execution_state: ProcessExecutionStatus | None = None,
    ) -> Run:
        current_loop_state = loop_state or LoopState()
        current_context_items = (
            list(context_items)
            if context_items is not None
            else self._context_items_from_history(history)
        )
        while run.current_step < run.max_steps:
            run.current_step += 1
            self._runs.save(run, authority=self._authority(ownership, run.run_id))
            if self._repairs is not None:
                model_budget = self._repairs.consume_model_call(
                    run.run_id,
                    fact_id=f"model-step:{run.current_step}",
                    authority=self._authority(ownership, run.run_id),
                )
                if not model_budget.consumed:
                    return self._finish_repair_run(
                        ownership,
                        run,
                        model_budget.state.status,
                    )
                current_context_items = [
                    item
                    for item in current_context_items
                    if item.kind is not ContextItemKind.REPAIR_RUNTIME_STATE
                ]
                current_context_items.append(
                    ContextItem(
                        kind=ContextItemKind.REPAIR_RUNTIME_STATE,
                        payload=self._repairs.runtime_context(run.run_id),
                    )
                )
            self._append_event(
                ownership,
                run.run_id,
                EventType.MODEL_REQUESTED,
                {"step_number": run.current_step},
            )
            try:
                built = self._context_builder.build(
                    task=run.task,
                    items=current_context_items,
                    step_number=run.current_step,
                )
                current_context_items = list(built.items)
                self._record_context_compaction(
                    ownership, run.run_id, built.removed_summaries
                )
                request = built.request.model_copy(
                    update={
                        "run_id": run.run_id,
                        "tools": self._tools.registry.specs(),
                    }
                )
                if self._model_executor is not None:
                    model_response = await self._model_executor.generate(
                        run,
                        request,
                        ownership=ownership,
                    )
                    output = model_response.action
                else:
                    raw_output = await self._model.generate(request)
                    if isinstance(raw_output, ModelResponse):
                        model_response = raw_output
                        output = raw_output.action
                    else:
                        model_response = None
                        output = parse_model_output(raw_output)
            except (ModelOutputError, ModelProviderError, ModelRequestError) as exc:
                error_code = (
                    exc.code
                    if isinstance(exc, ModelRequestError)
                    else (
                        ModelErrorCode.MODEL_OUTPUT_INVALID
                        if isinstance(exc, ModelOutputError)
                        else ModelErrorCode.MODEL_PROVIDER_ERROR
                    )
                )
                self._append_event(
                    ownership,
                    run.run_id,
                    EventType.MODEL_FAILED,
                    {
                        "step_number": run.current_step,
                        "error_type": error_code.value,
                    },
                )
                return self._fail_model_generation(
                    ownership, run, error_code, str(exc)
                )

            if model_response is not None and model_response.usage is not None:
                run.total_token_usage += model_response.usage.total_tokens or 0
                self._runs.save(
                    run, authority=self._authority(ownership, run.run_id)
                )
            if model_response is not None and model_response.multi_tool_response is not None:
                payload = model_response.multi_tool_response.audit_payload()
                self._append_event(
                    ownership,
                    run.run_id,
                    EventType.MODEL_PROVIDER_DEVIATION,
                    payload,
                )
                self._append_event(
                    ownership,
                    run.run_id,
                    EventType.MULTI_TOOL_RESPONSE_NORMALIZED,
                    payload,
                )
            self._append_event(
                ownership,
                run.run_id,
                EventType.MODEL_RESPONDED,
                {
                    "step_number": run.current_step,
                    "response_type": output.type,
                    "provider": model_response.provider if model_response else self._model.name,
                    "model": model_response.model if model_response else self._model.name,
                    "usage": (
                        model_response.usage.model_dump(mode="json")
                        if model_response and model_response.usage
                        else None
                    ),
                    "provider_metadata": (
                        model_response.sanitized_metadata if model_response else {}
                    ),
                },
            )
            if isinstance(output, FinalAnswer):
                if self._repairs is not None:
                    decision = self._repairs.evaluate_completion(
                        run.run_id,
                        answer_digest=hashlib.sha256(output.answer.encode("utf-8")).hexdigest(),
                        context=CompletionContext(),
                        authority=self._authority(ownership, run.run_id),
                    )
                    if decision.action is CompletionAction.CORRECT:
                        feedback = decision.feedback
                        if feedback is None:
                            return self._fail(
                                ownership, run, "Repair correction feedback is missing"
                            )
                        history.append({"repair_contract_feedback": feedback})
                        current_context_items.append(
                            ContextItem(
                                kind=ContextItemKind.REPAIR_CONTRACT_FEEDBACK,
                                payload={"message": feedback},
                            )
                        )
                        correction_context = self._context_builder.build(
                            task=run.task,
                            items=current_context_items,
                            step_number=run.current_step,
                        )
                        current_context_items = list(correction_context.items)
                        checkpoint = self._checkpoints.save(
                            run.run_id,
                            run.current_step,
                            RuntimeSnapshotV4(
                                run_id=run.run_id,
                                step_number=run.current_step,
                                history=list(history),
                                context_items=current_context_items,
                                resume_phase=ResumePhase.READY_FOR_MODEL,
                                loop_state=current_loop_state,
                                context_state=correction_context.state,
                                last_test_result=last_test_result,
                                test_execution_state=test_execution_state,
                                repair=self._repairs.snapshot(run.run_id),
                            ).model_dump(mode="json"),
                            authority=self._authority(ownership, run.run_id),
                        )
                        self._append_event(
                            ownership,
                            run.run_id,
                            EventType.CHECKPOINT_SAVED,
                            {
                                "checkpoint_id": str(checkpoint.checkpoint_id),
                                "step_number": checkpoint.step_number,
                            },
                        )
                        continue
                    if decision.action is CompletionAction.TERMINAL:
                        return self._fail(
                            ownership,
                            run,
                            decision.state.failure_reason.value
                            if decision.state.failure_reason is not None
                            else decision.state.status.value,
                        )
                    run.final_output = output.answer
                    self._runs.save(
                        run, authority=self._authority(ownership, run.run_id)
                    )
                    final_profile_id = self._repairs.final_profile_id(run.run_id)
                    final_call = ToolCall(
                        type="tool_call",
                        call_id=(f"trusted-final-verification-{decision.state.state_version}"),
                        tool="run_tests",
                        arguments={"profile_id": final_profile_id},
                        reason="AgentForge trusted final verification",
                    )
                    final_outcome = await self._tools.execute(
                        run,
                        final_call.tool,
                        final_call.arguments,
                        trusted_final_verification=True,
                        ownership=ownership,
                    )
                    if not isinstance(final_outcome, ApprovalRequired):
                        return self._fail(
                            ownership,
                            run,
                            final_outcome.error_message
                            or "Trusted final verification did not request approval",
                        )
                    return self._pause_for_approval(
                        ownership,
                        run,
                        final_call,
                        final_outcome,
                        history,
                        current_context_items,
                        current_loop_state,
                        built.state,
                        model_response,
                        last_test_result,
                        test_execution_state,
                    )
                run.final_output = output.answer
                run.transition_to(RunStatus.COMPLETED)
                self._runs.save(
                    run, authority=self._authority(ownership, run.run_id)
                )
                self._append_event(
                    ownership,
                    run.run_id,
                    EventType.RUN_COMPLETED,
                    {"final_output": output.answer},
                )
                return run

            if isinstance(output, ToolCall):
                outcome = await self._tools.execute(
                    run,
                    output.tool,
                    output.arguments,
                    ownership=ownership,
                )
                if isinstance(outcome, ApprovalRequired):
                    return self._pause_for_approval(
                        ownership,
                        run,
                        output,
                        outcome,
                        history,
                        current_context_items,
                        current_loop_state,
                        built.state,
                        model_response,
                        last_test_result,
                        test_execution_state,
                    )
                if not outcome.success:
                    if (
                        self._repairs is None
                        or outcome.error_type not in _RECOVERABLE_MODEL_TOOL_ERRORS
                    ):
                        return self._fail_tool(
                            ownership,
                            run,
                            outcome.error_message or "Tool execution failed",
                        )
                    repair_state = self._repairs.record_model_tool_failure(
                        run.run_id,
                        step_number=run.current_step,
                        tool_name=output.tool,
                        error_type=outcome.error_type,
                        authority=self._authority(ownership, run.run_id),
                    )
                    if repair_state.terminal:
                        return self._finish_repair_run(
                            ownership, run, repair_state.status
                        )
                rendered = self._result_renderer.render(output.tool, outcome)
                rendered_result = (
                    outcome.model_copy(
                        update={
                            "output": rendered.output,
                            "truncated": rendered.truncated,
                        }
                    )
                    if output.tool
                    in {
                        "list_files",
                        "read_file",
                        "search_text",
                        "get_git_diff",
                        "run_tests",
                    }
                    else outcome
                )
                result = rendered_result.model_dump(mode="json")
                history.extend(
                    [
                        output.model_dump(mode="json"),
                        {"tool_result": result, "call_id": output.call_id},
                    ]
                )
                current_context_items.extend(
                    [
                        ContextItem(
                            kind=ContextItemKind.TOOL_CALL,
                            payload=output.model_dump(mode="json"),
                            call_id=output.call_id,
                        ),
                        ContextItem(
                            kind=ContextItemKind.TOOL_RESULT,
                            payload=result,
                            call_id=output.call_id,
                        ),
                    ]
                )
                if model_response is not None and model_response.multi_tool_response is not None:
                    current_context_items.append(
                        ContextItem(
                            kind=ContextItemKind.MULTI_TOOL_NORMALIZATION,
                            payload=self._normalization_context_payload(
                                model_response.multi_tool_response
                            ),
                        )
                    )
                action_digest = self._action_digest(output)
                observation = self._loop_detector.observe(
                    current_loop_state,
                    action_digest=action_digest,
                    result_digest=rendered.sha256_digest,
                    error_code=(
                        outcome.error_type.value if outcome.error_type is not None else None
                    ),
                    tool_name=output.tool,
                    success=outcome.success,
                )
                current_loop_state = observation.state
                if observation.warning:
                    self._append_event(
                        ownership,
                        run.run_id,
                        EventType.LOOP_WARNING,
                        {
                            "action_digest": action_digest,
                            "repeat_count": current_loop_state.consecutive_same_action_result,
                        },
                    )
                    current_context_items.append(
                        ContextItem(
                            kind=ContextItemKind.LOOP_WARNING,
                            payload={
                                "action_digest": action_digest,
                                "repeat_count": current_loop_state.consecutive_same_action_result,
                            },
                        )
                    )
                if observation.terminal:
                    self._append_event(
                        ownership,
                        run.run_id,
                        EventType.LOOP_DETECTED,
                        {"action_digest": action_digest},
                    )
                    return self._fail(
                        ownership, run, "Repeated model tool loop detected"
                    )
                model_state = (
                    self._model_workflow.get_state(run.run_id)
                    if self._model_workflow is not None
                    else None
                )
                checkpoint_context = self._context_builder.build(
                    task=run.task,
                    items=current_context_items,
                    step_number=run.current_step,
                )
                current_context_items = list(checkpoint_context.items)
                self._record_context_compaction(
                    ownership,
                    run.run_id,
                    checkpoint_context.removed_summaries,
                )
                snapshot = RuntimeSnapshotV4(
                    run_id=run.run_id,
                    step_number=run.current_step,
                    history=list(history),
                    context_items=current_context_items,
                    resume_phase=ResumePhase.READY_FOR_MODEL,
                    model_usage=(
                        ModelUsage(
                            input_tokens=model_state.input_tokens,
                            output_tokens=model_state.output_tokens,
                            total_tokens=model_state.total_tokens,
                            cached_input_tokens=model_state.cached_input_tokens,
                            reasoning_tokens=model_state.reasoning_tokens,
                        )
                        if model_state is not None
                        else ModelUsage()
                    ),
                    model_request_count=(
                        model_state.model_request_count if model_state is not None else 0
                    ),
                    loop_state=current_loop_state,
                    context_state=checkpoint_context.state,
                    last_provider_metadata=(
                        model_response.sanitized_metadata if model_response else {}
                    ),
                    last_test_result=last_test_result,
                    test_execution_state=test_execution_state,
                    repair=(
                        self._repairs.snapshot(run.run_id) if self._repairs is not None else None
                    ),
                )
                checkpoint = self._checkpoints.save(
                    run.run_id,
                    run.current_step,
                    snapshot.model_dump(mode="json"),
                    authority=self._authority(ownership, run.run_id),
                )
                self._append_event(
                    ownership,
                    run.run_id,
                    EventType.CHECKPOINT_SAVED,
                    {
                        "checkpoint_id": str(checkpoint.checkpoint_id),
                        "step_number": checkpoint.step_number,
                    },
                )
        return self._fail(
            ownership, run, f"Maximum step count of {run.max_steps} exhausted"
        )

    @staticmethod
    def _context_items_from_history(history: list[JsonValue]) -> list[ContextItem]:
        items: list[ContextItem] = []
        for value in history:
            if isinstance(value, dict) and value.get("type") == "tool_call":
                call_id = value.get("call_id")
                items.append(
                    ContextItem(
                        kind=ContextItemKind.TOOL_CALL,
                        payload=value,
                        call_id=call_id if isinstance(call_id, str) else None,
                    )
                )
            elif isinstance(value, dict) and "tool_result" in value:
                call_id = value.get("call_id")
                items.append(
                    ContextItem(
                        kind=ContextItemKind.TOOL_RESULT,
                        payload=value["tool_result"],
                        call_id=call_id if isinstance(call_id, str) else None,
                    )
                )
            else:
                items.append(ContextItem(kind=ContextItemKind.LEGACY, payload=value))
        return items

    @staticmethod
    def _action_digest(output: ToolCall) -> str:
        canonical = json.dumps(
            {"tool": output.tool, "arguments": output.arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _pause_for_approval(
        self,
        ownership: RunOwnership,
        run: Run,
        output: ToolCall,
        required: ApprovalRequired,
        history: list[JsonValue],
        context_items: list[ContextItem],
        loop_state: LoopState,
        context_state: ResumeContextState,
        model_response: ModelResponse | None,
        last_test_result: TestResult | None,
        test_execution_state: ProcessExecutionStatus | None,
    ) -> Run:
        _, workflow = self._require_approval_support()
        approval_id = uuid4()
        checkpoint_id = uuid4()
        digest = compute_tool_call_digest(
            tool_name=required.tool_name,
            validated_arguments=required.validated_arguments,
            checkpoint_id=checkpoint_id,
            step_number=run.current_step,
        )
        model_state = (
            self._model_workflow.get_state(run.run_id) if self._model_workflow is not None else None
        )
        pending_test_execution = None
        if isinstance(required, TestApprovalRequired):
            test_plan = required.test_plan
            pending_test_execution = PendingTestExecution(
                approval_id=approval_id,
                profile_id=test_plan.profile_id,
                profile_version=test_plan.profile_version,
                profile_digest=test_plan.profile_digest,
                argv_digest=test_plan.argv_digest,
                environment_digest=test_plan.environment_digest,
            )
        snapshot = RuntimeSnapshotV4(
            run_id=run.run_id,
            step_number=run.current_step,
            history=list(history),
            context_items=list(context_items),
            pending_tool_call=PendingToolCall(
                tool=required.tool_name,
                call_id=output.call_id,
                arguments=required.validated_arguments,
                reason=output.reason,
            ),
            pending_approval_id=approval_id,
            tool_call_digest=digest,
            resume_phase=ResumePhase.AWAITING_APPROVAL,
            model_usage=(
                ModelUsage(
                    input_tokens=model_state.input_tokens,
                    output_tokens=model_state.output_tokens,
                    total_tokens=model_state.total_tokens,
                    cached_input_tokens=model_state.cached_input_tokens,
                    reasoning_tokens=model_state.reasoning_tokens,
                )
                if model_state is not None
                else ModelUsage()
            ),
            model_request_count=(model_state.model_request_count if model_state is not None else 0),
            loop_state=loop_state,
            context_state=context_state,
            last_provider_metadata=(model_response.sanitized_metadata if model_response else {}),
            pending_test_execution=pending_test_execution,
            last_test_result=last_test_result,
            test_execution_state=test_execution_state,
            repair=(self._repairs.snapshot(run.run_id) if self._repairs is not None else None),
        )
        checkpoint = Checkpoint(
            checkpoint_id=checkpoint_id,
            run_id=run.run_id,
            step_number=run.current_step,
            runtime_state=snapshot.model_dump(mode="json"),
        )
        approval = ApprovalRequest(
            approval_id=approval_id,
            run_id=run.run_id,
            checkpoint_id=checkpoint_id,
            tool_name=required.tool_name,
            sanitized_arguments=required.sanitized_arguments,
            request_digest=digest,
        )
        mutation_binding = None
        test_binding = None
        if isinstance(required, MutationApprovalRequired):
            if self._mutations is None:
                raise ResumeNotAllowedError(
                    "Runtime received a mutation without mutation persistence"
                )
            mutation_plan = required.mutation_plan
            mutation_binding = MutationApprovalBinding(
                approval_id=approval_id,
                run_id=run.run_id,
                checkpoint_id=checkpoint_id,
                tool_call_digest=digest,
                tool_name=mutation_plan.tool_name,
                target_path=mutation_plan.target_path,
                target_existed=mutation_plan.target_existed,
                before_sha256=mutation_plan.before_sha256,
                expected_after_sha256=mutation_plan.expected_after_sha256,
                bytes_written=mutation_plan.bytes_written,
            )
        if isinstance(required, TestApprovalRequired):
            if self._test_executions is None:
                raise ResumeNotAllowedError(
                    "Runtime received test execution without durable coordination"
                )
            source_revision_number, source_revision_digest = (
                self._test_executions._evaluator_only_source_binding(run.run_id)
                if run.run_id in self._evaluator_only_runs
                else self._test_executions.source_binding(run.run_id)
            )
            test_binding = TestApprovalBinding(
                approval_id=approval_id,
                run_id=run.run_id,
                checkpoint_id=checkpoint_id,
                tool_call_digest=digest,
                source_revision_number=source_revision_number,
                source_revision_digest=source_revision_digest,
                **required.test_plan.model_dump(),
            )
        authority = ownership.expect_release()
        workflow.pause_for_approval(
            run,
            checkpoint,
            approval,
            mutation_binding=mutation_binding,
            test_binding=test_binding,
            authority=authority,
        )
        return run

    def _load_snapshot(
        self,
        approval: ApprovalRequest,
        expected_phase: ResumePhase,
    ) -> RuntimeSnapshotV4:
        checkpoint: Checkpoint | None = (
            self._checkpoints.get(approval.checkpoint_id)
            if expected_phase is ResumePhase.AWAITING_APPROVAL
            else self._checkpoints.latest(approval.run_id)
        )
        if checkpoint is None:
            raise CheckpointNotFoundError(f"Run {approval.run_id} has no recovery checkpoint")
        snapshot = load_runtime_snapshot(
            checkpoint.runtime_state,
            run_id=checkpoint.run_id,
            step_number=checkpoint.step_number,
        )
        if checkpoint.run_id != approval.run_id or snapshot.run_id != approval.run_id:
            raise ValueError("Approval checkpoint belongs to another Run")
        if snapshot.step_number != checkpoint.step_number:
            raise ValueError("Approval checkpoint step does not match")
        if snapshot.pending_approval_id != approval.approval_id:
            raise ValueError("Approval checkpoint ID does not match")
        if snapshot.tool_call_digest != approval.request_digest:
            raise ValueError("Approval checkpoint digest does not match")
        if snapshot.resume_phase != expected_phase:
            raise ValueError(
                "Approval checkpoint resume phase does not match: "
                f"{snapshot.resume_phase!r} != {expected_phase!r}"
            )
        return snapshot

    def _record_context_compaction(
        self,
        ownership: RunOwnership,
        run_id: UUID,
        removed_summaries: list[str],
    ) -> None:
        if removed_summaries:
            self._append_event(
                ownership,
                run_id,
                EventType.CONTEXT_COMPACTED,
                {"removed_pair_count": len(removed_summaries)},
            )

    @staticmethod
    def _normalization_context_payload(
        info: MultiToolResponseInfo,
    ) -> dict[str, JsonValue]:
        return {
            "message": (
                "The previous provider response returned multiple tool calls. "
                "AgentForge executed only the first local READ call. Other calls were "
                "not executed. Re-evaluate the next step using the available result and "
                "do not assume discarded calls completed."
            ),
            "returned_call_count": info.returned_call_count,
            "selected_tool_name": info.selected_tool_name,
            "discarded_call_count": info.discarded_call_count,
            "policy": info.policy.value,
        }

    def _require_approval_support(
        self,
    ) -> tuple[ApprovalRepository, ApprovalWorkflow]:
        if self._approvals is None or self._approval_workflow is None:
            raise ResumeNotAllowedError("Runtime was not configured for approvals")
        return self._approvals, self._approval_workflow

    @staticmethod
    def _authority(
        ownership: RunOwnership, run_id: UUID
    ) -> RunLeaseAuthority:
        authority = ownership.authority
        if authority.run_id != run_id:
            raise StaleFenceError()
        return authority

    def _append_event(
        self,
        ownership: RunOwnership,
        run_id: UUID,
        event_type: EventType,
        payload: dict[str, JsonValue] | None = None,
    ) -> None:
        with self._runs.database.session() as session:
            EventLog().append(
                session,
                self._authority(ownership, run_id),
                event_type,
                payload or {},
            )

    def _driver(self, run_id: UUID) -> RunDriver:
        return RunDriver(
            self._run_leases,
            run_id=run_id,
            owner_id=f"{self._owner_id}:{uuid4()}",
            ttl=self._lease_ttl,
            heartbeat_interval=self._heartbeat_interval,
        )

    def _recovery_fail(
        self, ownership: RunOwnership, run: Run, reason: str
    ) -> Run:
        if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            raise ResumeNotAllowedError(reason)
        return self._fail(ownership, run, reason)

    def _finish_repair_run(
        self,
        ownership: RunOwnership,
        run: Run,
        status: RepairCompletionStatus,
    ) -> Run:
        if status is RepairCompletionStatus.VERIFIED_SUCCESS:
            run.transition_to(RunStatus.COMPLETED)
            self._runs.save(
                run, authority=self._authority(ownership, run.run_id)
            )
            self._append_event(
                ownership,
                run.run_id,
                EventType.RUN_COMPLETED,
                {"repair_status": status.value},
            )
            return run
        return self._fail(ownership, run, status.value)

    def _fail_model_generation(
        self,
        ownership: RunOwnership,
        run: Run,
        error_code: ModelErrorCode,
        failure_detail: str,
    ) -> Run:
        if self._repairs is None:
            return self._fail(ownership, run, failure_detail)
        repair_state = self._repairs.terminalize_model_failure(
            run.run_id,
            error_code=error_code,
            authority=self._authority(ownership, run.run_id),
        )
        return self._finish_repair_run(ownership, run, repair_state.status)

    def _fail_tool(
        self, ownership: RunOwnership, run: Run, reason: str
    ) -> Run:
        if self._repairs is not None:
            repair_state = self._repairs.terminalize_tool_failure(
                run.run_id, authority=self._authority(ownership, run.run_id)
            )
            return self._finish_repair_run(ownership, run, repair_state.status)
        return self._fail(ownership, run, reason)

    def _fail(self, ownership: RunOwnership, run: Run, reason: str) -> Run:
        if self._repairs is not None:
            self._repairs.terminalize_runtime_failure(
                run.run_id,
                authority=self._authority(ownership, run.run_id),
            )
        run.error_message = reason
        run.transition_to(RunStatus.FAILED)
        self._runs.save(run, authority=self._authority(ownership, run.run_id))
        self._append_event(
            ownership,
            run.run_id,
            EventType.RUN_FAILED,
            {"code": self._failure_code(reason).value},
        )
        return run

    @staticmethod
    def _failure_code(reason: str) -> RunFailureCode:
        try:
            return RunFailureCode(reason)
        except ValueError:
            return RunFailureCode.RUNTIME_FAILURE
