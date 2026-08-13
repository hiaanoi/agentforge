from datetime import timedelta
from typing import cast
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from agentforge.domain.enums import EventType, MutationExecutionStatus, ProcessExecutionStatus
from agentforge.domain.models import normalize_utc, utc_now
from agentforge.domain.mutations import MutationExecutionRecord
from agentforge.domain.repair import (
    BudgetConsumptionDecision,
    BudgetKind,
    RepairCompletionStatus,
    RepairState,
    RepairTaskPolicy,
    RepairTerminationReason,
    terminal_priority,
)
from agentforge.domain.test_execution import ProcessExecutionRecord
from agentforge.evaluation.validators import DiffValidationResult
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import EventLog, RunLeaseAuthority
from agentforge.persistence.run_leases import RunLeaseStore, claim_bound_write
from agentforge.persistence.tables import (
    DiffValidationResultRow,
    RepairBudgetConsumptionRow,
    RepairStateRow,
    RepairTaskPolicyRow,
)

_COUNTER_FIELDS: dict[BudgetKind, str] = {
    BudgetKind.MODEL: "model_calls_used",
    BudgetKind.READ: "read_calls_used",
    BudgetKind.EDIT: "edit_attempts_used",
    BudgetKind.TEST: "test_runs_used",
    BudgetKind.COMPLETION_CORRECTION: "completion_corrections_used",
    BudgetKind.POLICY_VIOLATION: "policy_violations",
}

_LIMIT_FIELDS: dict[BudgetKind, str] = {
    BudgetKind.MODEL: "max_model_calls",
    BudgetKind.READ: "max_read_calls",
    BudgetKind.EDIT: "max_edit_attempts",
    BudgetKind.TEST: "max_test_runs",
    BudgetKind.COMPLETION_CORRECTION: "max_completion_corrections",
    BudgetKind.POLICY_VIOLATION: "max_policy_violations",
}

_LIMIT_REASONS: dict[BudgetKind, RepairTerminationReason] = {
    BudgetKind.MODEL: RepairTerminationReason.MODEL_CALL_LIMIT,
    BudgetKind.READ: RepairTerminationReason.READ_LIMIT,
    BudgetKind.EDIT: RepairTerminationReason.EDIT_LIMIT,
    BudgetKind.TEST: RepairTerminationReason.TEST_LIMIT,
    BudgetKind.COMPLETION_CORRECTION: RepairTerminationReason.COMPLETION_CORRECTION_LIMIT,
    BudgetKind.POLICY_VIOLATION: RepairTerminationReason.POLICY_VIOLATION_LIMIT,
}


