from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from agentforge.application.contracts import (
    RuntimeTrustClass,
    VerificationCapsuleState,
)
from agentforge.domain.enums import ProcessExecutionStatus, ProcessFailureKind
from agentforge.domain.errors import (
    DuplicateProcessExecutionError,
    ProcessExecutionNotFoundError,
    TestApprovalBindingNotFoundError,
)
from agentforge.domain.test_execution import (
    ProcessExecutionRecord,
    TestApprovalBinding,
)
from agentforge.persistence.database import Database
from agentforge.persistence.tables import ProcessExecutionRow, TestApprovalBindingRow


class TestApprovalBindingRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, binding: TestApprovalBinding) -> TestApprovalBinding:
        with self._database.session() as session:
            session.add(self._to_row(binding))
        return binding

    def get_for_approval(self, approval_id: UUID) -> TestApprovalBinding:
        binding = self.find_for_approval(approval_id)
        if binding is None:
            raise TestApprovalBindingNotFoundError(
                f"Approval {approval_id} has no test-profile binding"
            )
        return binding

    def find_for_approval(self, approval_id: UUID) -> TestApprovalBinding | None:
        with self._database.session() as session:
            row = session.get(TestApprovalBindingRow, str(approval_id))
            return self._to_domain(row) if row is not None else None

    @staticmethod
    def _to_row(binding: TestApprovalBinding) -> TestApprovalBindingRow:
        return TestApprovalBindingRow(
            approval_id=str(binding.approval_id),
            run_id=str(binding.run_id),
            checkpoint_id=str(binding.checkpoint_id),
            tool_call_digest=binding.tool_call_digest,
            profile_id=binding.profile_id,
            profile_version=binding.profile_version,
            profile_digest=binding.profile_digest,
            executable_path=binding.executable_path,
            argv_digest=binding.argv_digest,
            cwd=binding.cwd,
            environment_digest=binding.environment_digest,
            source_revision_number=binding.source_revision_number,
            source_revision_digest=binding.source_revision_digest,
            created_at=binding.created_at,
        )

    @staticmethod
    def _to_domain(row: TestApprovalBindingRow) -> TestApprovalBinding:
        return TestApprovalBinding(
            approval_id=UUID(row.approval_id),
            run_id=UUID(row.run_id),
            checkpoint_id=UUID(row.checkpoint_id),
            tool_call_digest=row.tool_call_digest,
            profile_id=row.profile_id,
            profile_version=row.profile_version,
            profile_digest=row.profile_digest,
            executable_path=row.executable_path,
            argv_digest=row.argv_digest,
            cwd=row.cwd,
            environment_digest=row.environment_digest,
            source_revision_number=row.source_revision_number,
            source_revision_digest=row.source_revision_digest,
            created_at=row.created_at,
        )


class ProcessExecutionRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, record: ProcessExecutionRecord) -> ProcessExecutionRecord:
        try:
            with self._database.session() as session:
                session.add(self._to_row(record))
        except IntegrityError as exc:
            raise DuplicateProcessExecutionError(
                "A process execution already exists for this approval, digest, or Run attempt"
            ) from exc
        return record

    def get(self, execution_id: UUID) -> ProcessExecutionRecord:
        with self._database.session() as session:
            row = session.get(ProcessExecutionRow, str(execution_id))
            if row is None:
                raise ProcessExecutionNotFoundError(
                    f"Process execution {execution_id} does not exist"
                )
            return self._to_domain(row)

    def get_for_approval(self, approval_id: UUID) -> ProcessExecutionRecord:
        record = self.find_for_approval(approval_id)
        if record is None:
            raise ProcessExecutionNotFoundError(
                f"Approval {approval_id} has no process execution"
            )
        return record

    def find_for_approval(self, approval_id: UUID) -> ProcessExecutionRecord | None:
        with self._database.session() as session:
            row = session.scalar(
                select(ProcessExecutionRow).where(
                    ProcessExecutionRow.approval_id == str(approval_id)
                )
            )
            return self._to_domain(row) if row is not None else None

    def list_for_run(self, run_id: UUID) -> list[ProcessExecutionRecord]:
        with self._database.session() as session:
            rows = session.scalars(
                select(ProcessExecutionRow)
                .where(ProcessExecutionRow.run_id == str(run_id))
                .order_by(
                    ProcessExecutionRow.attempt_number,
                    ProcessExecutionRow.execution_id,
                )
            ).all()
            return [self._to_domain(row) for row in rows]

    @staticmethod
    def _to_row(record: ProcessExecutionRecord) -> ProcessExecutionRow:
        return ProcessExecutionRow(
            execution_id=str(record.execution_id),
            run_id=str(record.run_id),
            approval_id=str(record.approval_id),
            tool_call_digest=record.tool_call_digest,
            attempt_number=record.attempt_number,
            record_version=record.record_version,
            result_schema_version=record.result_schema_version,
            profile_id=record.profile_id,
            profile_version=record.profile_version,
            profile_digest=record.profile_digest,
            executable_path=record.executable_path,
            argv_digest=record.argv_digest,
            cwd=record.cwd,
            environment_digest=record.environment_digest,
            source_revision_number=record.source_revision_number,
            source_revision_digest=record.source_revision_digest,
            source_digest_at_start=record.source_digest_at_start,
            verification_capsule_id=(
                str(record.verification_capsule_id)
                if record.verification_capsule_id is not None
                else None
            ),
            capsule_state=record.capsule_state.value if record.capsule_state else None,
            source_snapshot_digest=record.source_snapshot_digest,
            verifier_artifact_digest=record.verifier_artifact_digest,
            executable_artifact_digest=record.executable_artifact_digest,
            artifact_algorithm_version=record.artifact_algorithm_version,
            runtime_trust_class=(
                record.runtime_trust_class.value if record.runtime_trust_class else None
            ),
            root_pid=record.root_pid,
            process_group_id=record.process_group_id,
            job_id=record.job_id,
            status=record.status.value,
            failure_kind=record.failure_kind.value if record.failure_kind else None,
            exit_code=record.exit_code,
            stdout_digest=record.stdout_digest,
            stderr_digest=record.stderr_digest,
            stdout_size=record.stdout_size,
            stderr_size=record.stderr_size,
            stdout_summary=record.stdout_summary,
            stderr_summary=record.stderr_summary,
            stdout_truncated=record.stdout_truncated,
            stderr_truncated=record.stderr_truncated,
            duration_ms=record.duration_ms,
            termination_reason=record.termination_reason,
            termination_result=record.termination_result,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _to_domain(row: ProcessExecutionRow) -> ProcessExecutionRecord:
        return ProcessExecutionRecord(
            execution_id=UUID(row.execution_id),
            run_id=UUID(row.run_id),
            approval_id=UUID(row.approval_id),
            tool_call_digest=row.tool_call_digest,
            attempt_number=row.attempt_number,
            record_version=row.record_version,
            result_schema_version=row.result_schema_version,
            profile_id=row.profile_id,
            profile_version=row.profile_version,
            profile_digest=row.profile_digest,
            executable_path=row.executable_path,
            argv_digest=row.argv_digest,
            cwd=row.cwd,
            environment_digest=row.environment_digest,
            source_revision_number=row.source_revision_number,
            source_revision_digest=row.source_revision_digest,
            source_digest_at_start=row.source_digest_at_start,
            verification_capsule_id=(
                UUID(row.verification_capsule_id)
                if row.verification_capsule_id is not None
                else None
            ),
            capsule_state=(
                VerificationCapsuleState(row.capsule_state)
                if row.capsule_state is not None
                else None
            ),
            source_snapshot_digest=row.source_snapshot_digest,
            verifier_artifact_digest=row.verifier_artifact_digest,
            executable_artifact_digest=row.executable_artifact_digest,
            artifact_algorithm_version=row.artifact_algorithm_version,
            runtime_trust_class=(
                RuntimeTrustClass(row.runtime_trust_class)
                if row.runtime_trust_class is not None
                else None
            ),
            root_pid=row.root_pid,
            process_group_id=row.process_group_id,
            job_id=row.job_id,
            status=ProcessExecutionStatus(row.status),
            failure_kind=(
                ProcessFailureKind(row.failure_kind)
                if row.failure_kind is not None
                else None
            ),
            exit_code=row.exit_code,
            stdout_digest=row.stdout_digest,
            stderr_digest=row.stderr_digest,
            stdout_size=row.stdout_size,
            stderr_size=row.stderr_size,
            stdout_summary=row.stdout_summary,
            stderr_summary=row.stderr_summary,
            stdout_truncated=row.stdout_truncated,
            stderr_truncated=row.stderr_truncated,
            duration_ms=row.duration_ms,
            termination_reason=row.termination_reason,
            termination_result=row.termination_result,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
