from datetime import timedelta
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agentforge.domain.enums import EventType
from agentforge.domain.models import utc_now
from agentforge.domain.test_execution import TestExecutionPlan
from agentforge.evaluation.baseline_models import (
    BaselineExecutionRecord,
    BaselineExecutionStatus,
    BaselineFailureReason,
    BaselineFailureSummary,
    ExpectedBaselineFailure,
)
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import EventLog
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.persistence.tables import EvaluationBaselineExecutionRow


class BaselineExecutionRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, record: BaselineExecutionRecord) -> BaselineExecutionRecord:
        try:
            with self._database.session() as session:
                existing = session.scalar(
                    select(EvaluationBaselineExecutionRow).where(
                        EvaluationBaselineExecutionRow.run_id == str(record.run_id)
                    )
                )
                if existing is not None:
                    persisted = self._to_domain(existing)
                    if not self._same_identity(persisted, record):
                        raise RuntimeError("Baseline execution identity conflict")
                    return persisted
                lease = RunLeaseStore(None).acquire_in_session(
                    session,
                    record.run_id,
                    owner_id=f"legacy-evaluator:baseline-create:{uuid4()}",
                    ttl=timedelta(seconds=30),
                )
                session.add(self._to_row(record))
                EventLog().append(
                    session,
                    lease.authority,
                    EventType.EVALUATION_BASELINE_CREATED,
                    self._event_payload(record),
                )
                RunLeaseStore(None).release_in_session(session, lease.authority)
        except IntegrityError as exc:
            raise RuntimeError("Baseline execution could not be created") from exc
        return record

    def get_for_run(self, run_id: UUID) -> BaselineExecutionRecord:
        record = self.find_for_run(run_id)
        if record is None:
            raise RuntimeError(f"Baseline execution for Run {run_id} is missing")
        return record

    def find_for_run(self, run_id: UUID) -> BaselineExecutionRecord | None:
        with self._database.session() as session:
            row = session.scalar(
                select(EvaluationBaselineExecutionRow).where(
                    EvaluationBaselineExecutionRow.run_id == str(run_id)
                )
            )
            return self._to_domain(row) if row is not None else None

    @staticmethod
    def _same_identity(
        persisted: BaselineExecutionRecord,
        requested: BaselineExecutionRecord,
    ) -> bool:
        return (
            persisted.run_id == requested.run_id
            and persisted.task_id == requested.task_id
            and persisted.workspace_baseline_id == requested.workspace_baseline_id
            and persisted.initial_workspace_digest
            == requested.initial_workspace_digest
            and persisted.test_plan == requested.test_plan
            and persisted.expected_failure == requested.expected_failure
        )

    @staticmethod
    def _to_row(record: BaselineExecutionRecord) -> EvaluationBaselineExecutionRow:
        return EvaluationBaselineExecutionRow(
            baseline_execution_id=str(record.baseline_execution_id),
            run_id=str(record.run_id),
            task_id=record.task_id,
            workspace_baseline_id=str(record.workspace_baseline_id),
            initial_workspace_digest=record.initial_workspace_digest,
            test_plan_data=record.test_plan.model_dump(mode="json"),
            expected_failure_data=record.expected_failure.model_dump(mode="json"),
            expected_fingerprint_digest=record.expected_failure.fingerprint_digest,
            status=record.status.value,
            failure_reason=record.failure_reason.value if record.failure_reason else None,
            record_version=record.record_version,
            result_schema_version=record.result_schema_version,
            actual_fingerprint_digest=record.actual_fingerprint_digest,
            failed_node_ids=list(record.failed_node_ids),
            exit_code=record.exit_code,
            root_pid=record.root_pid,
            process_group_id=record.process_group_id,
            job_id=record.job_id,
            duration_ms=record.duration_ms,
            termination_reason=record.termination_reason,
            termination_result=record.termination_result,
            stdout_digest=record.stdout_digest,
            stderr_digest=record.stderr_digest,
            stdout_size=record.stdout_size,
            stderr_size=record.stderr_size,
            output_truncated=record.output_truncated,
            safe_failure_summary_data=(
                record.safe_failure_summary.model_dump(mode="json")
                if record.safe_failure_summary is not None
                else None
            ),
            safe_failure_summary_digest=record.safe_failure_summary_digest,
            created_at=record.created_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
        )

    @staticmethod
    def _to_domain(row: EvaluationBaselineExecutionRow) -> BaselineExecutionRecord:
        return BaselineExecutionRecord(
            baseline_execution_id=UUID(row.baseline_execution_id),
            run_id=UUID(row.run_id),
            task_id=row.task_id,
            workspace_baseline_id=UUID(row.workspace_baseline_id),
            initial_workspace_digest=row.initial_workspace_digest,
            test_plan=TestExecutionPlan.model_validate(row.test_plan_data),
            expected_failure=ExpectedBaselineFailure.model_validate(
                row.expected_failure_data
            ),
            status=BaselineExecutionStatus(row.status),
            failure_reason=(
                BaselineFailureReason(row.failure_reason) if row.failure_reason else None
            ),
            record_version=row.record_version,
            result_schema_version=row.result_schema_version,
            actual_fingerprint_digest=row.actual_fingerprint_digest,
            failed_node_ids=tuple(row.failed_node_ids),
            exit_code=row.exit_code,
            root_pid=row.root_pid,
            process_group_id=row.process_group_id,
            job_id=row.job_id,
            duration_ms=row.duration_ms,
            termination_reason=row.termination_reason,
            termination_result=row.termination_result,
            stdout_digest=row.stdout_digest,
            stderr_digest=row.stderr_digest,
            stdout_size=row.stdout_size,
            stderr_size=row.stderr_size,
            output_truncated=row.output_truncated,
            safe_failure_summary=(
                BaselineFailureSummary.model_validate(row.safe_failure_summary_data)
                if row.safe_failure_summary_data is not None
                else None
            ),
            safe_failure_summary_digest=row.safe_failure_summary_digest,
            created_at=row.created_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )

    @staticmethod
    def _event_payload(record: BaselineExecutionRecord) -> dict[str, JsonValue]:
        return {
            "baseline_execution_id": str(record.baseline_execution_id),
            "task_id": record.task_id,
            "status": record.status.value,
            "profile_id": record.test_plan.profile_id,
            "profile_version": record.test_plan.profile_version,
            "profile_digest": record.test_plan.profile_digest,
            "workspace_digest": record.initial_workspace_digest,
            "expected_fingerprint_digest": record.expected_failure.fingerprint_digest,
        }


