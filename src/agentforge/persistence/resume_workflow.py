from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from agentforge.application.contracts import ReceiptStatus
from agentforge.application.kernel_errors import InvalidReceiptTransitionError, StaleFenceError
from agentforge.application.run_commands import ResumeRecoveryChoice, ResumeRun
from agentforge.domain.enums import ApprovalConsumptionState, EventType, RunStatus
from agentforge.domain.errors import ResumeNotAllowedError, RunNotFoundError
from agentforge.domain.models import normalize_utc, utc_now
from agentforge.domain.repair import RepairCompletionStatus
from agentforge.persistence.application_uow import ApplicationUnitOfWork
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import EventLog, RunLeaseAuthority
from agentforge.persistence.product_tables import ApplicationCommandReceiptRow, RunLeaseRow
from agentforge.persistence.receipts import ReceiptRecord, ReceiptStore, request_digest
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.persistence.tables import (
    ApprovalRequestRow,
    EventRow,
    MutationApprovalBindingRow,
    RepairStateRow,
    RunRow,
    TestApprovalBindingRow,
)


class ResumeFailpoint(StrEnum):
    BEFORE_COMMIT = "BEFORE_COMMIT"


class ResumePrepareDisposition(StrEnum):
    OWNS_COMMAND = "OWNS_COMMAND"
    OWNS_POST_WATERMARK_RECOVERY = "OWNS_POST_WATERMARK_RECOVERY"
    REPLAY_WAIT_FOREIGN_ACTIVE = "REPLAY_WAIT_FOREIGN_ACTIVE"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class ResumePreparation:
    receipt: ReceiptRecord
    authority: RunLeaseAuthority | None
    approval_id: UUID | None
    phase: ResumeRecoveryChoice | None
    execution_phase: str
    side_effect_claimed_now: bool
    disposition: ResumePrepareDisposition = ResumePrepareDisposition.OWNS_COMMAND

    @property
    def owns_command(self) -> bool:
        return self.disposition in {
            ResumePrepareDisposition.OWNS_COMMAND,
            ResumePrepareDisposition.OWNS_POST_WATERMARK_RECOVERY,
        }


ResumeDecisionClaimer = Callable[[Session, UUID, UUID, RunLeaseAuthority], None]


