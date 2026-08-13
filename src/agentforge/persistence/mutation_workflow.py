from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agentforge.application.kernel_errors import (
    MutationConflictError,
    PersistenceBoundaryError,
    SourceRevisionConflictError,
)
from agentforge.domain.enums import (
    ApprovalConsumptionState,
    ApprovalStatus,
    EventType,
    MutationExecutionStatus,
    RunFailureCode,
    RunStatus,
)
from agentforge.domain.errors import (
    MutationBindingNotFoundError,
    ResumeNotAllowedError,
)
from agentforge.domain.models import utc_now
from agentforge.domain.mutations import MutationExecutionRecord
from agentforge.domain.repair import RepairCompletionStatus, RepairTerminationReason
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import EventLog, RunLeaseAuthority
from agentforge.persistence.mutations import MutationExecutionRepository
from agentforge.persistence.product_tables import WorkspaceSourceBindingRow
from agentforge.persistence.repair_terminal import (
    RepairStateProvenance,
    terminalize_repair_in_session,
)
from agentforge.persistence.run_leases import RunLeaseStore, claim_bound_write
from agentforge.persistence.source_revisions import (
    DIGEST_ALGORITHM_VERSION,
    MutationRecoveryAction,
    SourceRevisionStore,
    WorkspaceDigester,
    classify_writing_recovery,
    source_revision_audit,
    source_revision_summary,
)
from agentforge.persistence.tables import (
    ApprovalRequestRow,
    MutationApprovalBindingRow,
    MutationExecutionRow,
    RunRow,
)
from agentforge.testing.failpoints import DisabledFailpoints, FailpointController