class BaselineExecutionWorkflow:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._repository = BaselineExecutionRepository(database)

    def claim(self, run_id: UUID, *, expected_version: int) -> BaselineExecutionRecord | None:
        now = utc_now()
        with self._database.session() as session:
            lease = RunLeaseStore(None).acquire_in_session(
                session,
                run_id,
                owner_id=f"legacy-evaluator:baseline-claim:{uuid4()}",
                ttl=timedelta(seconds=30),
            )
            changed = ApprovalWorkflow._affected_rows(
                session.execute(
                    update(EvaluationBaselineExecutionRow)
                    .where(
                        EvaluationBaselineExecutionRow.run_id == str(run_id),
                        EvaluationBaselineExecutionRow.status
                        == BaselineExecutionStatus.CREATED.value,
                        EvaluationBaselineExecutionRow.record_version == expected_version,
                    )
                    .values(
                        status=BaselineExecutionStatus.STARTED.value,
                        record_version=EvaluationBaselineExecutionRow.record_version + 1,
                        started_at=now,
                    )
                )
            )
            if changed != 1:
                RunLeaseStore(None).release_in_session(session, lease.authority)
                return None
            row = self._require_row(session, run_id)
            record = BaselineExecutionRepository._to_domain(row)
            EventLog().append(
                session,
                lease.authority,
                EventType.EVALUATION_BASELINE_STARTED,
                BaselineExecutionRepository._event_payload(record),
            )
            RunLeaseStore(None).release_in_session(session, lease.authority)
            return record

    def finish(
        self,
        run_id: UUID,
        *,
        expected_version: int,
        status: BaselineExecutionStatus,
        failure_reason: BaselineFailureReason | None,
        **values: object,
    ) -> BaselineExecutionRecord:
        if status not in {
            BaselineExecutionStatus.VERIFIED_EXPECTED_FAILURE,
            BaselineExecutionStatus.BLOCKED,
            BaselineExecutionStatus.INDETERMINATE,
        }:
            raise ValueError("Baseline finish requires a terminal status")
        now = utc_now()
        update_values = {
            **values,
            "status": status.value,
            "failure_reason": failure_reason.value if failure_reason else None,
            "record_version": EvaluationBaselineExecutionRow.record_version + 1,
            "completed_at": now,
        }
        with self._database.session() as session:
            lease = RunLeaseStore(None).acquire_in_session(
                session,
                run_id,
                owner_id=f"legacy-evaluator:baseline-finish:{uuid4()}",
                ttl=timedelta(seconds=30),
            )
            changed = ApprovalWorkflow._affected_rows(
                session.execute(
                    update(EvaluationBaselineExecutionRow)
                    .where(
                        EvaluationBaselineExecutionRow.run_id == str(run_id),
                        EvaluationBaselineExecutionRow.status
                        == BaselineExecutionStatus.STARTED.value,
                        EvaluationBaselineExecutionRow.record_version == expected_version,
                    )
                    .values(**update_values)
                )
            )
            if changed != 1:
                raise RuntimeError("Baseline terminal transition conflict")
            row = self._require_row(session, run_id)
            record = BaselineExecutionRepository._to_domain(row)
            event_type = {
                BaselineExecutionStatus.VERIFIED_EXPECTED_FAILURE: (
                    EventType.EVALUATION_BASELINE_VERIFIED
                ),
                BaselineExecutionStatus.BLOCKED: EventType.EVALUATION_BASELINE_BLOCKED,
                BaselineExecutionStatus.INDETERMINATE: EventType.EVALUATION_BASELINE_INDETERMINATE,
            }[status]
            payload = BaselineExecutionRepository._event_payload(record)
            payload.update(
                {
                    "failure_reason": failure_reason.value if failure_reason else None,
                    "actual_fingerprint_digest": record.actual_fingerprint_digest,
                    "failure_count": len(record.failed_node_ids),
                    "stdout_digest": record.stdout_digest,
                    "stderr_digest": record.stderr_digest,
                    "duration_ms": record.duration_ms,
                }
            )
            EventLog().append(session, lease.authority, event_type, payload)
            RunLeaseStore(None).release_in_session(session, lease.authority)
            return record

    def recover(self, run_id: UUID) -> BaselineExecutionRecord:
        record = self._repository.get_for_run(run_id)
        if record.status is not BaselineExecutionStatus.STARTED:
            return record
        return self.finish(
            run_id,
            expected_version=record.record_version,
            status=BaselineExecutionStatus.INDETERMINATE,
            failure_reason=BaselineFailureReason.PROCESS_OUTCOME_INDETERMINATE,
            termination_reason="runtime_restarted_while_baseline_started",
        )

    @staticmethod
    def _require_row(
        session: Session, run_id: UUID
    ) -> EvaluationBaselineExecutionRow:
        row = session.scalar(
            select(EvaluationBaselineExecutionRow).where(
                EvaluationBaselineExecutionRow.run_id == str(run_id)
            )
        )
        if row is None:
            raise RuntimeError("Baseline execution record is missing")
        return row
