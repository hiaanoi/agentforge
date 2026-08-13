from datetime import timedelta
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from agentforge.application.approval_commands import DecideApprovalCommand
from agentforge.application.contracts import ReceiptStatus
from agentforge.domain.enums import (
    ApprovalConsumptionState,
    ApprovalStatus,
    EventType,
    MutationExecutionStatus,
    ProcessExecutionStatus,
    ProcessFailureKind,
    RejectionStrategy,
    RunFailureCode,
    RunStatus,
)
from agentforge.domain.errors import (
    ApprovalDecisionConflictError,
    ApprovalNotFoundError,
    ResumeNotAllowedError,
    RunNotFoundError,
)
from agentforge.domain.models import (
    ApprovalRequest,
    Checkpoint,
    Run,
    utc_now,
)
from agentforge.domain.mutations import MutationApprovalBinding
from agentforge.domain.repair import RepairCompletionStatus, RepairTerminationReason
from agentforge.domain.test_execution import TestApprovalBinding
from agentforge.persistence.application_uow import ApplicationUnitOfWork
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import EventLog, RunLeaseAuthority
from agentforge.persistence.product_tables import WorkspaceSourceBindingRow
from agentforge.persistence.receipts import ReceiptStore
from agentforge.persistence.repair_terminal import (
    RepairStateProvenance,
    terminalize_repair_in_session,
)
from agentforge.persistence.repositories import (
    ApprovalRepository,
    RunRepository,
)
from agentforge.persistence.run_leases import RunLeaseStore, claim_bound_write
from agentforge.persistence.source_revisions import (
    source_revision_audit,
    source_revision_summary,
)
from agentforge.persistence.tables import (
    ApprovalRequestRow,
    CheckpointRow,
    MutationApprovalBindingRow,
    MutationExecutionRow,
    ProcessExecutionRow,
    RunRow,
    TestApprovalBindingRow,
)
from agentforge.testing.failpoints import DisabledFailpoints, FailpointController


