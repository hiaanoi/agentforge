from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from agentforge.domain.enums import MutationExecutionStatus
from agentforge.domain.errors import (
    DuplicateMutationExecutionError,
    MutationBindingNotFoundError,
    MutationExecutionNotFoundError,
)
from agentforge.domain.mutations import (
    MutationApprovalBinding,
    MutationExecutionRecord,
)
from agentforge.persistence.database import Database
from agentforge.persistence.tables import (
    MutationApprovalBindingRow,
    MutationExecutionRow,
)


class MutationApprovalBindingRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, binding: MutationApprovalBinding) -> MutationApprovalBinding:
        with self._database.session() as session:
            session.add(self._to_row(binding))
        return binding

    def get_for_approval(self, approval_id: UUID) -> MutationApprovalBinding:
        binding = self.find_for_approval(approval_id)
        if binding is None:
            raise MutationBindingNotFoundError(
                f"Approval {approval_id} has no mutation binding"
            )
        return binding

    def find_for_approval(self, approval_id: UUID) -> MutationApprovalBinding | None:
        with self._database.session() as session:
            row = session.get(MutationApprovalBindingRow, str(approval_id))
            return self._to_domain(row) if row is not None else None

    @staticmethod
    def _to_row(binding: MutationApprovalBinding) -> MutationApprovalBindingRow:
        return MutationApprovalBindingRow(
            approval_id=str(binding.approval_id),
            run_id=str(binding.run_id),
            checkpoint_id=str(binding.checkpoint_id),
            tool_call_digest=binding.tool_call_digest,
            tool_name=binding.tool_name,
            target_path=binding.target_path,
            target_existed=binding.target_existed,
            before_sha256=binding.before_sha256,
            expected_after_sha256=binding.expected_after_sha256,
            bytes_written=binding.bytes_written,
            created_at=binding.created_at,
        )

    @staticmethod
    def _to_domain(row: MutationApprovalBindingRow) -> MutationApprovalBinding:
        return MutationApprovalBinding(
            approval_id=UUID(row.approval_id),
            run_id=UUID(row.run_id),
            checkpoint_id=UUID(row.checkpoint_id),
            tool_call_digest=row.tool_call_digest,
            tool_name=row.tool_name,
            target_path=row.target_path,
            target_existed=row.target_existed,
            before_sha256=row.before_sha256,
            expected_after_sha256=row.expected_after_sha256,
            bytes_written=row.bytes_written,
            created_at=row.created_at,
        )


class MutationExecutionRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, record: MutationExecutionRecord) -> MutationExecutionRecord:
        try:
            with self._database.session() as session:
                session.add(self._to_row(record))
        except IntegrityError as exc:
            raise DuplicateMutationExecutionError(
                "A mutation execution already exists for this approval or digest"
            ) from exc
        return record

    def get(self, execution_id: UUID) -> MutationExecutionRecord:
        with self._database.session() as session:
            row = session.get(MutationExecutionRow, str(execution_id))
            if row is None:
                raise MutationExecutionNotFoundError(
                    f"Mutation execution {execution_id} does not exist"
                )
            return self._to_domain(row)

    def get_for_approval(self, approval_id: UUID) -> MutationExecutionRecord:
        record = self.find_for_approval(approval_id)
        if record is None:
            raise MutationExecutionNotFoundError(
                f"Approval {approval_id} has no mutation execution"
            )
        return record

    def find_for_approval(self, approval_id: UUID) -> MutationExecutionRecord | None:
        with self._database.session() as session:
            row = session.scalar(
                select(MutationExecutionRow).where(
                    MutationExecutionRow.approval_id == str(approval_id)
                )
            )
            return self._to_domain(row) if row is not None else None

    def list_for_run(self, run_id: UUID) -> list[MutationExecutionRecord]:
        with self._database.session() as session:
            rows = session.scalars(
                select(MutationExecutionRow)
                .where(MutationExecutionRow.run_id == str(run_id))
                .order_by(MutationExecutionRow.created_at, MutationExecutionRow.execution_id)
            ).all()
            return [self._to_domain(row) for row in rows]

    @staticmethod
    def _to_row(record: MutationExecutionRecord) -> MutationExecutionRow:
        return MutationExecutionRow(
            execution_id=str(record.execution_id),
            run_id=str(record.run_id),
            approval_id=str(record.approval_id),
            tool_call_digest=record.tool_call_digest,
            tool_name=record.tool_name,
            target_path=record.target_path,
            before_sha256=record.before_sha256,
            expected_after_sha256=record.expected_after_sha256,
            before_workspace_digest=record.before_workspace_digest,
            expected_after_workspace_digest=record.expected_after_workspace_digest,
            actual_after_sha256=record.actual_after_sha256,
            bytes_written=record.bytes_written,
            status=record.status.value,
            result_summary=record.result_summary,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _to_domain(row: MutationExecutionRow) -> MutationExecutionRecord:
        return MutationExecutionRecord(
            execution_id=UUID(row.execution_id),
            run_id=UUID(row.run_id),
            approval_id=UUID(row.approval_id),
            tool_call_digest=row.tool_call_digest,
            tool_name=row.tool_name,
            target_path=row.target_path,
            before_sha256=row.before_sha256,
            expected_after_sha256=row.expected_after_sha256,
            before_workspace_digest=row.before_workspace_digest,
            expected_after_workspace_digest=row.expected_after_workspace_digest,
            actual_after_sha256=row.actual_after_sha256,
            bytes_written=row.bytes_written,
            status=MutationExecutionStatus(row.status),
            result_summary=row.result_summary,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
