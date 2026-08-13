from dataclasses import dataclass, field
from typing import Literal, final
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy.orm import Session

from agentforge.application.kernel_errors import SourceRevisionConflictError
from agentforge.domain.enums import MutationExecutionStatus, ToolErrorCode
from agentforge.domain.errors import ToolRuntimeError
from agentforge.domain.models import ToolResult
from agentforge.domain.mutations import (
    MutationApprovalBinding,
    MutationExecutionRecord,
)
from agentforge.persistence.event_log import RunLeaseAuthority
from agentforge.persistence.mutation_workflow import MutationWorkflow
from agentforge.persistence.mutations import (
    MutationApprovalBindingRepository,
    MutationExecutionRepository,
)
from agentforge.persistence.source_revisions import (
    MutationRecoveryAction,
    WorkspaceDigester,
    source_revision_audit_from_summary,
)
from agentforge.tools.mutation.atomic import file_sha256
from agentforge.tools.mutation.security import MutationSecurityPolicy


class MutationOutcomeIndeterminateError(RuntimeError):
    """Signals that Runtime must stop after an uncertain mutation."""


@final
@dataclass(frozen=True, slots=True)
class EvaluatorOnlyUnboundSourcePolicy:
    """Explicit legacy adapter for evaluator Runs without product source bindings.

    This policy never claims to validate the actual workspace revision. A2 product
    assembly must not inject it; the evaluator seam is scheduled for removal in A2.
    """

    source_revision_semantics: Literal["UNBOUND_EVALUATOR_ONLY"] = field(
        default="UNBOUND_EVALUATOR_ONLY", init=False
    )
    source_verified: Literal[False] = field(default=False, init=False)