class ApprovalWorkflow:
    def __init__(
        self,
        database: Database,
        *,
        failpoints: FailpointController | None = None,
    ) -> None:
        self._initialize(database, failpoints, RepairStateProvenance.PRODUCT_BUNDLE)

    @classmethod
    def _evaluator_only_create(
        cls,
        database: Database,
        *,
        failpoints: FailpointController | None = None,
    ) -> "ApprovalWorkflow":
        workflow = cls.__new__(cls)
        workflow._initialize(
            database, failpoints, RepairStateProvenance.EVALUATOR_LEGACY
        )
        return workflow

    def _initialize(
        self,
        database: Database,
        failpoints: FailpointController | None,
        provenance: RepairStateProvenance,
    ) -> None:
        if type(provenance) is not RepairStateProvenance:
            raise TypeError("RepairState provenance must be an exact enum value")
        self._database = database
        self._failpoints = (
            DisabledFailpoints() if failpoints is None else failpoints
        )
        self._repair_state_provenance = provenance

    def __setattr__(self, name: str, value: object) -> None:
        if name == "_repair_state_provenance" and hasattr(self, name):
            raise AttributeError("RepairState provenance is immutable")
        object.__setattr__(self, name, value)

    @property
    def database(self) -> Database:
        return self._database

    def pause_for_approval(
        self,
        run: Run,
        checkpoint: Checkpoint,
        approval: ApprovalRequest,
        *,
        mutation_binding: MutationApprovalBinding | None = None,
        test_binding: TestApprovalBinding | None = None,
        authority: RunLeaseAuthority,
    ) -> None:
        if mutation_binding is not None and test_binding is not None:
            raise ValueError("An approval cannot bind mutation and test execution together")
        if mutation_binding is not None and (
            mutation_binding.approval_id != approval.approval_id
            or mutation_binding.run_id != run.run_id
            or mutation_binding.checkpoint_id != checkpoint.checkpoint_id
            or mutation_binding.tool_call_digest != approval.request_digest
            or mutation_binding.tool_name != approval.tool_name
        ):
            raise ValueError("Mutation binding does not match the approval boundary")
        if test_binding is not None and (
            test_binding.approval_id != approval.approval_id
            or test_binding.run_id != run.run_id
            or test_binding.checkpoint_id != checkpoint.checkpoint_id
            or test_binding.tool_call_digest != approval.request_digest
        ):
            raise ValueError("Test binding does not match the approval boundary")
        run.transition_to(RunStatus.WAITING_APPROVAL)
        with self._database.session() as session:
            claim_bound_write(session, run.run_id, authority)
            changed = self._affected_rows(
                session.execute(
                    update(RunRow)
                    .where(
                        RunRow.run_id == str(run.run_id),
                        RunRow.status == RunStatus.RUNNING.value,
                    )
                    .values(status=run.status.value, updated_at=run.updated_at)
                )
            )
            if changed != 1:
                raise ResumeNotAllowedError("Run is not running at the approval boundary")
            session.add(self._checkpoint_row(checkpoint))
            session.flush()
            session.add(ApprovalRepository._to_row(approval))
            session.flush()
            if mutation_binding is not None:
                session.add(
                    MutationApprovalBindingRow(
                        approval_id=str(mutation_binding.approval_id),
                        run_id=str(mutation_binding.run_id),
                        checkpoint_id=str(mutation_binding.checkpoint_id),
                        tool_call_digest=mutation_binding.tool_call_digest,
                        tool_name=mutation_binding.tool_name,
                        target_path=mutation_binding.target_path,
                        target_existed=mutation_binding.target_existed,
                        before_sha256=mutation_binding.before_sha256,
                        expected_after_sha256=mutation_binding.expected_after_sha256,
                        bytes_written=mutation_binding.bytes_written,
                        created_at=mutation_binding.created_at,
                    )
                )
                session.flush()
            if test_binding is not None:
                session.add(
                    TestApprovalBindingRow(
                        approval_id=str(test_binding.approval_id),
                        run_id=str(test_binding.run_id),
                        checkpoint_id=str(test_binding.checkpoint_id),
                        tool_call_digest=test_binding.tool_call_digest,
                        profile_id=test_binding.profile_id,
                        profile_version=test_binding.profile_version,
                        profile_digest=test_binding.profile_digest,
                        executable_path=test_binding.executable_path,
                        argv_digest=test_binding.argv_digest,
                        cwd=test_binding.cwd,
                        environment_digest=test_binding.environment_digest,
                        source_revision_number=test_binding.source_revision_number,
                        source_revision_digest=test_binding.source_revision_digest,
                        created_at=test_binding.created_at,
                    )
                )
                session.flush()
            self._append_event(
                session,
                authority,
                EventType.CHECKPOINT_SAVED,
                {
                    "checkpoint_id": str(checkpoint.checkpoint_id),
                    "step_number": checkpoint.step_number,
                },
            )
            if mutation_binding is not None:
                semantics, verified = source_revision_audit(
                    session.get(WorkspaceSourceBindingRow, str(run.run_id)) is not None,
                    actual_digest_verified=False,
                )
                self._append_event(
                    session,
                    authority,
                    EventType.MUTATION_REQUESTED,
                    {
                        "approval_id": str(approval.approval_id),
                        "tool_name": mutation_binding.tool_name,
                        "relative_path": mutation_binding.target_path,
                        "before_sha256": mutation_binding.before_sha256,
                        "expected_after_sha256": mutation_binding.expected_after_sha256,
                        "bytes_written": mutation_binding.bytes_written,
                        "status": "REQUESTED",
                        "source_revision_semantics": semantics,
                        "source_verified": verified,
                    },
                )
            if test_binding is not None:
                self._append_event(
                    session,
                    authority,
                    EventType.TEST_REQUESTED,
                    {
                        "approval_id": str(approval.approval_id),
                        "profile_id": test_binding.profile_id,
                        "profile_version": test_binding.profile_version,
                        "profile_digest": test_binding.profile_digest,
                        "status": "REQUESTED",
                    },
                )
            self._append_event(
                session,
                authority,
                EventType.APPROVAL_REQUESTED,
                {
                    "approval_id": str(approval.approval_id),
                    "tool_name": approval.tool_name,
                    "sanitized_arguments": approval.sanitized_arguments,
                },
            )
            self._append_event(
                session,
                authority,
                EventType.RUN_PAUSED,
                {"reason": "WAITING_APPROVAL"},
            )
            RunLeaseStore(None).release_in_session(session, authority)

    def _evaluator_only_resolve(
        self,
        approval_id: UUID,
        status: ApprovalStatus,
        strategy: RejectionStrategy,
        note: str | None,
    ) -> ApprovalRequest:
        if status not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
            raise ValueError("Approval can only be approved or rejected")
        with self._database.session() as session:
            return self._resolve_in_session(session, approval_id, status, strategy, note)

    def resolve_command(self, command: DecideApprovalCommand) -> ApprovalRequest:
        conflict = False
        conflict_message = "Approval decision lost the compare-and-swap"
        result: ApprovalRequest | None = None
        decision_committed = False
        with ApplicationUnitOfWork(self._database) as uow:
            session = uow.session
            row = session.get(ApprovalRequestRow, str(command.approval_id))
            if row is None:
                raise ApprovalNotFoundError(f"Approval {command.approval_id} does not exist")
            run_id = UUID(row.run_id)
            receipts = ReceiptStore()
            receipt = receipts.accept(session, command)
            if receipt.status is ReceiptStatus.COMPLETED:
                if receipt.result_scope_type != "RUN" or receipt.result_scope_id != str(run_id):
                    raise ApprovalDecisionConflictError("Approval receipt scope does not match")
                result = self._terminal_decision_replay(session, command)
                uow.commit()
                return result
            if receipt.status is ReceiptStatus.ACCEPTED:
                receipts.mark_in_progress(session, command.command_id, run_id)
            elif receipt.status is ReceiptStatus.IN_PROGRESS:
                pass
            else:
                conflict = True
                conflict_message = "Approval receipt is terminal"
            if not conflict:
                result, decision_committed = self._resolve_command_cas_in_session(
                    session, command
                )
                if result is None:
                    receipts.fail(session, command.command_id, at=utc_now())
                    conflict = True
                else:
                    receipts.complete(
                        session,
                        command.command_id,
                        at=result.decided_at or utc_now(),
                    )
            uow.commit()
        if conflict or result is None:
            raise ApprovalDecisionConflictError(conflict_message)
        if decision_committed:
            self._failpoints.hit("approval_committed_before_ack")
        return result

    def _resolve_command_cas_in_session(
        self,
        session: Session,
        command: DecideApprovalCommand,
    ) -> tuple[ApprovalRequest | None, bool]:
        row = session.get(ApprovalRequestRow, str(command.approval_id))
        if row is None:
            raise ApprovalNotFoundError(f"Approval {command.approval_id} does not exist")
        run_id = UUID(row.run_id)
        command_lease = RunLeaseStore(None).acquire_in_session(
            session,
            run_id,
            owner_id=f"approval-command:{command.command_id}",
            ttl=timedelta(seconds=30),
        )
        authority = command_lease.authority
        now = utc_now()
        changed = session.execute(
            update(ApprovalRequestRow)
            .where(
                ApprovalRequestRow.approval_id == str(command.approval_id),
                ApprovalRequestRow.run_id == str(run_id),
                ApprovalRequestRow.status == ApprovalStatus.PENDING.value,
                ApprovalRequestRow.consumption_state == ApprovalConsumptionState.NOT_STARTED.value,
            )
            .values(
                status=command.status.value,
                rejection_strategy=command.strategy.value,
                decision_note=command.note,
                decided_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if self._affected_rows(changed) != 1:
            existing = session.get(
                ApprovalRequestRow,
                str(command.approval_id),
                populate_existing=True,
            )
            RunLeaseStore(None).release_in_session(session, authority)
            if existing is not None and self._same_terminal_decision(existing, command):
                return ApprovalRepository._to_domain(existing), False
            return None, False

        row = session.get(
            ApprovalRequestRow,
            str(command.approval_id),
            populate_existing=True,
        )
        if row is None:
            raise ApprovalNotFoundError(f"Approval {command.approval_id} does not exist")
        run_row = self._require_run_row(session, run_id)
        if run_row.status != RunStatus.WAITING_APPROVAL.value:
            raise ResumeNotAllowedError("Run is not waiting for this approval")
        self._append_event(
            session,
            authority,
            (
                EventType.APPROVAL_GRANTED
                if command.status is ApprovalStatus.APPROVED
                else EventType.APPROVAL_REJECTED
            ),
            {"approval_id": row.approval_id, "strategy": command.strategy.value},
        )
        if command.status is ApprovalStatus.REJECTED:
            self._append_event(
                session,
                authority,
                EventType.TOOL_FAILED,
                {
                    "tool_name": row.tool_name,
                    "success": False,
                    "error_type": "APPROVAL_REJECTED",
                },
            )
        if (
            command.status is ApprovalStatus.REJECTED
            and command.strategy is RejectionStrategy.FAIL_RUN
        ):
            run_row.status = RunStatus.FAILED.value
            run_row.error_message = "Tool approval was rejected"
            self._append_event(
                session,
                authority,
                EventType.RUN_FAILED,
                {"code": RunFailureCode.APPROVAL_REJECTED.value},
            )
            terminalize_repair_in_session(
                session,
                run_id,
                status=RepairCompletionStatus.RUNTIME_FAILURE,
                reason=RepairTerminationReason.RUNTIME_FAILURE,
                at=now,
                provenance=self._repair_state_provenance,
            )
        else:
            run_row.status = RunStatus.PAUSED.value
        run_row.updated_at = now
        session.flush()
        result = ApprovalRepository._to_domain(row)
        RunLeaseStore(None).release_in_session(session, authority)
        return result, True

    def _terminal_decision_replay(
        self,
        session: Session,
        command: DecideApprovalCommand,
    ) -> ApprovalRequest:
        row = session.get(ApprovalRequestRow, str(command.approval_id))
        if row is None:
            raise ApprovalNotFoundError(f"Approval {command.approval_id} does not exist")
        if not self._same_terminal_decision(row, command):
            raise ApprovalDecisionConflictError("Approval already has a different decision")
        return ApprovalRepository._to_domain(row)

    @staticmethod
    def _same_terminal_decision(
        row: ApprovalRequestRow,
        command: DecideApprovalCommand,
    ) -> bool:
        return row.status == command.status.value and (
            command.status is not ApprovalStatus.REJECTED
            or row.rejection_strategy == command.strategy.value
        )

    def _resolve_in_session(
        self,
        session: Session,
        approval_id: UUID,
        status: ApprovalStatus,
        strategy: RejectionStrategy,
        note: str | None,
    ) -> ApprovalRequest:
        row = session.get(ApprovalRequestRow, str(approval_id))
        if row is None:
            raise ApprovalNotFoundError(f"Approval {approval_id} does not exist")
        if row.status == status.value:
            if status is ApprovalStatus.REJECTED and row.rejection_strategy != strategy.value:
                raise ApprovalDecisionConflictError(
                    "Approval was rejected with a different strategy"
                )
            return ApprovalRepository._to_domain(row)
        if row.status == ApprovalStatus.CANCELLED.value:
            raise ResumeNotAllowedError("Cancelled approval cannot be resolved")
        if row.status != ApprovalStatus.PENDING.value:
            raise ApprovalDecisionConflictError("Approval already has a different decision")
        run_id = UUID(row.run_id)
        run_row = self._require_run_row(session, run_id)
        if run_row.status != RunStatus.WAITING_APPROVAL.value:
            raise ResumeNotAllowedError("Run is not waiting for this approval")
        command_lease = RunLeaseStore(None).acquire_in_session(
            session,
            run_id,
            owner_id=f"approval-command:{uuid4()}",
            ttl=timedelta(seconds=30),
        )
        session.info["agentforge_run_authority"] = command_lease.authority
        now = utc_now()
        row.status = status.value
        row.rejection_strategy = strategy.value
        row.decision_note = note
        row.decided_at = now
        self._append_event(
            session,
            command_lease.authority,
            EventType.APPROVAL_GRANTED
            if status is ApprovalStatus.APPROVED
            else EventType.APPROVAL_REJECTED,
            {"approval_id": row.approval_id, "strategy": strategy.value},
        )
        if status is ApprovalStatus.REJECTED:
            self._append_event(
                session,
                command_lease.authority,
                EventType.TOOL_FAILED,
                {
                    "tool_name": row.tool_name,
                    "success": False,
                    "error_type": "APPROVAL_REJECTED",
                },
            )
        if status is ApprovalStatus.REJECTED and strategy is RejectionStrategy.FAIL_RUN:
            run_row.status = RunStatus.FAILED.value
            run_row.error_message = "Tool approval was rejected"
            self._append_event(
                session,
                command_lease.authority,
                EventType.RUN_FAILED,
                {"code": RunFailureCode.APPROVAL_REJECTED.value},
            )
            terminalize_repair_in_session(
                session,
                run_id,
                status=RepairCompletionStatus.RUNTIME_FAILURE,
                reason=RepairTerminationReason.RUNTIME_FAILURE,
                at=now,
                provenance=self._repair_state_provenance,
            )
        else:
            run_row.status = RunStatus.PAUSED.value
        run_row.updated_at = now
        session.flush()
        result = ApprovalRepository._to_domain(row)
        RunLeaseStore(None).release_in_session(session, command_lease.authority)
        return result

    def claim_resume(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        authority: RunLeaseAuthority,
    ) -> bool:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            approval_changed = self._affected_rows(
                session.execute(
                    update(ApprovalRequestRow)
                    .where(
                        ApprovalRequestRow.approval_id == str(approval_id),
                        ApprovalRequestRow.run_id == str(run_id),
                        ApprovalRequestRow.status.in_(
                            [ApprovalStatus.APPROVED.value, ApprovalStatus.REJECTED.value]
                        ),
                        ApprovalRequestRow.consumption_state
                        == ApprovalConsumptionState.NOT_STARTED.value,
                    )
                    .values(consumption_state=ApprovalConsumptionState.CLAIMED.value)
                )
            )
            if approval_changed != 1:
                return False
            now = utc_now()
            run_changed = self._affected_rows(
                session.execute(
                    update(RunRow)
                    .where(
                        RunRow.run_id == str(run_id),
                        RunRow.status == RunStatus.PAUSED.value,
                    )
                    .values(status=RunStatus.RUNNING.value, updated_at=now)
                )
            )
            if run_changed != 1:
                raise ResumeNotAllowedError("Run cannot be claimed for resume")
            self._append_event(
                session,
                authority,
                EventType.RUN_RESUMED,
                {"approval_id": str(approval_id), "phase": "DECISION"},
            )
            return True

    @staticmethod
    def claim_decision_in_session(
        session: Session,
        run_id: UUID,
        approval_id: UUID,
        authority: RunLeaseAuthority,
    ) -> None:
        """Claim only the decision fact; ResumeRunWorkflow owns Run/Event/Receipt."""
        claim_bound_write(session, run_id, authority)
        changed = ApprovalWorkflow._affected_rows(
            session.execute(
                update(ApprovalRequestRow)
                .where(
                    ApprovalRequestRow.approval_id == str(approval_id),
                    ApprovalRequestRow.run_id == str(run_id),
                    ApprovalRequestRow.status.in_(
                        [ApprovalStatus.APPROVED.value, ApprovalStatus.REJECTED.value]
                    ),
                    ApprovalRequestRow.consumption_state
                    == ApprovalConsumptionState.NOT_STARTED.value,
                )
                .values(consumption_state=ApprovalConsumptionState.CLAIMED.value)
            )
        )
        if changed != 1:
            raise ResumeNotAllowedError("Approval decision could not be claimed")

    def persist_consumed(
        self,
        run_id: UUID,
        approval_id: UUID,
        checkpoint: Checkpoint,
        *,
        result_status: str,
        result_summary: str,
        authority: RunLeaseAuthority,
    ) -> ApprovalRequest:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            row = session.get(ApprovalRequestRow, str(approval_id))
            if row is None or row.run_id != str(run_id):
                raise ApprovalNotFoundError(f"Approval {approval_id} does not exist for Run")
            if row.consumption_state != ApprovalConsumptionState.CLAIMED.value:
                raise ResumeNotAllowedError("Only a claimed approval can be consumed")
            run_row = self._require_run_row(session, run_id)
            if run_row.status != RunStatus.RUNNING.value:
                raise ResumeNotAllowedError("Run is not executing a claimed approval")
            now = utc_now()
            session.add(self._checkpoint_row(checkpoint))
            row.consumption_state = ApprovalConsumptionState.CONSUMED.value
            row.result_status = result_status[:100]
            row.result_summary = result_summary[:500]
            row.consumed_at = now
            run_row.status = RunStatus.PAUSED.value
            run_row.updated_at = now
            self._append_event(
                session,
                authority,
                EventType.CHECKPOINT_SAVED,
                {
                    "checkpoint_id": str(checkpoint.checkpoint_id),
                    "step_number": checkpoint.step_number,
                },
            )
            session.flush()
            return ApprovalRepository._to_domain(row)

    def claim_consumed_continuation(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        authority: RunLeaseAuthority,
    ) -> bool:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            approval = session.get(ApprovalRequestRow, str(approval_id))
            if (
                approval is None
                or approval.run_id != str(run_id)
                or approval.consumption_state != ApprovalConsumptionState.CONSUMED.value
            ):
                return False
            changed = self._affected_rows(
                session.execute(
                    update(RunRow)
                    .where(
                        RunRow.run_id == str(run_id),
                        RunRow.status == RunStatus.PAUSED.value,
                    )
                    .values(status=RunStatus.RUNNING.value, updated_at=utc_now())
                )
            )
            if changed != 1:
                return False
            self._append_event(
                session,
                authority,
                EventType.RUN_RESUMED,
                {"approval_id": str(approval_id), "phase": "MODEL"},
            )
            return True

    def mark_indeterminate(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        authority: RunLeaseAuthority,
    ) -> None:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            changed = self._affected_rows(
                session.execute(
                    update(ApprovalRequestRow)
                    .where(
                        ApprovalRequestRow.approval_id == str(approval_id),
                        ApprovalRequestRow.run_id == str(run_id),
                        ApprovalRequestRow.consumption_state
                        == ApprovalConsumptionState.CLAIMED.value,
                    )
                    .values(
                        consumption_state=ApprovalConsumptionState.INDETERMINATE.value,
                        result_status="indeterminate",
                        result_summary="Tool outcome is unknown after interrupted execution",
                    )
                )
            )
            if changed != 1:
                raise ResumeNotAllowedError("Approval is not in a claimed state")
            run_row = self._require_run_row(session, run_id)
            now = utc_now()
            run_row.status = RunStatus.FAILED.value
            run_row.error_message = "Approved tool outcome is indeterminate"
            run_row.updated_at = now
            self._append_event(
                session,
                authority,
                EventType.RUN_FAILED,
                {"code": RunFailureCode.APPROVAL_INDETERMINATE.value},
            )
            terminalize_repair_in_session(
                session,
                run_id,
                status=RepairCompletionStatus.INDETERMINATE,
                reason=RepairTerminationReason.INDETERMINATE_SIDE_EFFECT,
                at=now,
                provenance=self._repair_state_provenance,
            )

    def cancel(
        self,
        run_id: UUID,
        reason: str | None,
        *,
        authority: RunLeaseAuthority,
    ) -> Run:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            row = self._require_run_row(session, run_id)
            run = RunRepository._to_domain(row)
            if run.status is RunStatus.CANCELLED:
                return run
            if run.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
                raise ResumeNotAllowedError("Terminal Run cannot be cancelled")
            run.transition_to(RunStatus.CANCELLED)
            row.status = run.status.value
            row.updated_at = run.updated_at
            prepared_mutations = session.scalars(
                select(MutationExecutionRow).where(
                    MutationExecutionRow.run_id == str(run_id),
                    MutationExecutionRow.status == MutationExecutionStatus.PREPARED.value,
                )
            ).all()
            for mutation in prepared_mutations:
                source_bound = session.get(WorkspaceSourceBindingRow, str(run_id)) is not None
                semantics, verified = source_revision_audit(
                    source_bound, actual_digest_verified=False
                )
                mutation.status = MutationExecutionStatus.FAILED.value
                mutation.result_summary = source_revision_summary(
                    source_bound,
                    actual_digest_verified=False,
                    message="Mutation cancelled before execution",
                )
                mutation.updated_at = run.updated_at
                self._append_event(
                    session,
                    authority,
                    EventType.MUTATION_FAILED,
                    {
                        "execution_id": mutation.execution_id,
                        "run_id": mutation.run_id,
                        "tool_name": mutation.tool_name,
                        "relative_path": mutation.target_path,
                        "before_sha256": mutation.before_sha256,
                        "after_sha256": None,
                        "bytes_written": 0,
                        "duration_ms": 0,
                        "status": MutationExecutionStatus.FAILED.value,
                        "reason": "CANCELLED_BEFORE_EXECUTION",
                        "source_revision_semantics": semantics,
                        "source_verified": verified,
                    },
                )
            created_tests = session.scalars(
                select(ProcessExecutionRow).where(
                    ProcessExecutionRow.run_id == str(run_id),
                    ProcessExecutionRow.status == ProcessExecutionStatus.CREATED.value,
                )
            ).all()
            for execution in created_tests:
                execution.status = ProcessExecutionStatus.CANCELLED.value
                execution.failure_kind = ProcessFailureKind.CANCELLED.value
                execution.termination_reason = "cancelled_before_start"
                execution.termination_result = "no_process_started"
                execution.record_version += 1
                execution.updated_at = run.updated_at
                self._append_event(
                    session,
                    authority,
                    EventType.TEST_CANCELLED,
                    {
                        "execution_id": execution.execution_id,
                        "run_id": execution.run_id,
                        "approval_id": execution.approval_id,
                        "profile_id": execution.profile_id,
                        "profile_version": execution.profile_version,
                        "profile_digest": execution.profile_digest,
                        "attempt_number": execution.attempt_number,
                        "status": ProcessExecutionStatus.CANCELLED.value,
                        "failure_kind": ProcessFailureKind.CANCELLED.value,
                        "exit_code": None,
                        "duration_ms": 0,
                        "stdout_digest": None,
                        "stderr_digest": None,
                        "stdout_size": 0,
                        "stderr_size": 0,
                        "truncated": False,
                        "termination_result": "no_process_started",
                    },
                )
            session.execute(
                update(ApprovalRequestRow)
                .where(
                    ApprovalRequestRow.run_id == str(run_id),
                    ApprovalRequestRow.consumption_state != ApprovalConsumptionState.CONSUMED.value,
                )
                .values(
                    status=ApprovalStatus.CANCELLED.value,
                    decided_at=utc_now(),
                )
            )
            self._append_event(
                session,
                authority,
                EventType.RUN_CANCELLED,
                {"reason": reason or "Run cancelled"},
            )
            terminalize_repair_in_session(
                session,
                run_id,
                status=RepairCompletionStatus.CANCELLED,
                reason=RepairTerminationReason.CANCELLED,
                at=run.updated_at,
                provenance=self._repair_state_provenance,
            )
            return run

    @staticmethod
    def _affected_rows(result: object) -> int:
        rowcount = getattr(result, "rowcount", None)
        if not isinstance(rowcount, int):
            raise RuntimeError("Conditional update did not report an affected row count")
        return rowcount

    @staticmethod
    def _checkpoint_row(checkpoint: Checkpoint) -> CheckpointRow:
        return CheckpointRow(
            checkpoint_id=str(checkpoint.checkpoint_id),
            run_id=str(checkpoint.run_id),
            step_number=checkpoint.step_number,
            runtime_state=checkpoint.runtime_state,
            created_at=checkpoint.created_at,
        )

    @staticmethod
    def _require_run_row(session: Session, run_id: UUID) -> RunRow:
        row = session.get(RunRow, str(run_id))
        if row is None:
            raise RunNotFoundError(f"Run {run_id} does not exist")
        return row

    @staticmethod
    def _append_event(
        session: Session,
        authority: RunLeaseAuthority,
        event_type: EventType,
        payload: dict[str, JsonValue],
    ) -> None:
        EventLog().append(session, authority, event_type, payload)