class RepairWorkflow:
    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        """The immutable transaction domain bound to this workflow."""

        return self._database

    def _evaluator_only_start(
        self,
        run_id: UUID,
        policy: RepairTaskPolicy,
        baseline_id: UUID,
        baseline_digest: str,
    ) -> RepairState:
        lease = RunLeaseStore(self._database).acquire(
            run_id,
            owner_id=f"legacy-evaluator:repair-start:{uuid4()}",
            ttl=timedelta(seconds=30),
        )
        try:
            return self._start(
                run_id,
                policy,
                baseline_id,
                baseline_digest,
                authority=lease.authority,
            )
        finally:
            RunLeaseStore(self._database).release(lease.authority)

    def _start(
        self,
        run_id: UUID,
        policy: RepairTaskPolicy,
        baseline_id: UUID,
        baseline_digest: str,
        *,
        authority: RunLeaseAuthority,
    ) -> RepairState:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            existing_policy = session.get(RepairTaskPolicyRow, str(run_id))
            existing_state = session.get(RepairStateRow, str(run_id))
            if existing_policy is not None or existing_state is not None:
                if existing_policy is None or existing_state is None:
                    raise RuntimeError("Repair policy binding is incomplete")
                if (
                    existing_policy.policy_digest != policy.policy_digest
                    or existing_state.baseline_id != str(baseline_id)
                    or existing_state.baseline_digest != baseline_digest
                ):
                    raise RuntimeError("Repair policy binding conflicts with persisted state")
                return self._state_to_domain(existing_state)

            now = utc_now()
            deadline = now + timedelta(seconds=policy.max_wall_time_seconds)
            session.add(
                RepairTaskPolicyRow(
                    run_id=str(run_id),
                    task_id=policy.task_id,
                    policy_version=policy.policy_version,
                    policy_digest=policy.policy_digest,
                    policy_data=policy.model_dump(mode="json"),
                    created_at=now,
                )
            )
            state = RepairState(
                run_id=run_id,
                task_id=policy.task_id,
                policy_digest=policy.policy_digest,
                started_at=now,
                deadline_at=deadline,
                baseline_id=baseline_id,
                baseline_digest=baseline_digest,
                updated_at=now,
            )
            session.add(self._state_to_row(state))
            self._append_event(
                session,
                authority,
                EventType.REPAIR_POLICY_BOUND,
                {
                    "task_id": policy.task_id,
                    "policy_version": policy.policy_version,
                    "policy_digest": policy.policy_digest,
                },
            )
            self._append_event(
                session,
                authority,
                EventType.REPAIR_TASK_STARTED,
                {"task_id": policy.task_id, "baseline_digest": baseline_digest},
            )
            return state

    def get_policy(self, run_id: UUID) -> RepairTaskPolicy:
        with self._database.session() as session:
            row = self._require_policy(session, run_id)
            return RepairTaskPolicy.model_validate(row.policy_data)

    def get_state(self, run_id: UUID) -> RepairState:
        with self._database.session() as session:
            row = session.get(RepairStateRow, str(run_id))
            if row is None:
                raise RuntimeError(f"Repair state for Run {run_id} is missing")
            return self._state_to_domain(row)

    def consume_budget(
        self,
        run_id: UUID,
        kind: BudgetKind,
        fact_id: str,
        *,
        authority: RunLeaseAuthority,
    ) -> BudgetConsumptionDecision:
        if not fact_id or len(fact_id) > 200:
            raise ValueError("Budget fact_id must be a bounded non-empty value")
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            duplicate = session.scalar(
                select(RepairBudgetConsumptionRow).where(
                    RepairBudgetConsumptionRow.run_id == str(run_id),
                    RepairBudgetConsumptionRow.budget_kind == kind.value,
                    RepairBudgetConsumptionRow.fact_id == fact_id,
                )
            )
            state_row = self._require_state(session, run_id)
            if duplicate is not None:
                return BudgetConsumptionDecision(
                    state=self._state_to_domain(state_row), consumed=False
                )
            if state_row.status != RepairCompletionStatus.RUNNING.value:
                return BudgetConsumptionDecision(
                    state=self._state_to_domain(state_row),
                    consumed=False,
                    exhausted=state_row.status == RepairCompletionStatus.BUDGET_EXHAUSTED.value,
                )
            policy_row = session.get(RepairTaskPolicyRow, str(run_id))
            if policy_row is None:
                raise RuntimeError("Repair policy binding is missing")
            policy = RepairTaskPolicy.model_validate(policy_row.policy_data)
            counter_field = _COUNTER_FIELDS[kind]
            current = int(getattr(state_row, counter_field))
            limit = int(getattr(policy, _LIMIT_FIELDS[kind]))
            if current >= limit:
                state_row.status = (
                    RepairCompletionStatus.POLICY_BLOCKED.value
                    if kind is BudgetKind.POLICY_VIOLATION
                    else RepairCompletionStatus.BUDGET_EXHAUSTED.value
                )
                state_row.failure_reason = _LIMIT_REASONS[kind].value
                state_row.state_version += 1
                state_row.updated_at = utc_now()
                self._append_event(
                    session,
                    authority,
                    EventType.REPAIR_TASK_FAILED,
                    {"status": state_row.status, "reason": state_row.failure_reason},
                )
                return BudgetConsumptionDecision(
                    state=self._state_to_domain(state_row),
                    consumed=False,
                    exhausted=True,
                )
            setattr(state_row, counter_field, current + 1)
            state_row.state_version += 1
            state_row.updated_at = utc_now()
            session.add(
                RepairBudgetConsumptionRow(
                    consumption_id=str(uuid4()),
                    run_id=str(run_id),
                    budget_kind=kind.value,
                    fact_id=fact_id,
                    created_at=utc_now(),
                )
            )
            self._append_event(
                session,
                authority,
                EventType.REPAIR_BUDGET_CONSUMED,
                {
                    "budget_kind": kind.value,
                    "fact_id": fact_id,
                    "used": current + 1,
                    "limit": limit,
                },
            )
            return BudgetConsumptionDecision(state=self._state_to_domain(state_row), consumed=True)

    def record_policy_violation(
        self,
        run_id: UUID,
        *,
        fact_id: str,
        rule: str,
        severe: bool,
        authority: RunLeaseAuthority,
    ) -> RepairState:
        decision = self.consume_budget(
            run_id,
            BudgetKind.POLICY_VIOLATION,
            fact_id,
            authority=authority,
        )
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            if decision.consumed:
                self._append_event(
                    session,
                    authority,
                    EventType.REPAIR_POLICY_VIOLATION,
                    {"fact_id": fact_id, "rule": rule, "severe": severe},
                )
            row = self._require_state(session, run_id)
            if severe and row.status == RepairCompletionStatus.RUNNING.value:
                row.status = RepairCompletionStatus.POLICY_BLOCKED.value
                row.failure_reason = RepairTerminationReason.UNSUPPORTED_CAPABILITY.value
                row.state_version += 1
                row.updated_at = utc_now()
                self._append_event(
                    session,
                    authority,
                    EventType.REPAIR_TASK_FAILED,
                    {"status": row.status, "reason": row.failure_reason},
                )
            return self._state_to_domain(row)

    def mark_final_answer_received(
        self,
        run_id: UUID,
        expected_version: int,
        *,
        authority: RunLeaseAuthority,
    ) -> RepairState:
        now = utc_now()
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            result = session.execute(
                update(RepairStateRow)
                .where(
                    RepairStateRow.run_id == str(run_id),
                    RepairStateRow.state_version == expected_version,
                    RepairStateRow.status == RepairCompletionStatus.RUNNING.value,
                )
                .values(
                    final_answer_received=True,
                    state_version=RepairStateRow.state_version + 1,
                    updated_at=now,
                )
            )
            if ApprovalWorkflow._affected_rows(result) != 1:
                raise RuntimeError("Repair state version conflict")
            return self._state_to_domain(self._require_state(session, run_id))

    def observe_mutation(
        self,
        record: MutationExecutionRecord,
        *,
        authority: RunLeaseAuthority,
    ) -> RepairState:
        if record.status is MutationExecutionStatus.PREPARED:
            return self.get_state(record.run_id)
        self.consume_budget(
            record.run_id,
            BudgetKind.EDIT,
            str(record.execution_id),
            authority=authority,
        )
        with self._database.session() as session:
            claim_bound_write(session, record.run_id, authority)
            row = self._require_state(session, record.run_id)
            if row.status != RepairCompletionStatus.RUNNING.value:
                return self._state_to_domain(row)
            if record.status is MutationExecutionStatus.INDETERMINATE:
                row.status = RepairCompletionStatus.INDETERMINATE.value
                row.failure_reason = RepairTerminationReason.INDETERMINATE_SIDE_EFFECT.value
            elif record.status is not MutationExecutionStatus.COMMITTED:
                return self._state_to_domain(row)
            elif row.last_mutation_execution_id == str(record.execution_id):
                return self._state_to_domain(row)
            elif (
                row.last_mutation_committed_at is not None
                and normalize_utc(row.last_mutation_committed_at) > record.updated_at
            ):
                return self._state_to_domain(row)
            else:
                row.last_mutation_execution_id = str(record.execution_id)
                row.last_mutation_committed_at = record.updated_at
                row.latest_source_verified = False
            row.state_version += 1
            row.updated_at = utc_now()
            return self._state_to_domain(row)

    def observe_test(
        self,
        record: ProcessExecutionRecord,
        *,
        final_verification: bool,
        authority: RunLeaseAuthority,
    ) -> RepairState:
        if record.status is ProcessExecutionStatus.CREATED:
            return self.get_state(record.run_id)
        self.consume_budget(
            record.run_id,
            BudgetKind.TEST,
            str(record.execution_id),
            authority=authority,
        )
        with self._database.session() as session:
            claim_bound_write(session, record.run_id, authority)
            row = self._require_state(session, record.run_id)
            if row.status != RepairCompletionStatus.RUNNING.value:
                return self._state_to_domain(row)
            if record.status in {
                ProcessExecutionStatus.STARTED,
                ProcessExecutionStatus.CREATED,
            }:
                return self._state_to_domain(row)
            if record.status is ProcessExecutionStatus.INDETERMINATE:
                row.status = RepairCompletionStatus.INDETERMINATE.value
                row.failure_reason = RepairTerminationReason.INDETERMINATE_SIDE_EFFECT.value
                row.state_version += 1
                row.updated_at = utc_now()
                return self._state_to_domain(row)
            success = record.status is ProcessExecutionStatus.COMPLETED
            if final_verification:
                if row.final_verification_execution_id == str(record.execution_id):
                    return self._state_to_domain(row)
                row.final_verification_execution_id = str(record.execution_id)
                row.final_verification_success = success
                row.final_verification_completed_at = record.updated_at
                row.pending_final_verification = False
                row.status = (
                    RepairCompletionStatus.VERIFIED_SUCCESS.value
                    if success
                    else RepairCompletionStatus.FINAL_VERIFICATION_FAILED.value
                )
                row.failure_reason = (
                    None
                    if success
                    else RepairTerminationReason.FINAL_HIDDEN_TEST_FAILED.value
                )
                self._append_event(
                    session,
                    authority,
                    (
                        EventType.FINAL_VERIFICATION_COMPLETED
                        if success
                        else EventType.FINAL_VERIFICATION_FAILED
                    ),
                    {
                        "execution_id": str(record.execution_id),
                        "passed": success,
                        "duration_ms": record.duration_ms,
                        "stdout_digest": record.stdout_digest,
                        "stderr_digest": record.stderr_digest,
                    },
                )
                self._append_event(
                    session,
                    authority,
                    (
                        EventType.REPAIR_TASK_COMPLETED
                        if success
                        else EventType.REPAIR_TASK_FAILED
                    ),
                    {"status": row.status},
                )
            else:
                if row.last_development_test_execution_id == str(record.execution_id):
                    return self._state_to_domain(row)
                if (
                    row.last_development_test_completed_at is not None
                    and normalize_utc(row.last_development_test_completed_at) > record.updated_at
                ):
                    return self._state_to_domain(row)
                row.last_development_test_execution_id = str(record.execution_id)
                row.last_development_test_success = success
                row.last_development_test_completed_at = record.updated_at
                row.latest_source_verified = bool(
                    success
                    and (
                        row.last_mutation_committed_at is None
                        or record.updated_at > normalize_utc(row.last_mutation_committed_at)
                    )
                )
            row.state_version += 1
            row.updated_at = utc_now()
            return self._state_to_domain(row)

    def record_diff_validation(
        self,
        run_id: UUID,
        result: DiffValidationResult,
        *,
        authority: RunLeaseAuthority,
        persist_result: bool = False,
    ) -> RepairState:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            if persist_result:
                existing_result = session.scalar(
                    select(DiffValidationResultRow).where(
                        DiffValidationResultRow.run_id == str(run_id),
                        DiffValidationResultRow.baseline_digest == result.baseline_digest,
                        DiffValidationResultRow.final_workspace_digest
                        == result.final_workspace_digest,
                    )
                )
                if existing_result is None:
                    session.add(
                        DiffValidationResultRow(
                            validation_id=result.validation_id,
                            run_id=str(run_id),
                            policy_digest=self._require_policy(session, run_id).policy_digest,
                            baseline_digest=result.baseline_digest,
                            final_workspace_digest=result.final_workspace_digest,
                            diff_digest=result.diff_digest,
                            compliant=result.compliant,
                            result_data=result.model_dump(mode="json"),
                            validation_version=result.validation_version,
                            created_at=result.created_at,
                        )
                    )
                elif (
                    existing_result.diff_digest != result.diff_digest
                    or existing_result.result_data != result.model_dump(mode="json")
                ):
                    raise RuntimeError("Diff validation persistence conflict")
            row = self._require_state(session, run_id)
            if row.baseline_digest != result.baseline_digest:
                raise RuntimeError("Diff validation baseline does not match RepairState")
            if (
                row.last_diff_validation_id == result.validation_id
                and row.final_workspace_digest == result.final_workspace_digest
            ):
                return self._state_to_domain(row)
            if row.status != RepairCompletionStatus.RUNNING.value:
                return self._state_to_domain(row)
            row.last_diff_validation_id = result.validation_id
            row.final_workspace_digest = result.final_workspace_digest
            row.final_diff_digest = result.diff_digest
            if not result.compliant:
                row.status = RepairCompletionStatus.DIFF_POLICY_VIOLATION.value
                row.failure_reason = RepairTerminationReason.DIFF_POLICY_VIOLATION.value
            row.state_version += 1
            row.updated_at = utc_now()
            self._append_event(
                session,
                authority,
                EventType.WORKSPACE_DIFF_VALIDATED,
                {
                    "validation_id": result.validation_id,
                    "baseline_digest": result.baseline_digest,
                    "final_workspace_digest": result.final_workspace_digest,
                    "diff_digest": result.diff_digest,
                    "compliant": result.compliant,
                    "changed_file_count": result.changed_file_count,
                    "violation_count": len(result.violations),
                },
            )
            if not result.compliant:
                violation_kinds = [
                    cast(JsonValue, value)
                    for value in sorted(
                        {item.kind.value for item in result.violations}
                    )
                ]
                self._append_event(
                    session,
                    authority,
                    EventType.WORKSPACE_DIFF_VIOLATION,
                    {
                        "validation_id": result.validation_id,
                        "violation_kinds": violation_kinds,
                    },
                )
                self._append_event(
                    session,
                    authority,
                    EventType.REPAIR_TASK_FAILED,
                    {"status": row.status, "reason": row.failure_reason},
                )
            return self._state_to_domain(row)

    def record_completion_correction(
        self,
        run_id: UUID,
        *,
        answer_digest: str,
        feedback: str,
        maximum: int,
        authority: RunLeaseAuthority,
    ) -> BudgetConsumptionDecision:
        if len(answer_digest) != 64:
            raise ValueError("FinalAnswer digest must be SHA-256")
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            row = self._require_state(session, run_id)
            duplicate = session.scalar(
                select(RepairBudgetConsumptionRow).where(
                    RepairBudgetConsumptionRow.run_id == str(run_id),
                    RepairBudgetConsumptionRow.budget_kind
                    == BudgetKind.COMPLETION_CORRECTION.value,
                    RepairBudgetConsumptionRow.fact_id == answer_digest,
                )
            )
            if duplicate is not None:
                return BudgetConsumptionDecision(state=self._state_to_domain(row), consumed=False)
            if row.status != RepairCompletionStatus.RUNNING.value:
                return BudgetConsumptionDecision(state=self._state_to_domain(row), consumed=False)
            row.final_answer_received = True
            if row.completion_corrections_used >= maximum:
                row.status = RepairCompletionStatus.UNVERIFIED_FINAL.value
                row.failure_reason = RepairTerminationReason.LATEST_MUTATION_NOT_VERIFIED.value
                row.state_version += 1
                row.updated_at = utc_now()
                self._append_event(
                    session,
                    authority,
                    EventType.REPAIR_COMPLETION_REJECTED,
                    {"answer_digest": answer_digest, "corrected": False},
                )
                self._append_event(
                    session,
                    authority,
                    EventType.REPAIR_TASK_FAILED,
                    {"status": row.status, "reason": row.failure_reason},
                )
                return BudgetConsumptionDecision(
                    state=self._state_to_domain(row),
                    consumed=False,
                    exhausted=True,
                )
            row.completion_corrections_used += 1
            row.state_version += 1
            row.updated_at = utc_now()
            session.add(
                RepairBudgetConsumptionRow(
                    consumption_id=str(uuid4()),
                    run_id=str(run_id),
                    budget_kind=BudgetKind.COMPLETION_CORRECTION.value,
                    fact_id=answer_digest,
                    created_at=utc_now(),
                )
            )
            self._append_event(
                session,
                authority,
                EventType.REPAIR_BUDGET_CONSUMED,
                {
                    "budget_kind": BudgetKind.COMPLETION_CORRECTION.value,
                    "fact_id": answer_digest,
                    "used": row.completion_corrections_used,
                    "limit": maximum,
                },
            )
            self._append_event(
                session,
                authority,
                EventType.REPAIR_COMPLETION_REJECTED,
                {"answer_digest": answer_digest, "corrected": True},
            )
            self._append_event(
                session,
                authority,
                EventType.REPAIR_COMPLETION_CORRECTED,
                {
                    "answer_digest": answer_digest,
                    "feedback_digest": self._text_digest(feedback),
                },
            )
            return BudgetConsumptionDecision(state=self._state_to_domain(row), consumed=True)

    def mark_pending_final_verification(
        self,
        run_id: UUID,
        expected_version: int,
        *,
        authority: RunLeaseAuthority,
    ) -> RepairState:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            row = self._require_state(session, run_id)
            if row.state_version != expected_version:
                raise RuntimeError("Repair state version conflict")
            if row.status != RepairCompletionStatus.RUNNING.value:
                return self._state_to_domain(row)
            row.final_answer_received = True
            row.pending_final_verification = True
            row.state_version += 1
            row.updated_at = utc_now()
            self._append_event(
                session,
                authority,
                EventType.FINAL_VERIFICATION_REQUESTED,
                {"profile_visibility": "hidden"},
            )
            return self._state_to_domain(row)

    def transition_terminal(
        self,
        run_id: UUID,
        *,
        expected_version: int,
        status: RepairCompletionStatus,
        reason: RepairTerminationReason,
        authority: RunLeaseAuthority,
    ) -> RepairState:
        if status is RepairCompletionStatus.RUNNING:
            raise ValueError("Terminal transition requires a terminal status")
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            row = self._require_state(session, run_id)
            if row.state_version != expected_version:
                raise RuntimeError("Repair state version conflict")
            current_status = RepairCompletionStatus(row.status)
            if current_status is not RepairCompletionStatus.RUNNING:
                if terminal_priority(current_status) <= terminal_priority(status):
                    return self._state_to_domain(row)
            row.status = status.value
            row.failure_reason = reason.value
            row.state_version += 1
            row.updated_at = utc_now()
            self._append_event(
                session,
                authority,
                (
                    EventType.REPAIR_TASK_COMPLETED
                    if status is RepairCompletionStatus.VERIFIED_SUCCESS
                    else EventType.REPAIR_TASK_FAILED
                ),
                {"status": status.value, "reason": reason.value},
            )
            return self._state_to_domain(row)

    def _evaluator_only_transition_terminal(
        self,
        run_id: UUID,
        *,
        expected_version: int,
        status: RepairCompletionStatus,
        reason: RepairTerminationReason,
    ) -> RepairState:
        leases = RunLeaseStore(self._database)
        lease = leases.acquire(
            run_id,
            owner_id=f"legacy-evaluator:repair-terminal:{uuid4()}",
            ttl=timedelta(seconds=30),
        )
        try:
            return self.transition_terminal(
                run_id,
                expected_version=expected_version,
                status=status,
                reason=reason,
                authority=lease.authority,
            )
        finally:
            leases.release(lease.authority)

    @staticmethod
    def _require_state(session: Session, run_id: UUID) -> RepairStateRow:
        row = session.get(RepairStateRow, str(run_id))
        if row is None:
            raise RuntimeError(f"Repair state for Run {run_id} is missing")
        return row

    @staticmethod
    def _state_to_row(state: RepairState) -> RepairStateRow:
        return RepairStateRow(
            run_id=str(state.run_id),
            task_id=state.task_id,
            policy_digest=state.policy_digest,
            status=state.status.value,
            model_calls_used=state.model_calls_used,
            read_calls_used=state.read_calls_used,
            edit_attempts_used=state.edit_attempts_used,
            test_runs_used=state.test_runs_used,
            completion_corrections_used=state.completion_corrections_used,
            policy_violations=state.policy_violations,
            started_at=state.started_at,
            deadline_at=state.deadline_at,
            baseline_id=str(state.baseline_id),
            baseline_digest=state.baseline_digest,
            last_mutation_execution_id=None,
            last_mutation_committed_at=None,
            last_development_test_execution_id=None,
            last_development_test_success=None,
            last_development_test_completed_at=None,
            final_verification_execution_id=None,
            final_verification_success=None,
            final_verification_completed_at=None,
            last_diff_validation_id=None,
            final_workspace_digest=None,
            final_diff_digest=None,
            latest_source_verified=False,
            pending_final_verification=False,
            final_answer_received=False,
            failure_reason=None,
            state_version=state.state_version,
            updated_at=state.updated_at,
        )

    @staticmethod
    def _require_policy(session: Session, run_id: UUID) -> RepairTaskPolicyRow:
        row = session.get(RepairTaskPolicyRow, str(run_id))
        if row is None:
            raise RuntimeError(f"Repair policy for Run {run_id} is missing")
        return row

    @staticmethod
    def _state_to_domain(row: RepairStateRow) -> RepairState:
        return RepairState(
            run_id=UUID(row.run_id),
            task_id=row.task_id,
            policy_digest=row.policy_digest,
            status=RepairCompletionStatus(row.status),
            model_calls_used=row.model_calls_used,
            read_calls_used=row.read_calls_used,
            edit_attempts_used=row.edit_attempts_used,
            test_runs_used=row.test_runs_used,
            completion_corrections_used=row.completion_corrections_used,
            policy_violations=row.policy_violations,
            started_at=row.started_at,
            deadline_at=row.deadline_at,
            baseline_id=UUID(row.baseline_id),
            baseline_digest=row.baseline_digest,
            last_mutation_execution_id=(
                UUID(row.last_mutation_execution_id) if row.last_mutation_execution_id else None
            ),
            last_mutation_committed_at=row.last_mutation_committed_at,
            last_development_test_execution_id=(
                UUID(row.last_development_test_execution_id)
                if row.last_development_test_execution_id
                else None
            ),
            last_development_test_success=row.last_development_test_success,
            last_development_test_completed_at=row.last_development_test_completed_at,
            final_verification_execution_id=(
                UUID(row.final_verification_execution_id)
                if row.final_verification_execution_id
                else None
            ),
            final_verification_success=row.final_verification_success,
            final_verification_completed_at=row.final_verification_completed_at,
            last_diff_validation_id=row.last_diff_validation_id,
            final_workspace_digest=row.final_workspace_digest,
            final_diff_digest=row.final_diff_digest,
            latest_source_verified=row.latest_source_verified,
            pending_final_verification=row.pending_final_verification,
            final_answer_received=row.final_answer_received,
            failure_reason=(
                RepairTerminationReason(row.failure_reason) if row.failure_reason else None
            ),
            state_version=row.state_version,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _append_event(
        session: Session,
        authority: RunLeaseAuthority,
        event_type: EventType,
        payload: dict[str, JsonValue],
    ) -> None:
        EventLog().append(session, authority, event_type, payload)

    @staticmethod
    def _text_digest(value: str) -> str:
        import hashlib

        return hashlib.sha256(value.encode("utf-8")).hexdigest()