class ResumeRunWorkflow:
    """Atomically owns Receipt, execution lease, approval claim, Run CAS and Event."""

    def __init__(
        self,
        database: Database,
        *,
        mutation_claimer: ResumeDecisionClaimer | None = None,
        test_execution_claimer: ResumeDecisionClaimer | None = None,
    ) -> None:
        self._database = database
        self._receipts = ReceiptStore()
        self._leases = RunLeaseStore(None)
        self._events = EventLog()
        self._phase_claimers = {
            ResumeRecoveryChoice.MUTATION: mutation_claimer,
            ResumeRecoveryChoice.TEST_EXECUTION: test_execution_claimer,
        }

    def accept(self, command: ResumeRun) -> ReceiptRecord:
        """Durably accept a Resume intent before it waits for per-Run execution."""

        with ApplicationUnitOfWork(self._database) as uow:
            receipt = self._receipts.accept(uow.session, command)
            if receipt.status is ReceiptStatus.ACCEPTED:
                receipt = self._receipts.mark_in_progress(
                    uow.session, command.command_id, command.run_id
                )
            elif receipt.result_scope_type != "RUN" or receipt.result_scope_id != str(
                command.run_id
            ):
                raise InvalidReceiptTransitionError()
            uow.commit()
            return receipt

    def prepare(
        self,
        command: ResumeRun,
        *,
        owner_id: str,
        ttl: timedelta = timedelta(seconds=30),
        decision_claimer: ResumeDecisionClaimer | None = None,
        failpoint: ResumeFailpoint | None = None,
        allow_running_response_checkpoint: bool = False,
    ) -> ResumePreparation:
        with ApplicationUnitOfWork(self._database) as uow:
            session = uow.session
            receipt = self._receipts.accept(session, command)
            if receipt.status is ReceiptStatus.ACCEPTED:
                receipt = self._receipts.mark_in_progress(
                    session, command.command_id, command.run_id
                )
            elif receipt.status is not ReceiptStatus.IN_PROGRESS:
                raise InvalidReceiptTransitionError()
            if receipt.result_scope_type != "RUN" or receipt.result_scope_id != str(command.run_id):
                raise InvalidReceiptTransitionError()
            run = session.get(RunRow, str(command.run_id))
            if run is None:
                self._receipts.fail(session, command.command_id, at=utc_now())
                uow.commit()
                raise RunNotFoundError(f"Run {command.run_id} does not exist")

            approval = session.scalar(
                select(ApprovalRequestRow)
                .where(ApprovalRequestRow.run_id == str(command.run_id))
                .order_by(
                    ApprovalRequestRow.requested_at.desc(),
                    ApprovalRequestRow.approval_id.desc(),
                )
                .limit(1)
            )
            response_checkpoint_recovery = (
                approval is None
                and allow_running_response_checkpoint
                and run.status == RunStatus.RUNNING.value
            )
            if response_checkpoint_recovery and command.recovery_choice not in {
                ResumeRecoveryChoice.AUTO,
                ResumeRecoveryChoice.MODEL,
            }:
                response_checkpoint_recovery = False
            if approval is None and not response_checkpoint_recovery:
                rejected = self._receipts.fail(
                    session, command.command_id, at=utc_now()
                )
                result = ResumePreparation(
                    receipt=rejected,
                    authority=None,
                    approval_id=None,
                    phase=None,
                    execution_phase="",
                    side_effect_claimed_now=False,
                    disposition=ResumePrepareDisposition.REJECTED,
                )
                uow.commit()
                return result
            if response_checkpoint_recovery:
                phase = None
            else:
                assert approval is not None
                try:
                    phase = self._phase(session, command, approval)
                except ResumeNotAllowedError:
                    rejected = self._receipts.fail(
                        session, command.command_id, at=utc_now()
                    )
                    result = ResumePreparation(
                        receipt=rejected,
                        authority=None,
                        approval_id=UUID(approval.approval_id),
                        phase=None,
                        execution_phase="",
                        side_effect_claimed_now=False,
                        disposition=ResumePrepareDisposition.REJECTED,
                    )
                    uow.commit()
                    return result
            resolved_execution_phase = (
                "RESPONSE_CHECKPOINT" if phase is None else phase.value
            )
            approval_id = UUID(approval.approval_id) if approval is not None else None
            watermark = self._watermark(session, command, receipt)

            if run.status == RunStatus.RUNNING.value:
                current = self._leases.current_in_session(session, command.run_id)
                if current is not None:
                    if current.owner_id != owner_id:
                        result = ResumePreparation(
                            receipt=receipt,
                            authority=current.authority,
                            approval_id=approval_id,
                            phase=phase,
                            execution_phase=resolved_execution_phase,
                            side_effect_claimed_now=False,
                            # The foreign active lease is the only durable
                            # reason an observer may retry.  Its watermark may
                            # not have committed yet, so absence is not UNKNOWN.
                            disposition=ResumePrepareDisposition.REPLAY_WAIT_FOREIGN_ACTIVE,
                        )
                        uow.commit()
                        return result
                    if watermark is None:
                        raise StaleFenceError()
                    if (
                        watermark.payload.get("lease_token") != str(current.lease_token)
                        or watermark.payload.get("fencing_token") != current.fencing_token
                    ):
                        raise StaleFenceError()
                    result = ResumePreparation(
                        receipt=receipt,
                        authority=current.authority,
                        approval_id=approval_id,
                        phase=phase,
                        execution_phase=resolved_execution_phase,
                        side_effect_claimed_now=False,
                    )
                    uow.commit()
                    return result
                # A different explicit Resume takes ownership only after the
                # prior pre-watermark owner has expired.  That predecessor
                # never crossed the external-effect boundary, so its receipt
                # can be conclusively superseded.
                if watermark is None:
                    session.execute(
                        update(ApplicationCommandReceiptRow)
                        .where(
                            ApplicationCommandReceiptRow.command_id
                            != str(command.command_id),
                            ApplicationCommandReceiptRow.command_type
                            == command.command_type,
                            ApplicationCommandReceiptRow.result_scope_type == "RUN",
                            ApplicationCommandReceiptRow.result_scope_id
                            == str(command.run_id),
                            ApplicationCommandReceiptRow.status
                            == ReceiptStatus.IN_PROGRESS.value,
                        )
                        .values(
                            status=ReceiptStatus.INDETERMINATE.value,
                            updated_at=utc_now(),
                        )
                        .execution_options(synchronize_session=False)
                    )
            if run.status not in {RunStatus.PAUSED.value, RunStatus.RUNNING.value}:
                rejected = self._receipts.fail(
                    session, command.command_id, at=utc_now()
                )
                result = ResumePreparation(
                    receipt=rejected,
                    authority=None,
                    approval_id=approval_id,
                    phase=phase,
                    execution_phase=resolved_execution_phase,
                    side_effect_claimed_now=False,
                    disposition=ResumePrepareDisposition.REJECTED,
                )
                uow.commit()
                return result

            lease = self._leases.acquire_in_session(
                session, command.run_id, owner_id=owner_id, ttl=ttl
            )
            authority = lease.authority
            self._leases.claim_write(session, authority)
            needs_claim = approval is not None and (
                approval.consumption_state == ApprovalConsumptionState.NOT_STARTED.value
            )
            if needs_claim:
                assert approval is not None
            if needs_claim and phase is ResumeRecoveryChoice.DECISION:
                assert approval is not None
                if decision_claimer is None:
                    from agentforge.persistence.approval_workflow import ApprovalWorkflow

                    ApprovalWorkflow.claim_decision_in_session(
                        session,
                        command.run_id,
                        UUID(approval.approval_id),
                        authority,
                    )
                else:
                    decision_claimer(session, command.run_id, UUID(approval.approval_id), authority)
            elif needs_claim and phase in {
                ResumeRecoveryChoice.MUTATION,
                ResumeRecoveryChoice.TEST_EXECUTION,
            }:
                assert approval is not None
                phase_claimer = self._phase_claimers[phase]
                if phase_claimer is None:
                    raise ResumeNotAllowedError(
                        "Persisted side-effect phase requires its durable claimer"
                    )
                phase_claimer(
                    session,
                    command.run_id,
                    UUID(approval.approval_id),
                    authority,
                )
            elif needs_claim:
                raise ResumeNotAllowedError("Model recovery cannot claim a side effect")
            if run.status == RunStatus.PAUSED.value:
                changed = session.execute(
                    update(RunRow)
                    .where(
                        RunRow.run_id == str(command.run_id),
                        RunRow.status == RunStatus.PAUSED.value,
                    )
                    .values(status=RunStatus.RUNNING.value, updated_at=utc_now())
                )
                if self._rowcount(changed) != 1:
                    raise ResumeNotAllowedError("Run cannot be claimed for resume")
            if watermark is None:
                self._events.append(
                    session,
                    authority,
                    EventType.RUN_RESUMED,
                    {
                        "approval_id": approval.approval_id if approval is not None else None,
                        "phase": resolved_execution_phase,
                        "command_id": str(command.command_id),
                        "lease_token": str(authority.lease_token),
                        "fencing_token": authority.fencing_token,
                        "execution_phase": resolved_execution_phase,
                    },
                )
            if failpoint is ResumeFailpoint.BEFORE_COMMIT:
                raise RuntimeError("resume failpoint before commit")
            session.flush()
            result = ResumePreparation(
                receipt=receipt,
                authority=authority,
                approval_id=approval_id,
                phase=phase,
                execution_phase=resolved_execution_phase,
                side_effect_claimed_now=needs_claim,
                disposition=(
                    ResumePrepareDisposition.OWNS_POST_WATERMARK_RECOVERY
                    if watermark is not None
                    else ResumePrepareDisposition.OWNS_COMMAND
                ),
            )
            uow.commit()
            return result

    def replay(self, command: ResumeRun) -> ReceiptRecord | None:
        """Return an authoritative terminal Receipt without acquiring a new lease."""
        with ApplicationUnitOfWork(self._database) as uow:
            row = uow.session.get(ApplicationCommandReceiptRow, str(command.command_id))
            if row is None:
                return None
            if row.command_type != command.command_type or row.request_digest != request_digest(
                command
            ):
                from agentforge.application.kernel_errors import IdempotencyConflictError

                raise IdempotencyConflictError()
            receipt = self._receipts.get(uow.session, command.command_id)
            if receipt.status in {ReceiptStatus.ACCEPTED, ReceiptStatus.IN_PROGRESS}:
                return None
            if receipt.result_scope_type != "RUN" or receipt.result_scope_id != str(command.run_id):
                raise InvalidReceiptTransitionError()
            return receipt

    def finalize_observed_run(self, command: ResumeRun) -> ReceiptRecord | None:
        """Close an in-progress Resume only from its own durable terminal slice."""
        with ApplicationUnitOfWork(self._database) as uow:
            session = uow.session
            row = session.get(ApplicationCommandReceiptRow, str(command.command_id))
            if row is None:
                return None
            if row.command_type != command.command_type or row.request_digest != request_digest(
                command
            ):
                from agentforge.application.kernel_errors import IdempotencyConflictError

                raise IdempotencyConflictError()
            receipt = self._receipts.get(session, command.command_id)
            if receipt.status in {
                ReceiptStatus.COMPLETED,
                ReceiptStatus.FAILED,
                ReceiptStatus.INDETERMINATE,
            }:
                return receipt
            if (
                receipt.status is not ReceiptStatus.IN_PROGRESS
                or receipt.result_scope_type != "RUN"
                or receipt.result_scope_id != str(command.run_id)
            ):
                raise InvalidReceiptTransitionError()
            run = session.get(RunRow, str(command.run_id))
            repair = session.get(RepairStateRow, str(command.run_id))
            if (
                run is None
                or repair is None
                or run.status
                not in {
                    RunStatus.COMPLETED.value,
                    RunStatus.FAILED.value,
                    RunStatus.CANCELLED.value,
                }
                or self._watermark(session, command, receipt) is None
            ):
                return None
            terminalizer = (
                self._receipts.complete
                if run.status == RunStatus.COMPLETED.value
                else (
                    self._receipts.mark_indeterminate
                    if repair.status == RepairCompletionStatus.INDETERMINATE.value
                    else self._receipts.fail
                )
            )
            terminal = terminalizer(
                session, command.command_id, at=normalize_utc(run.updated_at)
            )
            uow.commit()
            return terminal

    def terminalize(
        self,
        command: ResumeRun,
        authority: RunLeaseAuthority,
        status: ReceiptStatus,
    ) -> ReceiptRecord:
        if status not in {ReceiptStatus.COMPLETED, ReceiptStatus.FAILED}:
            raise ValueError("active authority can only complete or fail a resume receipt")
        with ApplicationUnitOfWork(self._database) as uow:
            session = uow.session
            terminal_authority = authority
            release_terminal_authority = False
            try:
                self._leases.claim_write(session, terminal_authority)
            except StaleFenceError:
                # A newly requested approval releases its execution lease in the
                # RUN_PAUSED transaction. Close the Resume receipt under a fresh
                # receipt-only epoch only when that exact epoch was released.
                run = session.get(RunRow, str(command.run_id))
                lease = session.get(RunLeaseRow, str(command.run_id))
                if (
                    run is None
                    or run.status != RunStatus.WAITING_APPROVAL.value
                    or lease is None
                    or lease.owner_id != authority.owner_id
                    or lease.lease_token != str(authority.lease_token)
                    or lease.fencing_token != authority.fencing_token
                    or lease.released_at is None
                ):
                    raise
                terminal_authority = self._leases.acquire_in_session(
                    session,
                    command.run_id,
                    owner_id=f"resume-receipt:{command.command_id}",
                    ttl=timedelta(seconds=30),
                ).authority
                release_terminal_authority = True
                self._leases.claim_write(session, terminal_authority)
            receipt = self._receipts.get(session, command.command_id)
            if (
                receipt.status is not ReceiptStatus.IN_PROGRESS
                or receipt.result_scope_type != "RUN"
                or receipt.result_scope_id != str(command.run_id)
            ):
                raise InvalidReceiptTransitionError()
            if self._watermark(session, command, receipt) is None:
                raise InvalidReceiptTransitionError()
            terminal = (
                self._receipts.complete
                if status is ReceiptStatus.COMPLETED
                else self._receipts.fail
            )(session, command.command_id, at=utc_now())
            if release_terminal_authority:
                self._leases.release_in_session(session, terminal_authority)
            uow.commit()
            return terminal

    def mark_indeterminate(self, command: ResumeRun) -> ReceiptRecord:
        """Close only the command receipt after ownership loss; never touch Run/Event."""
        with ApplicationUnitOfWork(self._database) as uow:
            receipt = self._receipts.get(uow.session, command.command_id)
            if (
                receipt.status is ReceiptStatus.INDETERMINATE
                and receipt.result_scope_type == "RUN"
                and receipt.result_scope_id == str(command.run_id)
            ):
                return receipt
            if (
                receipt.status is not ReceiptStatus.IN_PROGRESS
                or receipt.result_scope_type != "RUN"
                or receipt.result_scope_id != str(command.run_id)
            ):
                raise InvalidReceiptTransitionError()
            terminal = self._receipts.mark_indeterminate(
                uow.session, command.command_id, at=utc_now()
            )
            uow.commit()
            return terminal

    @staticmethod
    def _watermark(
        session: Session, command: ResumeRun, receipt: ReceiptRecord
    ) -> EventRow | None:
        """First committed Resume event from this accepted command execution.

        Receipt creation is immutable acceptance evidence.  Pairing it with the
        exact command id and ordered global cursor excludes stale synthetic or
        historical RUN_RESUMED rows that happen to carry the same id.
        """
        events = session.scalars(
            select(EventRow)
            .where(
                EventRow.run_id == str(command.run_id),
                EventRow.event_type == EventType.RUN_RESUMED.value,
                EventRow.created_at >= receipt.created_at,
            )
            .order_by(EventRow.global_cursor)
        )
        return next(
            (
                event
                for event in events
                if event.payload.get("command_id") == str(command.command_id)
            ),
            None,
        )

    @staticmethod
    def _phase(
        session: Session,
        command: ResumeRun,
        approval: ApprovalRequestRow,
    ) -> ResumeRecoveryChoice:
        if approval.consumption_state == ApprovalConsumptionState.CONSUMED.value:
            actual = ResumeRecoveryChoice.MODEL
        elif session.get(MutationApprovalBindingRow, approval.approval_id) is not None:
            actual = ResumeRecoveryChoice.MUTATION
        elif session.get(TestApprovalBindingRow, approval.approval_id) is not None:
            actual = ResumeRecoveryChoice.TEST_EXECUTION
        else:
            actual = ResumeRecoveryChoice.DECISION
        if command.recovery_choice not in {ResumeRecoveryChoice.AUTO, actual}:
            raise ResumeNotAllowedError("Requested recovery phase does not match persisted state")
        return actual

    @staticmethod
    def _rowcount(result: object) -> int:
        value = getattr(result, "rowcount", None)
        return value if type(value) is int else 0