class MutationWorkflow:
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
    ) -> MutationWorkflow:
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

    def has_source_revision(self, run_id: UUID) -> bool:
        with self._database.session() as session:
            return SourceRevisionStore().find(session, run_id) is not None

    def _evaluator_only_ensure_prepared(
        self,
        approval_id: UUID,
        *,
        before_workspace_digest: str,
        expected_after_workspace_digest: str,
        actual_digest_verified: bool = False,
    ) -> MutationExecutionRecord:
        with self._database.session() as session:
            binding = session.get(MutationApprovalBindingRow, str(approval_id))
            if binding is None:
                raise MutationBindingNotFoundError(
                    f"Approval {approval_id} has no mutation binding"
                )
            run_id = UUID(binding.run_id)
        leases = RunLeaseStore(self._database)
        lease = leases.acquire(
            run_id,
            owner_id=f"legacy-evaluator:mutation-prepare:{uuid4()}",
            ttl=timedelta(seconds=30),
        )
        try:
            return self.ensure_prepared(
                approval_id,
                before_workspace_digest=before_workspace_digest,
                expected_after_workspace_digest=expected_after_workspace_digest,
                actual_digest_verified=actual_digest_verified,
                authority=lease.authority,
            )
        finally:
            leases.release(lease.authority)

    def ensure_prepared(
        self,
        approval_id: UUID,
        *,
        before_workspace_digest: str,
        expected_after_workspace_digest: str,
        actual_digest_verified: bool = False,
        authority: RunLeaseAuthority,
    ) -> MutationExecutionRecord:
        with self._database.session() as session:
            return self.ensure_prepared_in_session(
                session,
                approval_id,
                before_workspace_digest=before_workspace_digest,
                expected_after_workspace_digest=expected_after_workspace_digest,
                actual_digest_verified=actual_digest_verified,
                authority=authority,
            )

    def ensure_prepared_in_session(
        self,
        session: Session,
        approval_id: UUID,
        *,
        before_workspace_digest: str,
        expected_after_workspace_digest: str,
        actual_digest_verified: bool = False,
        authority: RunLeaseAuthority,
    ) -> MutationExecutionRecord:
        """Prepare a mutation in the caller's Resume transaction."""

        if not (
            WorkspaceDigester._valid_digest(before_workspace_digest)
            and WorkspaceDigester._valid_digest(expected_after_workspace_digest)
        ):
            raise SourceRevisionConflictError()
        binding = session.get(MutationApprovalBindingRow, str(approval_id))
        if binding is None:
            raise MutationBindingNotFoundError(
                f"Approval {approval_id} has no mutation binding"
            )
        claim_bound_write(session, UUID(binding.run_id), authority)
        existing = session.scalar(
            select(MutationExecutionRow).where(
                MutationExecutionRow.approval_id == str(approval_id)
            )
        )
        if existing is not None:
            return self._require_same_preparation(
                existing,
                binding,
                before_workspace_digest=before_workspace_digest,
                expected_after_workspace_digest=expected_after_workspace_digest,
            )
        approval = session.get(ApprovalRequestRow, str(approval_id))
        if approval is None or approval.status != ApprovalStatus.APPROVED.value:
            raise ResumeNotAllowedError("Only an approved mutation can prepare execution")
        source = SourceRevisionStore().find(session, UUID(binding.run_id))
        if source is not None and (
            source.expected_source_digest != before_workspace_digest
            or source.digest_algorithm_version != DIGEST_ALGORITHM_VERSION
        ):
            raise SourceRevisionConflictError()
        record = MutationExecutionRecord(
            run_id=UUID(binding.run_id),
            approval_id=approval_id,
            tool_call_digest=binding.tool_call_digest,
            tool_name=binding.tool_name,
            target_path=binding.target_path,
            before_sha256=binding.before_sha256,
            expected_after_sha256=binding.expected_after_sha256,
            before_workspace_digest=before_workspace_digest,
            expected_after_workspace_digest=expected_after_workspace_digest,
            bytes_written=binding.bytes_written,
            result_summary=source_revision_summary(
                source is not None,
                actual_digest_verified=actual_digest_verified and source is not None,
                message="Mutation prepared",
            ),
        )
        try:
            with session.begin_nested():
                session.add(MutationExecutionRepository._to_row(record))
                session.flush()
        except IntegrityError:
            existing = session.scalar(
                select(MutationExecutionRow).where(
                    MutationExecutionRow.approval_id == str(approval_id)
                )
            )
            if existing is None:
                raise PersistenceBoundaryError() from None
            return self._require_same_preparation(
                existing,
                binding,
                before_workspace_digest=before_workspace_digest,
                expected_after_workspace_digest=expected_after_workspace_digest,
            )
        return record

    @staticmethod
    def _require_same_preparation(
        row: MutationExecutionRow,
        binding: MutationApprovalBindingRow,
        *,
        before_workspace_digest: str,
        expected_after_workspace_digest: str,
    ) -> MutationExecutionRecord:
        record = MutationExecutionRepository._to_domain(row)
        if (
            record.run_id != UUID(binding.run_id)
            or record.approval_id != UUID(binding.approval_id)
            or record.tool_call_digest != binding.tool_call_digest
            or record.tool_name != binding.tool_name
            or record.target_path != binding.target_path
            or record.before_sha256 != binding.before_sha256
            or record.expected_after_sha256 != binding.expected_after_sha256
            or record.bytes_written != binding.bytes_written
            or record.before_workspace_digest != before_workspace_digest
            or record.expected_after_workspace_digest != expected_after_workspace_digest
        ):
            raise MutationConflictError()
        return record

    def claim_resume(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        actual_workspace_digest: str,
        workspace_root_identity: str | None = None,
        authority: RunLeaseAuthority,
    ) -> bool:
        claimed = False
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            record = self._record_for_approval(session, approval_id)
            if record.run_id != run_id:
                return False
            if record.before_workspace_digest != actual_workspace_digest:
                raise SourceRevisionConflictError()
            source = SourceRevisionStore().find(session, run_id)
            if source is not None:
                if workspace_root_identity is None:
                    raise SourceRevisionConflictError()
                SourceRevisionStore().require_actual(
                    session,
                    run_id,
                    actual_workspace_digest,
                    workspace_root_identity=workspace_root_identity,
                    authority=authority,
                )
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
            mutation_changed = ApprovalWorkflow._affected_rows(
                session.execute(
                    update(MutationExecutionRow)
                    .where(
                        MutationExecutionRow.approval_id == str(approval_id),
                        MutationExecutionRow.run_id == str(run_id),
                        MutationExecutionRow.status == MutationExecutionStatus.PREPARED.value,
                    )
                    .values(
                        status=MutationExecutionStatus.WRITING.value,
                        updated_at=now,
                    )
                )
            )
            if mutation_changed != 1:
                raise ResumeNotAllowedError("Mutation execution is not prepared")
            run_changed = ApprovalWorkflow._affected_rows(
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
                raise ResumeNotAllowedError("Mutation Run cannot be claimed for resume")
            record = self._record_for_approval(session, approval_id)
            EventLog().append(
                session,
                authority,
                EventType.RUN_RESUMED,
                {"approval_id": str(approval_id), "phase": "MUTATION"},
            )
            EventLog().append(
                session,
                authority,
                EventType.MUTATION_STARTED,
                self._event_payload(
                    record,
                    duration_ms=0,
                    source_bound=source is not None,
                    actual_digest_verified=source is not None,
                ),
            )
            claimed = True
        if claimed:
            self._failpoints.hit("mutation_writing")
        return claimed

    def claim_resume_in_session(
        self,
        session: Session,
        run_id: UUID,
        approval_id: UUID,
        *,
        actual_workspace_digest: str,
        workspace_root_identity: str | None,
        authority: RunLeaseAuthority,
    ) -> None:
        """Atomically claim Approval+Mutation; outer Resume owns Run/Event/Receipt."""
        claim_bound_write(session, run_id, authority)
        record = self._record_for_approval(session, approval_id)
        if record.run_id != run_id:
            raise ResumeNotAllowedError("Mutation execution belongs to another Run")
        if record.before_workspace_digest != actual_workspace_digest:
            raise SourceRevisionConflictError()
        source = SourceRevisionStore().find(session, run_id)
        if source is not None:
            if workspace_root_identity is None:
                raise SourceRevisionConflictError()
            SourceRevisionStore().require_actual(
                session,
                run_id,
                actual_workspace_digest,
                workspace_root_identity=workspace_root_identity,
                authority=authority,
            )
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
            raise ResumeNotAllowedError("Mutation approval could not be claimed")
        mutation_changed = ApprovalWorkflow._affected_rows(
            session.execute(
                update(MutationExecutionRow)
                .where(
                    MutationExecutionRow.approval_id == str(approval_id),
                    MutationExecutionRow.run_id == str(run_id),
                    MutationExecutionRow.status == MutationExecutionStatus.PREPARED.value,
                )
                .values(
                    status=MutationExecutionStatus.WRITING.value,
                    updated_at=utc_now(),
                )
            )
        )
        if mutation_changed != 1:
            raise ResumeNotAllowedError("Mutation execution is not prepared")
        record = self._record_for_approval(session, approval_id)
        EventLog().append(
            session,
            authority,
            EventType.MUTATION_STARTED,
            self._event_payload(
                record,
                duration_ms=0,
                source_bound=source is not None,
                actual_digest_verified=source is not None,
            ),
        )

    def mark_committed(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        actual_after_sha256: str,
        actual_workspace_digest: str,
        bytes_written: int,
        duration_ms: int,
        authority: RunLeaseAuthority,
    ) -> MutationExecutionRecord:
        return self._finish(
            run_id,
            approval_id,
            status=MutationExecutionStatus.COMMITTED,
            actual_after_sha256=actual_after_sha256,
            actual_workspace_digest=actual_workspace_digest,
            bytes_written=bytes_written,
            result_summary="Mutation committed",
            duration_ms=duration_ms,
            authority=authority,
        )

    def mark_failed(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        result_summary: str,
        duration_ms: int,
        authority: RunLeaseAuthority,
    ) -> MutationExecutionRecord:
        return self._finish(
            run_id,
            approval_id,
            status=MutationExecutionStatus.FAILED,
            actual_after_sha256=None,
            actual_workspace_digest=None,
            bytes_written=0,
            result_summary=result_summary,
            duration_ms=duration_ms,
            authority=authority,
        )

    def mark_indeterminate(
        self,
        run_id: UUID,
        approval_id: UUID,
        reason: str,
        *,
        authority: RunLeaseAuthority,
    ) -> None:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            self._mark_indeterminate_in_session(session, run_id, approval_id, reason, authority)

    def _mark_indeterminate_in_session(
        self,
        session: Session,
        run_id: UUID,
        approval_id: UUID,
        reason: str,
        authority: RunLeaseAuthority,
    ) -> None:
        source_bound = SourceRevisionStore().find(session, run_id) is not None
        reason = source_revision_summary(source_bound, actual_digest_verified=False, message=reason)
        now = utc_now()
        changed = ApprovalWorkflow._affected_rows(
            session.execute(
                update(MutationExecutionRow)
                .where(
                    MutationExecutionRow.approval_id == str(approval_id),
                    MutationExecutionRow.run_id == str(run_id),
                    MutationExecutionRow.status.in_(
                        [
                            MutationExecutionStatus.WRITING.value,
                            MutationExecutionStatus.COMMITTED.value,
                        ]
                    ),
                )
                .values(
                    status=MutationExecutionStatus.INDETERMINATE.value,
                    result_summary=reason[:500],
                    updated_at=now,
                )
            )
        )
        if changed != 1:
            raise ResumeNotAllowedError("Mutation outcome cannot become indeterminate")
        approval_changed = ApprovalWorkflow._affected_rows(
            session.execute(
                update(ApprovalRequestRow)
                .where(
                    ApprovalRequestRow.approval_id == str(approval_id),
                    ApprovalRequestRow.run_id == str(run_id),
                    ApprovalRequestRow.consumption_state == ApprovalConsumptionState.CLAIMED.value,
                )
                .values(
                    consumption_state=ApprovalConsumptionState.INDETERMINATE.value,
                    result_status="indeterminate",
                    result_summary=reason[:500],
                )
            )
        )
        if approval_changed != 1:
            raise ResumeNotAllowedError("Approval is not claimed for mutation recovery")
        run_changed = ApprovalWorkflow._affected_rows(
            session.execute(
                update(RunRow)
                .where(
                    RunRow.run_id == str(run_id),
                    RunRow.status == RunStatus.RUNNING.value,
                )
                .values(
                    status=RunStatus.FAILED.value,
                    error_message="Mutation outcome is indeterminate",
                    updated_at=now,
                )
            )
        )
        if run_changed != 1:
            raise ResumeNotAllowedError("Mutation Run is not active")
        record = self._record_for_approval(session, approval_id)
        EventLog().append(
            session,
            authority,
            EventType.MUTATION_INDETERMINATE,
            self._event_payload(
                record,
                duration_ms=0,
                source_bound=source_bound,
                actual_digest_verified=False,
            ),
        )
        EventLog().append(
            session,
            authority,
            EventType.RUN_FAILED,
            {"code": RunFailureCode.MUTATION_INDETERMINATE.value},
        )
        terminalize_repair_in_session(
            session,
            run_id,
            status=RepairCompletionStatus.INDETERMINATE,
            reason=RepairTerminationReason.INDETERMINATE_SIDE_EFFECT,
            at=now,
            provenance=self._repair_state_provenance,
        )

    def _finish(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        status: MutationExecutionStatus,
        actual_after_sha256: str | None,
        actual_workspace_digest: str | None,
        bytes_written: int,
        result_summary: str,
        duration_ms: int,
        authority: RunLeaseAuthority,
    ) -> MutationExecutionRecord:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            before_record = self._record_for_approval(session, approval_id)
            source = SourceRevisionStore().find(session, run_id)
            digest_verified = status is MutationExecutionStatus.COMMITTED and source is not None
            result_summary = source_revision_summary(
                source is not None,
                actual_digest_verified=digest_verified,
                message=result_summary,
            )
            if status is MutationExecutionStatus.COMMITTED:
                if (
                    actual_workspace_digest != before_record.expected_after_workspace_digest
                    or actual_after_sha256 != before_record.expected_after_sha256
                ):
                    raise SourceRevisionConflictError()
            changed = ApprovalWorkflow._affected_rows(
                session.execute(
                    update(MutationExecutionRow)
                    .where(
                        MutationExecutionRow.approval_id == str(approval_id),
                        MutationExecutionRow.run_id == str(run_id),
                        MutationExecutionRow.status == MutationExecutionStatus.WRITING.value,
                    )
                    .values(
                        status=status.value,
                        actual_after_sha256=actual_after_sha256,
                        bytes_written=bytes_written,
                        result_summary=result_summary[:500],
                        updated_at=utc_now(),
                    )
                )
            )
            if changed != 1:
                raise ResumeNotAllowedError("Only a WRITING mutation can finish")
            record = self._record_for_approval(session, approval_id)
            if (
                status is MutationExecutionStatus.COMMITTED
                and record.actual_after_sha256 != record.expected_after_sha256
            ):
                raise ResumeNotAllowedError("Committed mutation hash does not match its plan")
            if status is MutationExecutionStatus.COMMITTED and source is not None:
                SourceRevisionStore().advance(
                    session,
                    run_id,
                    before_digest=record.before_workspace_digest,
                    expected_after_digest=record.expected_after_workspace_digest,
                    expected_revision_number=source.source_revision_number,
                    authority=authority,
                )
            event_type = (
                EventType.MUTATION_COMMITTED
                if status is MutationExecutionStatus.COMMITTED
                else EventType.MUTATION_FAILED
            )
            EventLog().append(
                session,
                authority,
                event_type,
                self._event_payload(
                    record,
                    duration_ms=duration_ms,
                    source_bound=source is not None,
                    actual_digest_verified=digest_verified,
                ),
            )
            return record

    def recover_writing(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        actual_workspace_digest: str,
        workspace_root_identity: str | None = None,
        authority: RunLeaseAuthority,
    ) -> MutationRecoveryAction:
        with self._database.session() as session:
            claim_bound_write(session, run_id, authority)
            record = self._record_for_approval(session, approval_id)
            if record.run_id != run_id:
                raise SourceRevisionConflictError()
            source = SourceRevisionStore().find(session, run_id)
            row = session.get(WorkspaceSourceBindingRow, str(run_id))
            if (
                source is None
                or row is None
                or workspace_root_identity is None
                or row.workspace_root_identity != workspace_root_identity
            ):
                raise SourceRevisionConflictError()
            if record.status is MutationExecutionStatus.COMMITTED:
                if (
                    actual_workspace_digest != record.expected_after_workspace_digest
                    or source.expected_source_digest != record.expected_after_workspace_digest
                ):
                    raise SourceRevisionConflictError()
                return MutationRecoveryAction.FINALIZE
            if record.status is not MutationExecutionStatus.WRITING:
                raise ResumeNotAllowedError("Mutation execution is not recoverable")
            if source.expected_source_digest != record.before_workspace_digest:
                raise SourceRevisionConflictError()
            action = classify_writing_recovery(
                actual_workspace_digest,
                before=record.before_workspace_digest,
                expected_after=record.expected_after_workspace_digest,
            )
            if action is MutationRecoveryAction.RETRY:
                self._reset_writing_for_retry(session, run_id, approval_id)
            elif action is MutationRecoveryAction.FINALIZE:
                self._finalize_writing_recovery(session, run_id, approval_id, record, authority)
            else:
                self._mark_indeterminate_in_session(
                    session,
                    run_id,
                    approval_id,
                    "Mutation outcome is indeterminate",
                    authority,
                )
            return action

    @staticmethod
    def _reset_writing_for_retry(session: Session, run_id: UUID, approval_id: UUID) -> None:
        now = utc_now()
        mutation_changed = ApprovalWorkflow._affected_rows(
            session.execute(
                update(MutationExecutionRow)
                .where(
                    MutationExecutionRow.approval_id == str(approval_id),
                    MutationExecutionRow.run_id == str(run_id),
                    MutationExecutionRow.status == MutationExecutionStatus.WRITING.value,
                )
                .values(status=MutationExecutionStatus.PREPARED.value, updated_at=now)
            )
        )
        approval_changed = ApprovalWorkflow._affected_rows(
            session.execute(
                update(ApprovalRequestRow)
                .where(
                    ApprovalRequestRow.approval_id == str(approval_id),
                    ApprovalRequestRow.run_id == str(run_id),
                    ApprovalRequestRow.consumption_state == ApprovalConsumptionState.CLAIMED.value,
                )
                .values(consumption_state=ApprovalConsumptionState.NOT_STARTED.value)
            )
        )
        run_changed = ApprovalWorkflow._affected_rows(
            session.execute(
                update(RunRow)
                .where(
                    RunRow.run_id == str(run_id),
                    RunRow.status == RunStatus.RUNNING.value,
                )
                .values(status=RunStatus.PAUSED.value, updated_at=now)
            )
        )
        if (mutation_changed, approval_changed, run_changed) != (1, 1, 1):
            raise ResumeNotAllowedError("Mutation retry transition was rejected")

    def _finalize_writing_recovery(
        self,
        session: Session,
        run_id: UUID,
        approval_id: UUID,
        record: MutationExecutionRecord,
        authority: RunLeaseAuthority,
    ) -> None:
        source = SourceRevisionStore().get(session, run_id)
        changed = ApprovalWorkflow._affected_rows(
            session.execute(
                update(MutationExecutionRow)
                .where(
                    MutationExecutionRow.approval_id == str(approval_id),
                    MutationExecutionRow.run_id == str(run_id),
                    MutationExecutionRow.status == MutationExecutionStatus.WRITING.value,
                )
                .values(
                    status=MutationExecutionStatus.COMMITTED.value,
                    actual_after_sha256=record.expected_after_sha256,
                    result_summary=source_revision_summary(
                        True,
                        actual_digest_verified=True,
                        message="Mutation finalized during recovery",
                    ),
                    updated_at=utc_now(),
                )
            )
        )
        if changed != 1:
            raise ResumeNotAllowedError("Mutation recovery finalize was rejected")
        SourceRevisionStore().advance(
            session,
            run_id,
            before_digest=record.before_workspace_digest,
            expected_after_digest=record.expected_after_workspace_digest,
            expected_revision_number=source.source_revision_number,
            authority=authority,
        )
        finalized = self._record_for_approval(session, approval_id)
        EventLog().append(
            session,
            authority,
            EventType.MUTATION_COMMITTED,
            self._event_payload(
                finalized,
                duration_ms=0,
                source_bound=True,
                actual_digest_verified=True,
            ),
        )

    @staticmethod
    def _record_for_approval(session: Session, approval_id: UUID) -> MutationExecutionRecord:
        row = session.scalar(
            select(MutationExecutionRow).where(MutationExecutionRow.approval_id == str(approval_id))
        )
        if row is None:
            raise ResumeNotAllowedError("Mutation execution record is missing")
        return MutationExecutionRepository._to_domain(row)

    @staticmethod
    def _event_payload(
        record: MutationExecutionRecord,
        *,
        duration_ms: int,
        source_bound: bool,
        actual_digest_verified: bool,
    ) -> dict[str, JsonValue]:
        semantics, verified = source_revision_audit(
            source_bound, actual_digest_verified=actual_digest_verified
        )
        return {
            "execution_id": str(record.execution_id),
            "run_id": str(record.run_id),
            "tool_name": record.tool_name,
            "relative_path": record.target_path,
            "before_sha256": record.before_sha256,
            "after_sha256": record.actual_after_sha256,
            "bytes_written": record.bytes_written,
            "duration_ms": duration_ms,
            "status": record.status.value,
            "source_revision_semantics": semantics,
            "source_verified": verified,
        }
