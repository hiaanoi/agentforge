from datetime import timedelta
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agentforge.application.contracts import (
    RunControlRequestStatus,
    RuntimeTrustClass,
    VerificationCapsuleState,
)
from agentforge.application.kernel_errors import SourceRevisionConflictError
from agentforge.domain.enums import (
    ApprovalConsumptionState,
    ApprovalStatus,
    EventType,
    ProcessExecutionStatus,
    ProcessFailureKind,
    RunFailureCode,
    RunStatus,
    ToolErrorCode,
)
from agentforge.domain.errors import (
    ResumeNotAllowedError,
    TestApprovalBindingNotFoundError,
)
from agentforge.domain.models import utc_now
from agentforge.domain.repair import RepairCompletionStatus, RepairTerminationReason
from agentforge.domain.test_execution import UNBOUND_SOURCE_REVISION_DIGEST, ProcessExecutionRecord
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import EventLog, RunLeaseAuthority
from agentforge.persistence.product_tables import RunControlRequestRow, WorkspaceSourceBindingRow
from agentforge.persistence.repair_terminal import (
    RepairStateProvenance,
    terminalize_repair_in_session,
)
from agentforge.persistence.run_leases import RunLeaseStore, claim_bound_write
from agentforge.persistence.tables import (
    ApprovalRequestRow,
    ProcessExecutionRow,
    RepairTaskPolicyRow,
    RunRow,
    TestApprovalBindingRow,
)
from agentforge.persistence.test_executions import ProcessExecutionRepository


