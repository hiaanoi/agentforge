import asyncio
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from agentforge.application.contracts import ProfilePurpose
from agentforge.application.kernel_errors import (
    ProfileTrustMismatchError,
    WorkspaceDigestError,
)
from agentforge.domain.enums import (
    ProcessExecutionStatus,
    ProcessFailureKind,
    ToolErrorCode,
)
from agentforge.domain.errors import (
    ResumeNotAllowedError,
    TestProfileBindingMismatchError,
    ToolRuntimeError,
)
from agentforge.domain.models import ToolResult
from agentforge.domain.test_execution import (
    UNBOUND_SOURCE_REVISION_DIGEST,
    ProcessExecutionRecord,
    TestApprovalBinding,
    TestProfile,
    TestProfileSummary,
    TestResult,
)
from agentforge.persistence.event_log import RunAuthorityProvider, RunLeaseAuthority
from agentforge.persistence.profile_trust import ProfileKernel
from agentforge.persistence.source_revisions import WorkspaceDigester
from agentforge.persistence.test_execution_workflow import TestExecutionWorkflow
from agentforge.persistence.test_executions import (
    ProcessExecutionRepository,
    TestApprovalBindingRepository,
)
from agentforge.persistence.verification_capsules import (
    VerificationCapsule,
    VerificationCapsuleBuilder,
)
from agentforge.process.base import (
    ProcessTreeSupervisor,
    SupervisorOutcome,
    SupervisorStatus,
)
from agentforge.process.managed import (
    ManagedExecutionKey,
    ManagedExecutionOrigin,
    ManagedTestExecutionCore,
)
from agentforge.process.runner import create_process_tree_supervisor
from agentforge.tools.testing.profiles import TestProfileRegistry


class TestExecutionOutcomeIndeterminateError(RuntimeError):
    """Signals that Runtime must stop after an uncertain process tree."""


class TestExecutionCancelledError(RuntimeError):
    """Signals that a managed test was cancelled and must not continue the model."""


class TestExecutionPreflightFailedError(RuntimeError):
    """Signals a durable prelaunch rejection already finalized by the workflow."""