class MutationCoordinator:
    def __init__(
        self,
        bindings: MutationApprovalBindingRepository,
        executions: MutationExecutionRepository,
        workflow: MutationWorkflow,
        security: MutationSecurityPolicy,
        digester: WorkspaceDigester | None = None,
        source_policy: EvaluatorOnlyUnboundSourcePolicy | None = None,
    ) -> None:
        self._bindings = bindings
        self._executions = executions
        self._workflow = workflow
        self._security = security
        self._digester = digester or WorkspaceDigester()
        self._source_policy = source_policy

    def binding_for_approval(self, approval_id: UUID) -> MutationApprovalBinding | None:
        return self._bindings.find_for_approval(approval_id)

    def ensure_prepared(
        self, approval_id: UUID, *, authority: RunLeaseAuthority
    ) -> MutationExecutionRecord:
        binding = self._bindings.get_for_approval(approval_id)
        source_bound = self._require_source_policy(binding.run_id)
        existing = self._executions.find_for_approval(approval_id)
        if existing is not None:
            return existing
        # For the evaluator-only adapter these are plan digests, not a claim that the
        # workspace has an authoritative source revision.
        snapshot = self._digester.snapshot(self._security.resolver.workspace)
        expected_after = self._digester.project_digest(
            snapshot,
            relative_path=binding.target_path,
            size_bytes=binding.bytes_written,
            content_sha256=binding.expected_after_sha256,
        )
        return self._workflow.ensure_prepared(
            approval_id,
            before_workspace_digest=snapshot.digest,
            expected_after_workspace_digest=expected_after,
            actual_digest_verified=source_bound,
            authority=authority,
        )

    def _evaluator_only_ensure_prepared(
        self, approval_id: UUID
    ) -> MutationExecutionRecord:
        binding = self._bindings.get_for_approval(approval_id)
        source_bound = self._require_source_policy(binding.run_id)
        existing = self._executions.find_for_approval(approval_id)
        if existing is not None:
            return existing
        snapshot = self._digester.snapshot(self._security.resolver.workspace)
        expected_after = self._digester.project_digest(
            snapshot,
            relative_path=binding.target_path,
            size_bytes=binding.bytes_written,
            content_sha256=binding.expected_after_sha256,
        )
        return self._workflow._evaluator_only_ensure_prepared(
            approval_id,
            before_workspace_digest=snapshot.digest,
            expected_after_workspace_digest=expected_after,
            actual_digest_verified=source_bound,
        )

    def claim_resume(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        authority: RunLeaseAuthority,
    ) -> bool:
        record = self._executions.get_for_approval(approval_id)
        source_bound = self._require_source_policy(run_id)
        actual = (
            self._digester.digest(self._security.resolver.workspace)
            if source_bound
            else record.before_workspace_digest
        )
        return self._workflow.claim_resume(
            run_id,
            approval_id,
            actual_workspace_digest=actual,
            workspace_root_identity=str(self._security.resolver.workspace),
            authority=authority,
        )

    def claim_resume_in_session(
        self,
        session: Session,
        run_id: UUID,
        approval_id: UUID,
        authority: RunLeaseAuthority,
    ) -> None:
        source_bound = self._require_source_policy(run_id)
        snapshot = self._digester.snapshot(self._security.resolver.workspace)
        actual = snapshot.digest
        binding = self._bindings.get_for_approval(approval_id)
        expected_after = self._digester.project_digest(
            snapshot,
            relative_path=binding.target_path,
            size_bytes=binding.bytes_written,
            content_sha256=binding.expected_after_sha256,
        )
        self._workflow.ensure_prepared_in_session(
            session,
            approval_id,
            before_workspace_digest=actual,
            expected_after_workspace_digest=expected_after,
            actual_digest_verified=source_bound,
            authority=authority,
        )
        self._workflow.claim_resume_in_session(
            session,
            run_id,
            approval_id,
            actual_workspace_digest=actual,
            workspace_root_identity=str(self._security.resolver.workspace),
            authority=authority,
        )

    def get_execution(self, execution_id: UUID) -> MutationExecutionRecord:
        return self._executions.get(execution_id)

    def get_for_approval(self, approval_id: UUID) -> MutationExecutionRecord:
        return self._executions.get_for_approval(approval_id)

    def list_executions(self, run_id: UUID) -> list[MutationExecutionRecord]:
        return self._executions.list_for_run(run_id)

    def record_result(
        self,
        run_id: UUID,
        approval_id: UUID,
        result: ToolResult,
        *,
        duration_ms: int,
        authority: RunLeaseAuthority,
    ) -> MutationExecutionRecord:
        source_bound = self._require_source_policy(run_id)
        if not result.success:
            if result.error_type is ToolErrorCode.MUTATION_INDETERMINATE:
                self._workflow.mark_indeterminate(
                    run_id,
                    approval_id,
                    result.error_message or "Mutation outcome is uncertain",
                    authority=authority,
                )
                raise MutationOutcomeIndeterminateError
            return self._workflow.mark_failed(
                run_id,
                approval_id,
                result_summary=result.error_message or "Mutation failed",
                duration_ms=duration_ms,
                authority=authority,
            )
        after = result.metadata.get("after_sha256")
        bytes_written = result.metadata.get("bytes_written")
        if not isinstance(after, str) or not isinstance(bytes_written, int):
            self._workflow.mark_indeterminate(
                run_id,
                approval_id,
                "Successful mutation returned incomplete durable metadata",
                authority=authority,
            )
            raise MutationOutcomeIndeterminateError
        try:
            record = self._executions.get_for_approval(approval_id)
            actual_workspace_digest = (
                self._digester.digest(self._security.resolver.workspace)
                if source_bound
                else record.expected_after_workspace_digest
            )
            return self._workflow.mark_committed(
                run_id,
                approval_id,
                actual_after_sha256=after,
                actual_workspace_digest=actual_workspace_digest,
                bytes_written=bytes_written,
                duration_ms=duration_ms,
                authority=authority,
            )
        except SourceRevisionConflictError:
            self._workflow.mark_indeterminate(
                run_id,
                approval_id,
                "Workspace changed outside the prepared mutation",
                authority=authority,
            )
            raise MutationOutcomeIndeterminateError from None

    def recover_claimed(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        authority: RunLeaseAuthority,
    ) -> ToolResult | None:
        record = self._executions.get_for_approval(approval_id)
        actual_workspace_digest: str | None
        source_bound = self._require_source_policy(run_id)
        if record.run_id != run_id:
            raise ValueError("Mutation execution belongs to another Run")
        if record.status is MutationExecutionStatus.WRITING:
            if not source_bound:
                self._workflow.mark_indeterminate(
                    run_id,
                    approval_id,
                    "Process stopped while mutation status was WRITING",
                    authority=authority,
                )
                return None
            actual_workspace_digest = self._digester.digest(self._security.resolver.workspace)
            action = self._workflow.recover_writing(
                run_id,
                approval_id,
                actual_workspace_digest=actual_workspace_digest,
                workspace_root_identity=str(self._security.resolver.workspace),
                authority=authority,
            )
            if action is not MutationRecoveryAction.FINALIZE:
                return None
            record = self._executions.get_for_approval(approval_id)
        if record.status is MutationExecutionStatus.INDETERMINATE:
            return None
        if record.status is MutationExecutionStatus.FAILED:
            return ToolResult(
                success=False,
                error_type=ToolErrorCode.TOOL_EXECUTION_ERROR,
                error_message=record.result_summary or "Mutation failed",
                metadata=self._result_metadata(record),
            )
        if record.status is not MutationExecutionStatus.COMMITTED:
            raise ValueError("Claimed mutation has no recoverable execution result")
        try:
            target = self._security.resolve_target(record.target_path)
            actual = file_sha256(target)
            actual_workspace_digest = self._digester.digest(self._security.resolver.workspace)
        except ToolRuntimeError:
            actual = None
            actual_workspace_digest = None
        if actual != record.expected_after_sha256 or (
            source_bound and actual_workspace_digest != record.expected_after_workspace_digest
        ):
            self._workflow.mark_indeterminate(
                run_id,
                approval_id,
                "Committed mutation target changed before recovery",
                authority=authority,
            )
            return None
        return ToolResult(
            success=True,
            output={"path": record.target_path, "status": "written"},
            metadata=self._result_metadata(record),
        )

    def _require_source_policy(self, run_id: UUID) -> bool:
        if self._workflow.has_source_revision(run_id):
            return True
        if type(self._source_policy) is EvaluatorOnlyUnboundSourcePolicy:
            return False
        raise SourceRevisionConflictError()

    @staticmethod
    def _result_metadata(record: MutationExecutionRecord) -> dict[str, JsonValue]:
        semantics, verified = source_revision_audit_from_summary(record.result_summary)
        return {
            "path": record.target_path,
            "before_sha256": record.before_sha256,
            "after_sha256": record.actual_after_sha256,
            "bytes_written": record.bytes_written,
            "operation": record.tool_name,
            "source_revision_semantics": semantics,
            "source_verified": verified,
        }