class TestExecutionWorkflow:
    def __init__(self, database: Database) -> None:
        self._initialize(database, RepairStateProvenance.PRODUCT_BUNDLE)

    @classmethod
    def _evaluator_only_create(
        cls, database: Database
    ) -> "TestExecutionWorkflow":
        workflow = cls.__new__(cls)
        workflow._initialize(database, RepairStateProvenance.EVALUATOR_LEGACY)
        return workflow

    def _initialize(
        self,
        database: Database,
        provenance: RepairStateProvenance,
    ) -> None:
        if type(provenance) is not RepairStateProvenance:
            raise TypeError("RepairState provenance must be an exact enum value")
        self._database = database
        self._repair_state_provenance = provenance

    def __setattr__(self, name: str, value: object) -> None:
        if name == "_repair_state_provenance" and hasattr(self, name):
            raise AttributeError("RepairState provenance is immutable")
        object.__setattr__(self, name, value)

    @property
    def database(self) -> Database:
        return self._database

    def _evaluator_only_ensure_created(
        self, approval_id: UUID
    ) -> ProcessExecutionRecord:
        with self._database.session() as session:
            binding = session.get(TestApprovalBindingRow, str(approval_id))
            if binding is None:
                raise TestApprovalBindingNotFoundError(
                    f"Approval {approval_id} has no test-profile binding"
                )
            run_id = UUID(binding.run_id)
        leases = RunLeaseStore(self._database)
        lease = leases.acquire(
            run_id,
            owner_id=f"legacy-evaluator:test-create:{uuid4()}",
            ttl=timedelta(seconds=30),
        )
        try:
            return self.ensure_created(approval_id, authority=lease.authority)
        finally:
            leases.release(lease.authority)

    def ensure_created(
        self, approval_id: UUID, *, authority: RunLeaseAuthority
    ) -> ProcessExecutionRecord:
        try:
            with self._database.session() as session:
                existing = session.scalar(
                    select(ProcessExecutionRow).where(
                        ProcessExecutionRow.approval_id == str(approval_id)
                    )
                )
                if existing is not None:
                    return ProcessExecutionRepository._to_domain(existing)
                approval = session.get(ApprovalRequestRow, str(approval_id))
                binding = session.get(TestApprovalBindingRow, str(approval_id))
                if binding is None:
                    raise TestApprovalBindingNotFoundError(
                        f"Approval {approval_id} has no test-profile binding"
                    )
                claim_bound_write(session, UUID(binding.run_id), authority)
                if approval is None or approval.status != ApprovalStatus.APPROVED.value:
                    raise ResumeNotAllowedError(
                        "Only an approved test profile can create an execution"
                    )
                maximum = session.scalar(
                    select(func.max(ProcessExecutionRow.attempt_number)).where(
                        ProcessExecutionRow.run_id == binding.run_id
                    )
                )
                record = ProcessExecutionRecord(
                    run_id=UUID(binding.run_id),
                    approval_id=approval_id,
                    tool_call_digest=binding.tool_call_digest,
                    attempt_number=(maximum or 0) + 1,
                    profile_id=binding.profile_id,
                    profile_version=binding.profile_version,
                    profile_digest=binding.profile_digest,
                    executable_path=binding.executable_path,
                    argv_digest=binding.argv_digest,
                    cwd=binding.cwd,
                    environment_digest=binding.environment_digest,
                    source_revision_number=binding.source_revision_number,
                    source_revision_digest=binding.source_revision_digest,
                )
                session.add(ProcessExecutionRepository._to_row(record))
                session.flush()
                return record
        except IntegrityError:
            return ProcessExecutionRepository(self._database).get_for_approval(approval_id)

    def claim_resume(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        authority: RunLeaseAuthority,
    ) -> bool:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            approval_changed = ApprovalWorkflow._affected_rows(
                session.execute(
                    update(ApprovalRequestRow)
                    .where(
                        ApprovalRequestRow.approval_id == str(approval_id),
                        ApprovalRequestRow.run_id == str(run_id),
                        ApprovalRequestRow.status == ApprovalStatus.APPROVED.value,
                        ApprovalRequestRow.consumption_state
                        == ApprovalConsumptionState.NOT_STARTED.value,
                    )
                    .values(consumption_state=ApprovalConsumptionState.CLAIMED.value)
                )
            )
            if approval_changed != 1:
                return False
            now = utc_now()
            execution_changed = ApprovalWorkflow._affected_rows(
                session.execute(
                    update(ProcessExecutionRow)
                    .where(
                        ProcessExecutionRow.approval_id == str(approval_id),
                        ProcessExecutionRow.run_id == str(run_id),
                        ProcessExecutionRow.status == ProcessExecutionStatus.CREATED.value,
                    )
                    .values(
                        status=ProcessExecutionStatus.STARTED.value,
                        record_version=ProcessExecutionRow.record_version + 1,
                        updated_at=now,
                    )
                )
            )
            if execution_changed != 1:
                raise ResumeNotAllowedError("Test execution is not in CREATED state")
            run_changed = ApprovalWorkflow._affected_rows(
                session.execute(
                    update(RunRow)
                    .where(
                        RunRow.run_id == str(run_id),
                        RunRow.status == RunStatus.PAUSED.value,
                        RunRow.tool_call_count < RunRow.max_tool_calls,
                    )
                    .values(
                        status=RunStatus.RUNNING.value,
                        tool_call_count=RunRow.tool_call_count + 1,
                        updated_at=now,
                    )
                )
            )
            if run_changed != 1:
                raise ResumeNotAllowedError(
                    "Test execution cannot start because the tool budget is exhausted"
                )
            record = self._record_for_approval(session, approval_id)
            EventLog().append(
                session,
                authority,
                EventType.RUN_RESUMED,
                {"approval_id": str(approval_id), "phase": "TEST_EXECUTION"},
            )
            EventLog().append(
                session,
                authority,
                EventType.TOOL_STARTED,
                {"tool_name": "run_tests", "managed_execution": True},
            )
            EventLog().append(
                session,
                authority,
                EventType.TEST_STARTED,
                self._event_payload(record),
            )
            return True

    def claim_resume_in_session(
        self,
        session: Session,
        run_id: UUID,
        approval_id: UUID,
        *,
        authority: RunLeaseAuthority,
    ) -> None:
        """Atomically claim Approval+Process; outer Resume owns Run/Event/Receipt."""
        claim_bound_write(session, run_id, authority)
        execution = session.scalar(
            select(ProcessExecutionRow).where(
                ProcessExecutionRow.approval_id == str(approval_id)
            )
        )
        if execution is None:
            binding = session.get(TestApprovalBindingRow, str(approval_id))
            approval = session.get(ApprovalRequestRow, str(approval_id))
            if (
                binding is None
                or binding.run_id != str(run_id)
                or approval is None
                or approval.status != ApprovalStatus.APPROVED.value
            ):
                raise ResumeNotAllowedError(
                    "Approved test execution cannot be created atomically"
                )
            maximum = session.scalar(
                select(func.max(ProcessExecutionRow.attempt_number)).where(
                    ProcessExecutionRow.run_id == str(run_id)
                )
            )
            record = ProcessExecutionRecord(
                run_id=run_id,
                approval_id=approval_id,
                tool_call_digest=binding.tool_call_digest,
                attempt_number=(maximum or 0) + 1,
                profile_id=binding.profile_id,
                profile_version=binding.profile_version,
                profile_digest=binding.profile_digest,
                executable_path=binding.executable_path,
                argv_digest=binding.argv_digest,
                cwd=binding.cwd,
                environment_digest=binding.environment_digest,
                source_revision_number=binding.source_revision_number,
                source_revision_digest=binding.source_revision_digest,
            )
            session.add(ProcessExecutionRepository._to_row(record))
            session.flush()
        approval_changed = ApprovalWorkflow._affected_rows(
            session.execute(
                update(ApprovalRequestRow)
                .where(
                    ApprovalRequestRow.approval_id == str(approval_id),
                    ApprovalRequestRow.run_id == str(run_id),
                    ApprovalRequestRow.status == ApprovalStatus.APPROVED.value,
                    ApprovalRequestRow.consumption_state
                    == ApprovalConsumptionState.NOT_STARTED.value,
                )
                .values(consumption_state=ApprovalConsumptionState.CLAIMED.value)
            )
        )
        if approval_changed != 1:
            raise ResumeNotAllowedError("Test approval could not be claimed")
        now = utc_now()
        execution_changed = ApprovalWorkflow._affected_rows(
            session.execute(
                update(ProcessExecutionRow)
                .where(
                    ProcessExecutionRow.approval_id == str(approval_id),
                    ProcessExecutionRow.run_id == str(run_id),
                    ProcessExecutionRow.status == ProcessExecutionStatus.CREATED.value,
                )
                .values(
                    status=ProcessExecutionStatus.STARTED.value,
                    record_version=ProcessExecutionRow.record_version + 1,
                    updated_at=now,
                )
            )
        )
        if execution_changed != 1:
            raise ResumeNotAllowedError("Test execution is not in CREATED state")
        budget_changed = ApprovalWorkflow._affected_rows(
            session.execute(
                update(RunRow)
                .where(
                    RunRow.run_id == str(run_id),
                    RunRow.status == RunStatus.PAUSED.value,
                    RunRow.tool_call_count < RunRow.max_tool_calls,
                )
                .values(tool_call_count=RunRow.tool_call_count + 1, updated_at=now)
            )
        )
        if budget_changed != 1:
            raise ResumeNotAllowedError(
                "Test execution cannot start because the tool budget is exhausted"
            )
        record = self._record_for_approval(session, approval_id)
        events = EventLog()
        events.append(
            session,
            authority,
            EventType.TOOL_STARTED,
            {"tool_name": "run_tests", "managed_execution": True},
        )
        events.append(
            session,
            authority,
            EventType.TEST_STARTED,
            self._event_payload(record),
        )

    def finish(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        expected_version: int,
        status: ProcessExecutionStatus,
        failure_kind: ProcessFailureKind | None,
        exit_code: int | None,
        stdout_digest: str,
        stderr_digest: str,
        stdout_size: int,
        stderr_size: int,
        stdout_summary: str,
        stderr_summary: str,
        stdout_truncated: bool,
        stderr_truncated: bool,
        duration_ms: int,
        termination_reason: str | None,
        termination_result: str | None,
        root_pid: int | None = None,
        process_group_id: int | None = None,
        job_id: str | None = None,
        source_digest_after: str | None = None,
        source_digest_current: str | None = None,
        authority: RunLeaseAuthority,
    ) -> ProcessExecutionRecord:
        if status in {ProcessExecutionStatus.CREATED, ProcessExecutionStatus.STARTED}:
            raise ValueError("Process execution must finish in a terminal status")
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            execution = session.scalar(
                select(ProcessExecutionRow).where(
                    ProcessExecutionRow.approval_id == str(approval_id),
                    ProcessExecutionRow.run_id == str(run_id),
                )
            )
            binding = session.get(TestApprovalBindingRow, str(approval_id))
            source = session.get(WorkspaceSourceBindingRow, str(run_id))
            source_bound = False
            source_matches = False
            if execution is not None and binding is not None:
                # The live Run fence serializes AgentForge-managed mutation/test writes;
                # the authoritative revision row is re-read in this same terminal
                # transaction to close the managed-writer boundary.
                source_bound = (
                    execution.source_revision_digest
                    != UNBOUND_SOURCE_REVISION_DIGEST
                )
                source_matches = (
                    source_bound
                    and source is not None
                    and source_digest_after is not None
                    and source_digest_current is not None
                    and execution.source_digest_at_start is not None
                    and execution.source_revision_number
                    == binding.source_revision_number
                    == source.source_revision_number
                    and execution.source_revision_digest
                    == binding.source_revision_digest
                    == source.expected_source_digest
                    == execution.source_digest_at_start
                    == source_digest_after
                    == source_digest_current
                )
                if execution.capsule_state == VerificationCapsuleState.SEALED.value:
                    source_matches = bool(
                        source_matches
                        and execution.source_snapshot_digest
                        == execution.source_digest_at_start
                        and execution.verifier_artifact_digest is not None
                        and execution.executable_artifact_digest is not None
                        and execution.runtime_trust_class
                        == RuntimeTrustClass.NON_HERMETIC.value
                    )
            if source_bound and not source_matches:
                status = ProcessExecutionStatus.INDETERMINATE
                failure_kind = ProcessFailureKind.INDETERMINATE
                exit_code = None
                termination_result = ToolErrorCode.SOURCE_REVISION_MISMATCH.value
            changed = ApprovalWorkflow._affected_rows(
                session.execute(
                    update(ProcessExecutionRow)
                    .where(
                        ProcessExecutionRow.approval_id == str(approval_id),
                        ProcessExecutionRow.run_id == str(run_id),
                        ProcessExecutionRow.status == ProcessExecutionStatus.STARTED.value,
                        ProcessExecutionRow.record_version == expected_version,
                    )
                    .values(
                        status=status.value,
                        failure_kind=failure_kind.value if failure_kind else None,
                        exit_code=exit_code,
                        stdout_digest=stdout_digest,
                        stderr_digest=stderr_digest,
                        stdout_size=stdout_size,
                        stderr_size=stderr_size,
                        stdout_summary=stdout_summary[:20_000],
                        stderr_summary=stderr_summary[:20_000],
                        stdout_truncated=stdout_truncated,
                        stderr_truncated=stderr_truncated,
                        duration_ms=duration_ms,
                        termination_reason=(
                            termination_reason[:100] if termination_reason else None
                        ),
                        termination_result=(
                            termination_result[:500] if termination_result else None
                        ),
                        root_pid=root_pid,
                        process_group_id=process_group_id,
                        job_id=job_id,
                        record_version=ProcessExecutionRow.record_version + 1,
                        updated_at=utc_now(),
                    )
                )
            )
            if changed != 1:
                raise ResumeNotAllowedError(
                    "Process execution terminal fact lost its version claim"
                )
            record = self._record_for_approval(session, approval_id)
            EventLog().append(
                session,
                authority,
                self._terminal_event(status),
                self._event_payload(record),
            )
            tool_succeeded = status in {
                ProcessExecutionStatus.COMPLETED,
                ProcessExecutionStatus.TIMEOUT,
            } or (
                status is ProcessExecutionStatus.FAILED
                and failure_kind is ProcessFailureKind.TEST_FAILURE
            )
            EventLog().append(
                session,
                authority,
                EventType.TOOL_COMPLETED if tool_succeeded else EventType.TOOL_FAILED,
                {
                    "tool_name": "run_tests",
                    "success": tool_succeeded,
                    "error_type": (
                        None
                        if tool_succeeded
                        else (
                            failure_kind.value
                            if failure_kind is not None
                            else ToolErrorCode.TOOL_EXECUTION_ERROR.value
                        )
                    ),
                    "duration_ms": duration_ms,
                    "truncated": stdout_truncated or stderr_truncated,
                    "managed_execution": True,
                },
            )
            return record

    def source_binding(self, run_id: UUID) -> tuple[int, str]:
        """Read the durable revision to freeze into an approval binding."""
        with self._database.session() as session:
            source = session.get(WorkspaceSourceBindingRow, str(run_id))
            if source is None:
                raise SourceRevisionConflictError()
            return source.source_revision_number, source.expected_source_digest

    def begin_verification_capsule(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        capsule_id: UUID,
        authority: RunLeaseAuthority,
    ) -> ProcessExecutionRecord:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            changed = ApprovalWorkflow._affected_rows(
                session.execute(
                    update(ProcessExecutionRow)
                    .where(
                        ProcessExecutionRow.run_id == str(run_id),
                        ProcessExecutionRow.approval_id == str(approval_id),
                        ProcessExecutionRow.status == ProcessExecutionStatus.STARTED.value,
                        ProcessExecutionRow.verification_capsule_id.is_(None),
                    )
                    .values(
                        verification_capsule_id=str(capsule_id),
                        capsule_state=VerificationCapsuleState.STAGING.value,
                        record_version=ProcessExecutionRow.record_version + 1,
                        updated_at=utc_now(),
                    )
                )
            )
            if changed != 1:
                raise ResumeNotAllowedError("Verification capsule cannot enter STAGING")
            return self._record_for_approval(session, approval_id)

    def seal_verification_capsule(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        capsule_id: UUID,
        source_digest: str,
        verifier_digest: str,
        executable_digest: str,
        artifact_algorithm_version: int,
        workspace_root_identity: str,
        authority: RunLeaseAuthority,
    ) -> ProcessExecutionRecord:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            binding = session.get(TestApprovalBindingRow, str(approval_id))
            source = session.get(WorkspaceSourceBindingRow, str(run_id))
            execution = session.scalar(
                select(ProcessExecutionRow).where(
                    ProcessExecutionRow.run_id == str(run_id),
                    ProcessExecutionRow.approval_id == str(approval_id),
                )
            )
            if (
                binding is None
                or source is None
                or execution is None
                or execution.status != ProcessExecutionStatus.STARTED.value
                or execution.verification_capsule_id != str(capsule_id)
                or execution.capsule_state != VerificationCapsuleState.STAGING.value
                or source.workspace_root_identity != workspace_root_identity
                or not (
                    execution.source_revision_number
                    == binding.source_revision_number
                    == source.source_revision_number
                )
                or not (
                    execution.source_revision_digest
                    == binding.source_revision_digest
                    == source.expected_source_digest
                    == source_digest
                )
                or execution.profile_digest != binding.profile_digest
                or artifact_algorithm_version < 1
            ):
                raise ResumeNotAllowedError(ToolErrorCode.SOURCE_REVISION_MISMATCH.value)
            execution.source_digest_at_start = source_digest
            execution.source_snapshot_digest = source_digest
            execution.verifier_artifact_digest = verifier_digest
            execution.executable_artifact_digest = executable_digest
            execution.artifact_algorithm_version = artifact_algorithm_version
            execution.runtime_trust_class = RuntimeTrustClass.NON_HERMETIC.value
            execution.capsule_state = VerificationCapsuleState.SEALED.value
            execution.record_version += 1
            execution.updated_at = utc_now()
            return self._record_for_approval(session, approval_id)

    def _evaluator_only_source_binding(self, run_id: UUID) -> tuple[int, str]:
        """Explicit legacy seam; verification purpose rejects its sentinel later."""
        try:
            return self.source_binding(run_id)
        except SourceRevisionConflictError:
            return 0, UNBOUND_SOURCE_REVISION_DIGEST

    def require_source_at_start(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        actual_digest: str,
        workspace_root_identity: str,
        authority: RunLeaseAuthority,
    ) -> None:
        """Bind the launch to the recorded approval revision under fencing."""
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            binding = session.get(TestApprovalBindingRow, str(approval_id))
            execution = session.scalar(
                select(ProcessExecutionRow).where(
                    ProcessExecutionRow.approval_id == str(approval_id)
                )
            )
            source = session.get(WorkspaceSourceBindingRow, str(run_id))
            if binding is None or execution is None:
                raise ResumeNotAllowedError("Test source binding is missing")
            if binding.source_revision_digest == UNBOUND_SOURCE_REVISION_DIGEST:
                return
            if (
                source is None
                or source.workspace_root_identity != workspace_root_identity
                or binding.source_revision_number != source.source_revision_number
                or execution.source_revision_number != binding.source_revision_number
                or execution.source_revision_digest != binding.source_revision_digest
                or binding.source_revision_digest != source.expected_source_digest
                or actual_digest != source.expected_source_digest
            ):
                raise ResumeNotAllowedError(
                    ToolErrorCode.SOURCE_REVISION_MISMATCH.value
                )
            if execution.source_digest_at_start is None:
                execution.source_digest_at_start = actual_digest
                execution.record_version += 1
                execution.updated_at = utc_now()
            elif execution.source_digest_at_start != actual_digest:
                raise ResumeNotAllowedError(
                    ToolErrorCode.SOURCE_REVISION_MISMATCH.value
                )

    def fail_before_start(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        reason: str,
        authority: RunLeaseAuthority,
    ) -> ProcessExecutionRecord:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            now = utc_now()
            changed = ApprovalWorkflow._affected_rows(
                session.execute(
                    update(ProcessExecutionRow)
                    .where(
                        ProcessExecutionRow.approval_id == str(approval_id),
                        ProcessExecutionRow.run_id == str(run_id),
                        ProcessExecutionRow.status == ProcessExecutionStatus.CREATED.value,
                    )
                    .values(
                        status=ProcessExecutionStatus.FAILED.value,
                        failure_kind=ProcessFailureKind.PROFILE_MISMATCH.value,
                        termination_result=reason[:500],
                        record_version=ProcessExecutionRow.record_version + 1,
                        updated_at=now,
                    )
                )
            )
            if changed != 1:
                raise ResumeNotAllowedError("Only a CREATED test execution can fail preflight")
            approval_changed = ApprovalWorkflow._affected_rows(
                session.execute(
                    update(ApprovalRequestRow)
                    .where(
                        ApprovalRequestRow.approval_id == str(approval_id),
                        ApprovalRequestRow.run_id == str(run_id),
                        ApprovalRequestRow.consumption_state
                        == ApprovalConsumptionState.NOT_STARTED.value,
                    )
                    .values(
                        consumption_state=ApprovalConsumptionState.CONSUMED.value,
                        result_status="failed",
                        result_summary="Test profile binding changed before execution",
                        consumed_at=now,
                    )
                )
            )
            if approval_changed != 1:
                raise ResumeNotAllowedError("Test approval cannot fail before start")
            run_changed = ApprovalWorkflow._affected_rows(
                session.execute(
                    update(RunRow)
                    .where(
                        RunRow.run_id == str(run_id),
                        RunRow.status == RunStatus.PAUSED.value,
                    )
                    .values(
                        status=RunStatus.FAILED.value,
                        error_message="Test profile binding changed before execution",
                        updated_at=now,
                    )
                )
            )
            if run_changed != 1:
                raise ResumeNotAllowedError("Test Run cannot fail profile validation")
            record = self._record_for_approval(session, approval_id)
            binding = session.get(TestApprovalBindingRow, str(approval_id))
            policy = session.get(RepairTaskPolicyRow, str(run_id))
            final_profile = (
                binding is not None
                and policy is not None
                and policy.policy_data.get("final_verification_profile_id")
                == binding.profile_id
            )
            EventLog().append(
                session,
                authority,
                EventType.TEST_FAILED,
                self._event_payload(record),
            )
            EventLog().append(
                session,
                authority,
                EventType.RUN_FAILED,
                {"code": RunFailureCode.TEST_PROFILE_MISMATCH.value},
            )
            terminalize_repair_in_session(
                session,
                run_id,
                status=(
                    RepairCompletionStatus.FINAL_VERIFICATION_FAILED
                    if final_profile
                    else RepairCompletionStatus.TESTS_FAILED
                ),
                reason=(
                    RepairTerminationReason.FINAL_HIDDEN_TEST_FAILED
                    if final_profile
                    else RepairTerminationReason.DEVELOPMENT_TEST_FAILED
                ),
                at=now,
                provenance=self._repair_state_provenance,
            )
            return record

    def mark_indeterminate(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        reason: str,
        authority: RunLeaseAuthority,
    ) -> ProcessExecutionRecord:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            now = utc_now()
            changed = ApprovalWorkflow._affected_rows(
                session.execute(
                    update(ProcessExecutionRow)
                    .where(
                        ProcessExecutionRow.approval_id == str(approval_id),
                        ProcessExecutionRow.run_id == str(run_id),
                        ProcessExecutionRow.status == ProcessExecutionStatus.STARTED.value,
                    )
                    .values(
                        status=ProcessExecutionStatus.INDETERMINATE.value,
                        failure_kind=ProcessFailureKind.INDETERMINATE.value,
                        termination_result=reason[:500],
                        record_version=ProcessExecutionRow.record_version + 1,
                        updated_at=now,
                    )
                )
            )
            if changed != 1:
                raise ResumeNotAllowedError("Only a STARTED execution can become indeterminate")
            approval_changed = ApprovalWorkflow._affected_rows(
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
                        result_summary=reason[:500],
                    )
                )
            )
            if approval_changed != 1:
                raise ResumeNotAllowedError("Claimed test approval is missing")
            run_changed = ApprovalWorkflow._affected_rows(
                session.execute(
                    update(RunRow)
                    .where(
                        RunRow.run_id == str(run_id),
                        RunRow.status == RunStatus.RUNNING.value,
                    )
                    .values(
                        status=RunStatus.FAILED.value,
                        error_message="Test execution outcome is indeterminate",
                        updated_at=now,
                    )
                )
            )
            if run_changed != 1:
                raise ResumeNotAllowedError("Test Run is not active")
            record = self._record_for_approval(session, approval_id)
            EventLog().append(
                session,
                authority,
                EventType.TEST_INDETERMINATE,
                self._event_payload(record),
            )
            EventLog().append(
                session,
                authority,
                EventType.RUN_FAILED,
                {"code": RunFailureCode.TEST_EXECUTION_INDETERMINATE.value},
            )
            terminalize_repair_in_session(
                session,
                run_id,
                status=RepairCompletionStatus.INDETERMINATE,
                reason=RepairTerminationReason.INDETERMINATE_SIDE_EFFECT,
                at=now,
                provenance=self._repair_state_provenance,
            )
            return record

    def finalize_control_cancel(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        authority: RunLeaseAuthority,
    ) -> None:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            run = session.get(RunRow, str(run_id))
            approval = session.get(ApprovalRequestRow, str(approval_id))
            if run is None or approval is None or approval.run_id != str(run_id):
                raise ResumeNotAllowedError("Cancelled test control state is missing")
            if run.status == RunStatus.CANCELLED.value:
                return
            if run.status != RunStatus.RUNNING.value:
                raise ResumeNotAllowedError("Active test Run cannot be cancelled")
            now = utc_now()
            control = session.scalar(
                select(RunControlRequestRow).where(
                    RunControlRequestRow.run_id == str(run_id),
                    RunControlRequestRow.status == RunControlRequestStatus.REQUESTED.value,
                )
            )
            if control is not None:
                control.status = RunControlRequestStatus.CANCELLED.value
                control.updated_at = now
            approval.status = ApprovalStatus.CANCELLED.value
            approval.consumption_state = ApprovalConsumptionState.CONSUMED.value
            approval.result_status = "cancelled"
            approval.result_summary = "Test execution was cancelled"
            approval.decided_at = approval.decided_at or now
            approval.consumed_at = now
            run.status = RunStatus.CANCELLED.value
            run.updated_at = now
            EventLog().append(
                session,
                authority,
                EventType.RUN_CANCELLED,
                {"reason": "Test execution cancelled"},
            )
            terminalize_repair_in_session(
                session,
                run_id,
                status=RepairCompletionStatus.CANCELLED,
                reason=RepairTerminationReason.CANCELLED,
                at=now,
                provenance=self._repair_state_provenance,
            )

    @staticmethod
    def _record_for_approval(
        session: object,
        approval_id: UUID,
    ) -> ProcessExecutionRecord:
        row = session.scalar(  # type: ignore[attr-defined]
            select(ProcessExecutionRow).where(ProcessExecutionRow.approval_id == str(approval_id))
        )
        if row is None:
            raise ResumeNotAllowedError("Process execution record is missing")
        return ProcessExecutionRepository._to_domain(row)

    @staticmethod
    def _terminal_event(status: ProcessExecutionStatus) -> EventType:
        return {
            ProcessExecutionStatus.COMPLETED: EventType.TEST_COMPLETED,
            ProcessExecutionStatus.FAILED: EventType.TEST_FAILED,
            ProcessExecutionStatus.TIMEOUT: EventType.TEST_TIMEOUT,
            ProcessExecutionStatus.CANCELLED: EventType.TEST_CANCELLED,
            ProcessExecutionStatus.INDETERMINATE: EventType.TEST_INDETERMINATE,
        }[status]

    @staticmethod
    def _event_payload(record: ProcessExecutionRecord) -> dict[str, JsonValue]:
        return {
            "execution_id": str(record.execution_id),
            "run_id": str(record.run_id),
            "approval_id": str(record.approval_id),
            "profile_id": record.profile_id,
            "profile_version": record.profile_version,
            "profile_digest": record.profile_digest,
            "attempt_number": record.attempt_number,
            "status": record.status.value,
            "failure_kind": record.failure_kind.value if record.failure_kind else None,
            "exit_code": record.exit_code,
            "duration_ms": record.duration_ms,
            "stdout_digest": record.stdout_digest,
            "stderr_digest": record.stderr_digest,
            "stdout_size": record.stdout_size,
            "stderr_size": record.stderr_size,
            "truncated": record.stdout_truncated or record.stderr_truncated,
            "termination_result": record.termination_result,
        }