class TestExecutionCoordinator:
    def __init__(
        self,
        bindings: TestApprovalBindingRepository,
        executions: ProcessExecutionRepository,
        workflow: TestExecutionWorkflow,
        profiles: TestProfileRegistry,
        *,
        supervisor_factory: Callable[[], ProcessTreeSupervisor] = (create_process_tree_supervisor),
        capsule_builder: VerificationCapsuleBuilder | None = None,
        digester: WorkspaceDigester | None = None,
    ) -> None:
        self._bindings = bindings
        self._executions = executions
        self._workflow = workflow
        self._profiles = profiles
        self._profile_kernel = ProfileKernel(workflow.database, profiles)
        self._digester = digester or WorkspaceDigester()
        self._capsules = capsule_builder or VerificationCapsuleBuilder(
            workflow.database.verification_artifact_root,
            digester=self._digester,
        )
        self._managed = ManagedTestExecutionCore(
            supervisor_factory=supervisor_factory,
        )

    def binding_for_approval(self, approval_id: UUID) -> TestApprovalBinding | None:
        return self._bindings.find_for_approval(approval_id)

    def ensure_created(
        self, approval_id: UUID, *, authority: RunLeaseAuthority
    ) -> ProcessExecutionRecord:
        return self._workflow.ensure_created(approval_id, authority=authority)

    def _evaluator_only_ensure_created(
        self, approval_id: UUID
    ) -> ProcessExecutionRecord:
        return self._workflow._evaluator_only_ensure_created(approval_id)

    def claim_resume(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        authority: RunLeaseAuthority,
    ) -> bool:
        return self._workflow.claim_resume(
            run_id, approval_id, authority=authority
        )

    def claim_resume_in_session(
        self,
        session: Session,
        run_id: UUID,
        approval_id: UUID,
        authority: RunLeaseAuthority,
    ) -> None:
        self._workflow.claim_resume_in_session(session, run_id, approval_id, authority=authority)

    def get_execution(self, execution_id: UUID) -> ProcessExecutionRecord:
        return self._executions.get(execution_id)

    def get_for_approval(self, approval_id: UUID) -> ProcessExecutionRecord:
        return self._executions.get_for_approval(approval_id)

    def list_executions(self, run_id: UUID) -> list[ProcessExecutionRecord]:
        return self._executions.list_for_run(run_id)

    def list_profiles(self) -> list[TestProfileSummary]:
        return [
            TestProfileSummary(
                profile_id=profile.profile_id,
                name=profile.name,
                description=profile.description,
                profile_version=profile.profile_version,
                timeout_seconds=profile.timeout_seconds,
                max_output_bytes=profile.max_output_bytes,
                enabled=profile.enabled,
            )
            for profile in self._profiles.list_enabled()
        ]

    def source_binding(self, run_id: UUID) -> tuple[int, str]:
        return self._workflow.source_binding(run_id)

    def _evaluator_only_source_binding(self, run_id: UUID) -> tuple[int, str]:
        return self._workflow._evaluator_only_source_binding(run_id)

    async def execute_approved(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        already_claimed: bool = False,
        expected_purpose: ProfilePurpose = ProfilePurpose.DEVELOPMENT,
        ownership: RunAuthorityProvider,
    ) -> ToolResult:
        binding = self._bindings.get_for_approval(approval_id)
        if not already_claimed:
            self.ensure_created(approval_id, authority=ownership.authority)
        try:
            before = ownership.authority
            profile = await asyncio.to_thread(self._profiles.require_bound, binding)
            self._require_same_ownership(before, ownership.authority)
        except (TestProfileBindingMismatchError, ToolRuntimeError) as exc:
            self._workflow.fail_before_start(
                run_id,
                approval_id,
                reason="Registered test profile does not match approval binding",
                authority=ownership.authority,
            )
            raise TestExecutionPreflightFailedError from exc
        if profile.purpose is not expected_purpose:
            self._workflow.fail_before_start(
                run_id,
                approval_id,
                reason="Test profile purpose does not match execution purpose",
                authority=ownership.authority,
            )
            raise TestExecutionPreflightFailedError
        source_bound = (
            binding.source_revision_digest != UNBOUND_SOURCE_REVISION_DIGEST
        )
        source_required = expected_purpose is ProfilePurpose.VERIFICATION
        if source_bound or source_required:
            try:
                before = ownership.authority
                await asyncio.to_thread(
                    self._profile_kernel.resolve_trusted,
                    binding.profile_id,
                    purpose=expected_purpose,
                )
                self._require_same_ownership(before, ownership.authority)
            except ProfileTrustMismatchError as exc:
                self._workflow.fail_before_start(
                    run_id,
                    approval_id,
                    reason="Test profile has no exact durable trust",
                    authority=ownership.authority,
                )
                raise TestExecutionPreflightFailedError from exc
        if not already_claimed and not self.claim_resume(
            run_id, approval_id, authority=ownership.authority
        ):
            raise ValueError("Test approval could not be claimed")
        if already_claimed:
            claimed = self._executions.get_for_approval(approval_id)
            if claimed.run_id != run_id or claimed.status is not ProcessExecutionStatus.STARTED:
                raise ValueError("Test execution was not atomically claimed")
        if source_required and not source_bound:
            self._workflow.mark_indeterminate(
                run_id,
                approval_id,
                reason=ToolErrorCode.SOURCE_REVISION_MISMATCH.value,
                authority=ownership.authority,
            )
            raise TestExecutionOutcomeIndeterminateError
        capsule: VerificationCapsule | None = None
        launch_profile = profile
        if source_required:
            if profile.verifier_root is None:
                self._workflow.mark_indeterminate(
                    run_id,
                    approval_id,
                    reason="Verification profile has no verifier artifact root",
                    authority=ownership.authority,
                )
                raise TestExecutionOutcomeIndeterminateError
            capsule_id = uuid4()
            execution = self._workflow.begin_verification_capsule(
                run_id,
                approval_id,
                capsule_id=capsule_id,
                authority=ownership.authority,
            )
            try:
                capsule = await self._capture_with_ownership(
                    ownership,
                    execution.execution_id,
                    capsule_id,
                    self._profiles.workspace_root,
                    Path(profile.verifier_root),
                )
                executable_digest = await self._executable_digest_with_ownership(
                    ownership, profile
                )
                if executable_digest != profile.executable_digest:
                    raise WorkspaceDigestError()
                self._workflow.seal_verification_capsule(
                    run_id,
                    approval_id,
                    capsule_id=capsule.capsule_id,
                    source_digest=capsule.source_digest,
                    verifier_digest=capsule.verifier_digest,
                    executable_digest=executable_digest,
                    artifact_algorithm_version=capsule.algorithm_version,
                    workspace_root_identity=str(self._profiles.workspace_root),
                    authority=ownership.authority,
                )
                await self._verify_capsule_with_ownership(ownership, capsule)
                if (
                    await self._executable_digest_with_ownership(ownership, profile)
                    != executable_digest
                ):
                    raise WorkspaceDigestError()
                launch_profile = self._capsules.launch_profile(profile, capsule)
            except (WorkspaceDigestError, ResumeNotAllowedError):
                self._workflow.mark_indeterminate(
                    run_id,
                    approval_id,
                    reason=ToolErrorCode.SOURCE_REVISION_MISMATCH.value,
                    authority=ownership.authority,
                )
                raise TestExecutionOutcomeIndeterminateError from None
        elif source_bound:
            try:
                actual_at_start = await self._digest_with_ownership(ownership)
                self._workflow.require_source_at_start(
                    run_id,
                    approval_id,
                    actual_digest=actual_at_start,
                    workspace_root_identity=str(self._profiles.workspace_root),
                    authority=ownership.authority,
                )
            except (ResumeNotAllowedError, WorkspaceDigestError):
                self._workflow.mark_indeterminate(
                    run_id,
                    approval_id,
                    reason=ToolErrorCode.SOURCE_REVISION_MISMATCH.value,
                    authority=ownership.authority,
                )
                raise TestExecutionOutcomeIndeterminateError from None
        try:
            if (
                await self._executable_digest_with_ownership(ownership, profile)
                != profile.executable_digest
            ):
                raise TestProfileBindingMismatchError(
                    ToolErrorCode.TEST_PROFILE_MISMATCH,
                    "Executable digest changed before launch",
                )
        except (TestProfileBindingMismatchError, ToolRuntimeError) as exc:
            self._workflow.mark_indeterminate(
                run_id,
                approval_id,
                reason="Executable identity changed before process launch",
                authority=ownership.authority,
            )
            raise TestExecutionOutcomeIndeterminateError from exc
        managed = await self._managed.execute(
            self._execution_key(approval_id),
            launch_profile,
        )
        outcome = managed.outcome
        task_cancelled = managed.caller_cancelled
        started = self._executions.get_for_approval(approval_id)
        source_digest_current: str | None
        try:
            source_digest_after = (
                capsule.source_digest
                if capsule is not None
                else (
                    await self._digest_with_ownership(ownership)
                    if source_bound
                    else None
                )
            )
            # This last scan is deliberately adjacent to the fenced terminal write.
            # AgentForge-managed writes require the same live Run lease and advance the
            # durable source revision checked by ``finish``. An unrelated OS process
            # cannot be locked out of the workspace, so mutation after this scan is an
            # unavoidable userspace-detection limit; placing it here minimizes the gap.
            if capsule is not None:
                await self._verify_capsule_with_ownership(ownership, capsule)
                source_digest_current = capsule.source_digest
            else:
                source_digest_current = (
                    await self._digest_with_ownership(ownership) if source_bound else None
                )
        except WorkspaceDigestError:
            self._workflow.mark_indeterminate(
                run_id,
                approval_id,
                reason=ToolErrorCode.SOURCE_REVISION_MISMATCH.value,
                authority=ownership.authority,
            )
            raise TestExecutionOutcomeIndeterminateError from None
        if outcome.status is SupervisorStatus.INDETERMINATE:
            self._workflow.mark_indeterminate(
                run_id,
                approval_id,
                reason="Process-tree termination could not be confirmed",
                authority=ownership.authority,
            )
            raise TestExecutionOutcomeIndeterminateError
        status, failure_kind = self._map_status(outcome)
        record = self._workflow.finish(
            run_id,
            approval_id,
            expected_version=started.record_version,
            status=status,
            failure_kind=failure_kind,
            exit_code=outcome.exit_code,
            stdout_digest=outcome.stdout.sha256_digest,
            stderr_digest=outcome.stderr.sha256_digest,
            stdout_size=outcome.stdout.size,
            stderr_size=outcome.stderr.size,
            stdout_summary=outcome.stdout.summary,
            stderr_summary=outcome.stderr.summary,
            stdout_truncated=outcome.stdout.truncated,
            stderr_truncated=outcome.stderr.truncated,
            duration_ms=outcome.duration_ms,
            termination_reason=outcome.termination_reason,
            termination_result=outcome.termination_result,
            root_pid=outcome.root_pid,
            process_group_id=outcome.process_group_id,
            job_id=outcome.job_id,
            source_digest_after=source_digest_after,
            source_digest_current=source_digest_current,
            authority=ownership.authority,
        )
        if status is ProcessExecutionStatus.CANCELLED:
            self._workflow.finalize_control_cancel(
                run_id, approval_id, authority=ownership.authority
            )
            if task_cancelled:
                raise asyncio.CancelledError
            raise TestExecutionCancelledError
        if task_cancelled:
            self._workflow.finalize_control_cancel(
                run_id, approval_id, authority=ownership.authority
            )
            raise asyncio.CancelledError
        return self._record_to_tool_result(record)

    async def _digest_with_ownership(self, ownership: RunAuthorityProvider) -> str:
        before = ownership.authority
        digest = await asyncio.to_thread(
            self._digester.digest, self._profiles.workspace_root
        )
        after = ownership.authority
        if (
            after.run_id != before.run_id
            or after.owner_id != before.owner_id
            or after.fencing_token != before.fencing_token
        ):
            raise TestExecutionOutcomeIndeterminateError
        return digest

    async def _capture_with_ownership(
        self,
        ownership: RunAuthorityProvider,
        execution_id: UUID,
        capsule_id: UUID,
        source_root: Path,
        verifier_root: Path,
    ) -> VerificationCapsule:
        before = ownership.authority
        capsule = await asyncio.to_thread(
            self._capsules.capture,
            execution_id=execution_id,
            capsule_id=capsule_id,
            source_root=source_root,
            verifier_root=verifier_root,
        )
        self._require_same_ownership(before, ownership.authority)
        return capsule

    async def _verify_capsule_with_ownership(
        self, ownership: RunAuthorityProvider, capsule: VerificationCapsule
    ) -> None:
        before = ownership.authority
        await asyncio.to_thread(self._capsules.verify, capsule)
        self._require_same_ownership(before, ownership.authority)

    async def _executable_digest_with_ownership(
        self, ownership: RunAuthorityProvider, profile: TestProfile
    ) -> str:
        before = ownership.authority
        digest = await asyncio.to_thread(self._profiles.executable_digest, profile)
        self._require_same_ownership(before, ownership.authority)
        return digest

    @staticmethod
    def _require_same_ownership(
        before: RunLeaseAuthority, after: RunLeaseAuthority
    ) -> None:
        if (
            after.run_id != before.run_id
            or after.owner_id != before.owner_id
            or after.fencing_token != before.fencing_token
        ):
            raise TestExecutionOutcomeIndeterminateError

    def recover_claimed(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        authority: RunLeaseAuthority,
    ) -> ToolResult | None:
        record = self._executions.get_for_approval(approval_id)
        if record.run_id != run_id:
            raise ValueError("Process execution belongs to another Run")
        if record.status is ProcessExecutionStatus.STARTED:
            self._workflow.mark_indeterminate(
                run_id,
                approval_id,
                reason="Runtime restarted while test execution was STARTED",
                authority=authority,
            )
            return None
        if record.status is ProcessExecutionStatus.INDETERMINATE:
            return None
        if record.status is ProcessExecutionStatus.CANCELLED:
            return None
        if record.status is ProcessExecutionStatus.CREATED:
            raise ValueError("Claimed test approval still has a CREATED execution")
        return self._record_to_tool_result(record)

    def cancel_active(self, run_id: UUID, reason: str | None = None) -> bool:
        executions = self._executions.list_for_run(run_id)
        active = next(
            (
                record
                for record in reversed(executions)
                if record.status is ProcessExecutionStatus.STARTED
            ),
            None,
        )
        if active is None:
            return False
        return self._managed.cancel(
            self._execution_key(active.approval_id),
            reason or "runtime_cancel",
        )

    @staticmethod
    def _execution_key(approval_id: UUID) -> ManagedExecutionKey:
        return ManagedExecutionKey(
            origin=ManagedExecutionOrigin.APPROVED_TOOL,
            execution_id=str(approval_id),
        )

    @staticmethod
    def _map_status(
        outcome: SupervisorOutcome,
    ) -> tuple[ProcessExecutionStatus, ProcessFailureKind | None]:
        if outcome.status is SupervisorStatus.EXITED:
            if outcome.exit_code == 0:
                return ProcessExecutionStatus.COMPLETED, None
            return ProcessExecutionStatus.FAILED, ProcessFailureKind.TEST_FAILURE
        if outcome.status is SupervisorStatus.TIMEOUT:
            return ProcessExecutionStatus.TIMEOUT, ProcessFailureKind.TIMEOUT
        if outcome.status is SupervisorStatus.CANCELLED:
            return ProcessExecutionStatus.CANCELLED, ProcessFailureKind.CANCELLED
        if outcome.status is SupervisorStatus.LAUNCH_FAILED:
            return ProcessExecutionStatus.FAILED, ProcessFailureKind.LAUNCH_FAILURE
        return ProcessExecutionStatus.INDETERMINATE, ProcessFailureKind.INDETERMINATE

    @classmethod
    def _record_to_tool_result(cls, record: ProcessExecutionRecord) -> ToolResult:
        if (
            record.termination_result
            == ToolErrorCode.SOURCE_REVISION_MISMATCH.value
        ):
            return ToolResult(
                success=False,
                error_type=ToolErrorCode.SOURCE_REVISION_MISMATCH,
                error_message="Test source revision does not match",
                metadata={
                    "execution_id": str(record.execution_id),
                    "attempt_number": record.attempt_number,
                    "process_status": record.status.value,
                },
            )
        if record.stdout_digest is None or record.stderr_digest is None:
            return ToolResult(
                success=False,
                error_type=ToolErrorCode.TOOL_EXECUTION_ERROR,
                error_message="Test execution result metadata is incomplete",
            )
        result = TestResult(
            success=record.status is ProcessExecutionStatus.COMPLETED,
            failure_kind=record.failure_kind,
            exit_code=record.exit_code,
            duration_ms=record.duration_ms,
            stdout_summary=record.stdout_summary,
            stderr_summary=record.stderr_summary,
            stdout_digest=record.stdout_digest,
            stderr_digest=record.stderr_digest,
            stdout_size=record.stdout_size,
            stderr_size=record.stderr_size,
            truncated=record.stdout_truncated or record.stderr_truncated,
            digest=cls._result_digest(record),
            termination_reason=record.termination_reason,
        )
        infrastructure_failure = record.failure_kind in {
            ProcessFailureKind.LAUNCH_FAILURE,
            ProcessFailureKind.PROFILE_MISMATCH,
            ProcessFailureKind.SUPERVISOR_FAILURE,
            ProcessFailureKind.INDETERMINATE,
        }
        return ToolResult(
            success=not infrastructure_failure,
            output=result.model_dump(mode="json") if not infrastructure_failure else None,
            error_type=(ToolErrorCode.TOOL_EXECUTION_ERROR if infrastructure_failure else None),
            error_message=("Managed test execution failed" if infrastructure_failure else None),
            duration_ms=record.duration_ms,
            truncated=result.truncated,
            metadata={
                "execution_id": str(record.execution_id),
                "attempt_number": record.attempt_number,
                "process_status": record.status.value,
                "result_digest": result.digest,
            },
        )

    @staticmethod
    def _result_digest(record: ProcessExecutionRecord) -> str:
        canonical = json.dumps(
            {
                "status": record.status.value,
                "failure_kind": (record.failure_kind.value if record.failure_kind else None),
                "exit_code": record.exit_code,
                "stdout_digest": record.stdout_digest,
                "stderr_digest": record.stderr_digest,
                "stdout_size": record.stdout_size,
                "stderr_size": record.stderr_size,
                "duration_ms": record.duration_ms,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
