from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentforge.application.contracts import OutcomeStatus, ReceiptStatus, RunControlRequestStatus
from agentforge.application.kernel_errors import (
    IdempotencyConflictError,
    InvalidReceiptTransitionError,
)
from agentforge.application.run_commands import (
    CancelRun,
    ResumeRecoveryChoice,
    ResumeRun,
)
from agentforge.application.run_driver import RunDriver, RunOwnership
from agentforge.domain.enums import (
    ApprovalConsumptionState,
    ApprovalStatus,
    EventType,
    RunStatus,
)
from agentforge.domain.models import ApprovalRequest, Checkpoint, Run
from agentforge.persistence.database import Database
from agentforge.persistence.product_tables import (
    ApplicationCommandReceiptRow,
    RunControlRequestRow,
    RunLeaseRow,
)
from agentforge.persistence.repositories import ApprovalRepository, EventRepository, RunRepository
from agentforge.persistence.resume_workflow import ResumeFailpoint, ResumeRunWorkflow
from agentforge.persistence.run_control import RunControlWorkflow
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.persistence.tables import (
    ApprovalRequestRow,
    CheckpointRow,
    MutationApprovalBindingRow,
    RunRow,
)
from agentforge.persistence.tables import (
    TestApprovalBindingRow as PersistedTestApprovalBindingRow,
)


def paused_kernel(path: Path) -> tuple[Database, Run, ApprovalRequest]:
    database = Database.from_path(path)
    database.create_schema()
    run = RunRepository(database).create(Run(task="resume safely"))
    checkpoint = Checkpoint(run_id=run.run_id, step_number=1, runtime_state={})
    approval = ApprovalRequest(
        run_id=run.run_id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_name="read_file",
        sanitized_arguments={"path": "README.md"},
        request_digest="a" * 64,
        status=ApprovalStatus.APPROVED,
    )
    with database.session() as session:
        row = session.get(RunRow, str(run.run_id))
        assert row is not None
        row.status = RunStatus.PAUSED.value
        session.add(
            CheckpointRow(
                checkpoint_id=str(checkpoint.checkpoint_id),
                run_id=str(run.run_id),
                step_number=checkpoint.step_number,
                runtime_state=checkpoint.runtime_state,
                created_at=checkpoint.created_at,
            )
        )
    ApprovalRepository(database).create(approval)
    return database, run, approval


def test_resume_command_is_strict_frozen_and_digest_covers_recovery_choice() -> None:
    command_id = uuid4()
    run_id = uuid4()
    first = ResumeRun(
        command_id=command_id,
        run_id=run_id,
        recovery_choice=ResumeRecoveryChoice.DECISION,
    )
    assert first.command_type == "RESUME_RUN"
    with pytest.raises(ValidationError):
        first.run_id = uuid4()  # type: ignore[misc]
    for command_type in (ResumeRun, CancelRun):
        for invalid in (True, "1", 1.0, 2):
            with pytest.raises(ValidationError):
                command_type.model_validate(
                    {
                        "schema_version": invalid,
                        "command_id": uuid4(),
                        "run_id": uuid4(),
                    }
                )


def test_resume_claim_receipt_lease_run_approval_and_event_commit_once(tmp_path: Path) -> None:
    database, run, approval = paused_kernel(tmp_path / "resume.sqlite3")
    command = ResumeRun(
        command_id=uuid4(),
        run_id=run.run_id,
        recovery_choice=ResumeRecoveryChoice.DECISION,
    )
    workflow = ResumeRunWorkflow(database)

    first = workflow.prepare(command, owner_id="resume-worker")
    repeated = workflow.prepare(command, owner_id="resume-worker")

    assert first.receipt.status is ReceiptStatus.IN_PROGRESS
    assert repeated.authority == first.authority
    assert repeated.receipt == first.receipt
    assert RunRepository(database).get(run.run_id).status is RunStatus.RUNNING
    assert (
        ApprovalRepository(database).get(approval.approval_id).consumption_state
        is ApprovalConsumptionState.CLAIMED
    )
    assert [event.event_type for event in EventRepository(database).list_for_run(run.run_id)].count(
        EventType.RUN_RESUMED
    ) == 1
    current = RunLeaseStore(database).current(run.run_id)
    assert current is not None and current.authority == first.authority
    database.close()


@pytest.mark.parametrize(
    ("phase", "binding_kind"),
    [
        (ResumeRecoveryChoice.DECISION, None),
        (ResumeRecoveryChoice.MUTATION, "mutation"),
        (ResumeRecoveryChoice.TEST_EXECUTION, "test"),
    ],
)
def test_resume_auto_derives_pre_model_phase_from_persisted_topology(
    tmp_path: Path,
    phase: ResumeRecoveryChoice,
    binding_kind: str | None,
) -> None:
    database, run, approval = paused_kernel(tmp_path / f"phase-{phase.value}.sqlite3")
    with database.session() as session:
        if binding_kind == "mutation":
            session.add(
                MutationApprovalBindingRow(
                    approval_id=str(approval.approval_id),
                    run_id=str(run.run_id),
                    checkpoint_id=str(approval.checkpoint_id),
                    tool_call_digest="b" * 64,
                    tool_name="write_file",
                    target_path="safe.txt",
                    target_existed=False,
                    before_sha256=None,
                    expected_after_sha256="c" * 64,
                    bytes_written=1,
                    created_at=approval.requested_at,
                )
            )
        elif binding_kind == "test":
            session.add(
                PersistedTestApprovalBindingRow(
                    approval_id=str(approval.approval_id),
                    run_id=str(run.run_id),
                    checkpoint_id=str(approval.checkpoint_id),
                    tool_call_digest="d" * 64,
                    profile_id="unit",
                    profile_version=1,
                    profile_digest="e" * 64,
                    executable_path="python",
                    argv_digest="f" * 64,
                    cwd=".",
                    environment_digest="1" * 64,
                    created_at=approval.requested_at,
                )
            )
    claims: list[ResumeRecoveryChoice] = []

    def claim(session: object, *_: object) -> None:
        row = session.get(ApprovalRequestRow, str(approval.approval_id))  # type: ignore[attr-defined]
        assert row is not None
        row.consumption_state = ApprovalConsumptionState.CLAIMED.value
        claims.append(phase)

    workflow = ResumeRunWorkflow(
        database,
        mutation_claimer=(claim if binding_kind == "mutation" else None),
        test_execution_claimer=(claim if binding_kind == "test" else None),
    )
    prepared = workflow.prepare(
        ResumeRun(command_id=uuid4(), run_id=run.run_id),
        owner_id="phase-worker",
    )

    assert prepared.phase is phase
    assert claims == ([] if binding_kind is None else [phase])
    resumed = [
        event
        for event in EventRepository(database).list_for_run(run.run_id)
        if event.event_type is EventType.RUN_RESUMED
    ]
    assert resumed[-1].payload["phase"] == phase.value
    assert resumed[-1].payload["execution_phase"] == phase.value
    with database.session() as session:
        lease = session.get(RunLeaseRow, str(run.run_id))
        assert lease is not None
        lease.acquired_at -= timedelta(seconds=40)
        lease.heartbeat_at -= timedelta(seconds=35)
        lease.expires_at -= timedelta(seconds=31)
    taken = workflow.prepare(
        ResumeRun(command_id=uuid4(), run_id=run.run_id),
        owner_id="replacement-worker",
    )
    assert taken.phase is phase
    assert taken.side_effect_claimed_now is False
    assert claims == ([] if binding_kind is None else [phase])
    database.close()


def test_resume_model_phase_ignores_completed_side_effect_binding(
    tmp_path: Path,
) -> None:
    database, run, approval = paused_kernel(tmp_path / "phase-model.sqlite3")
    with database.session() as session:
        approval_row = session.get(ApprovalRequestRow, str(approval.approval_id))
        assert approval_row is not None
        approval_row.consumption_state = ApprovalConsumptionState.CONSUMED.value
        session.add(
            MutationApprovalBindingRow(
                approval_id=str(approval.approval_id),
                run_id=str(run.run_id),
                checkpoint_id=str(approval.checkpoint_id),
                tool_call_digest="2" * 64,
                tool_name="write_file",
                target_path="safe.txt",
                target_existed=False,
                before_sha256=None,
                expected_after_sha256="3" * 64,
                bytes_written=1,
                created_at=approval.requested_at,
            )
        )

    prepared = ResumeRunWorkflow(database).prepare(
        ResumeRun(command_id=uuid4(), run_id=run.run_id),
        owner_id="model-worker",
    )

    assert prepared.phase is ResumeRecoveryChoice.MODEL
    assert prepared.execution_phase == ResumeRecoveryChoice.MODEL.value
    with database.session() as session:
        lease = session.get(RunLeaseRow, str(run.run_id))
        assert lease is not None
        lease.acquired_at -= timedelta(seconds=40)
        lease.heartbeat_at -= timedelta(seconds=35)
        lease.expires_at -= timedelta(seconds=31)
    taken = ResumeRunWorkflow(database).prepare(
        ResumeRun(command_id=uuid4(), run_id=run.run_id),
        owner_id="replacement-model-worker",
    )
    assert taken.phase is ResumeRecoveryChoice.MODEL
    assert taken.side_effect_claimed_now is False
    database.close()


@pytest.mark.parametrize("replacement_command", [False, True])
def test_expired_resume_owner_can_take_over_without_reclaiming_side_effect(
    tmp_path: Path,
    replacement_command: bool,
) -> None:
    database, run, approval = paused_kernel(
        tmp_path / f"resume-takeover-{replacement_command}.sqlite3"
    )
    first_command = ResumeRun(command_id=uuid4(), run_id=run.run_id)
    workflow = ResumeRunWorkflow(database)
    first = workflow.prepare(
        first_command,
        owner_id="crashed-worker",
    )
    with database.session() as session:
        lease = session.get(RunLeaseRow, str(run.run_id))
        assert lease is not None
        lease.acquired_at -= timedelta(seconds=3)
        lease.heartbeat_at -= timedelta(seconds=2)
        lease.expires_at -= timedelta(seconds=31)
    command = (
        ResumeRun(command_id=uuid4(), run_id=run.run_id)
        if replacement_command
        else first_command
    )

    taken = workflow.prepare(command, owner_id="replacement-worker")

    assert taken.authority.fencing_token == first.authority.fencing_token + 1
    assert taken.phase is ResumeRecoveryChoice.DECISION
    # A replacement after the durable RUN_RESUMED watermark owns recovery of
    # that persisted command slice; it must not be treated as a passive replay.
    assert taken.owns_command is True
    assert (
        ApprovalRepository(database).get(approval.approval_id).consumption_state
        is ApprovalConsumptionState.CLAIMED
    )
    if replacement_command:
        assert workflow.replay(first_command).status is ReceiptStatus.INDETERMINATE  # type: ignore[union-attr]
    database.close()


def test_resume_digest_conflict_does_not_claim_twice(tmp_path: Path) -> None:
    database, run, _ = paused_kernel(tmp_path / "conflict.sqlite3")
    command_id = uuid4()
    workflow = ResumeRunWorkflow(database)
    workflow.prepare(
        ResumeRun(
            command_id=command_id,
            run_id=run.run_id,
            recovery_choice=ResumeRecoveryChoice.DECISION,
        ),
        owner_id="worker",
    )
    with pytest.raises(IdempotencyConflictError):
        workflow.prepare(
            ResumeRun(
                command_id=command_id,
                run_id=run.run_id,
                recovery_choice=ResumeRecoveryChoice.MODEL,
            ),
            owner_id="worker",
        )
    database.close()


def test_resume_crash_before_commit_leaves_zero_claim(tmp_path: Path) -> None:
    database, run, approval = paused_kernel(tmp_path / "resume-crash.sqlite3")
    command = ResumeRun(command_id=uuid4(), run_id=run.run_id)
    with pytest.raises(RuntimeError, match="resume failpoint"):
        ResumeRunWorkflow(database).prepare(
            command,
            owner_id="worker",
            failpoint=ResumeFailpoint.BEFORE_COMMIT,
        )
    with database.session() as session:
        assert session.get(ApplicationCommandReceiptRow, str(command.command_id)) is None
        approval_row = session.get(ApprovalRequestRow, str(approval.approval_id))
        assert approval_row is not None
        assert approval_row.consumption_state == ApprovalConsumptionState.NOT_STARTED.value
    assert RunRepository(database).get(run.run_id).status is RunStatus.PAUSED
    assert RunLeaseStore(database).current(run.run_id) is None
    database.close()


def test_resume_claim_callback_failure_rolls_back_every_fact(tmp_path: Path) -> None:
    database, run, approval = paused_kernel(tmp_path / "resume-claim-rollback.sqlite3")
    command = ResumeRun(command_id=uuid4(), run_id=run.run_id)

    def broken_claim(session: object, *_: object) -> None:
        row = session.get(ApprovalRequestRow, str(approval.approval_id))  # type: ignore[attr-defined]
        assert row is not None
        row.consumption_state = ApprovalConsumptionState.CLAIMED.value
        raise RuntimeError("claim crash")

    with pytest.raises(RuntimeError, match="claim crash"):
        ResumeRunWorkflow(database).prepare(
            command,
            owner_id="worker",
            decision_claimer=broken_claim,  # type: ignore[arg-type]
        )
    with database.session() as session:
        assert session.get(ApplicationCommandReceiptRow, str(command.command_id)) is None
        row = session.get(ApprovalRequestRow, str(approval.approval_id))
        assert row is not None
        assert row.consumption_state == ApprovalConsumptionState.NOT_STARTED.value
    assert RunRepository(database).get(run.run_id).status is RunStatus.PAUSED
    assert RunLeaseStore(database).current(run.run_id) is None
    database.close()


def test_resume_terminal_receipt_replays_without_a_second_claim(tmp_path: Path) -> None:
    database, run, _ = paused_kernel(tmp_path / "resume-terminal-replay.sqlite3")
    command = ResumeRun(command_id=uuid4(), run_id=run.run_id)
    workflow = ResumeRunWorkflow(database)
    prepared = workflow.prepare(command, owner_id="worker")
    completed = workflow.terminalize(command, prepared.authority, ReceiptStatus.COMPLETED)
    assert completed.status is ReceiptStatus.COMPLETED
    assert workflow.replay(command) == completed
    with pytest.raises(InvalidReceiptTransitionError):
        workflow.prepare(command, owner_id="worker")
    assert [event.event_type for event in EventRepository(database).list_for_run(run.run_id)].count(
        EventType.RUN_RESUMED
    ) == 1
    database.close()


def test_cancel_command_only_writes_receipt_and_control_request(tmp_path: Path) -> None:
    database, run, _ = paused_kernel(tmp_path / "control.sqlite3")
    command = CancelRun(command_id=uuid4(), run_id=run.run_id, reason="user request")
    workflow = RunControlWorkflow(database)

    first = workflow.request_cancel(command)
    repeated = workflow.request_cancel(command)

    assert first == repeated
    assert first.status is RunControlRequestStatus.REQUESTED
    assert RunRepository(database).get(run.run_id).status is RunStatus.PAUSED
    with database.session() as session:
        rows = session.query(RunControlRequestRow).all()
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        assert len(rows) == 1
        assert receipt is not None and receipt.status == ReceiptStatus.COMPLETED.value
        assert rows[0].updated_at == rows[0].requested_at
    database.close()


def test_resume_replay_rejects_same_owner_with_replaced_lease(tmp_path: Path) -> None:
    database, run, _ = paused_kernel(tmp_path / "resume-replaced.sqlite3")
    command = ResumeRun(command_id=uuid4(), run_id=run.run_id)
    workflow = ResumeRunWorkflow(database)
    first = workflow.prepare(command, owner_id="stable-owner", ttl=timedelta(milliseconds=1))
    with database.session() as session:
        row = session.get(RunRow, str(run.run_id))
        assert row is not None
        row.status = RunStatus.RUNNING.value
    with database.session() as session:
        from agentforge.persistence.product_tables import RunLeaseRow

        lease_row = session.get(RunLeaseRow, str(run.run_id))
        assert lease_row is not None
        lease_row.acquired_at = lease_row.acquired_at - timedelta(seconds=3)
        lease_row.heartbeat_at = lease_row.heartbeat_at - timedelta(seconds=2)
        lease_row.expires_at = lease_row.expires_at - timedelta(seconds=1)
    replacement = RunLeaseStore(database).acquire(
        run.run_id, owner_id="stable-owner", ttl=timedelta(seconds=30)
    )
    assert replacement.lease_token != first.authority.lease_token
    with pytest.raises(Exception, match="stale"):
        workflow.prepare(command, owner_id="stable-owner")
    database.close()


@pytest.mark.asyncio
async def test_driver_returns_unknown_and_cancels_blocked_operation_on_lease_loss(
    tmp_path: Path,
) -> None:
    database, run, _ = paused_kernel(tmp_path / "driver-unknown.sqlite3")
    leases = RunLeaseStore(database)
    driver = RunDriver(
        leases,
        run_id=run.run_id,
        owner_id="driver",
        ttl=timedelta(milliseconds=1200),
        heartbeat_interval=timedelta(milliseconds=100),
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked(ownership: RunOwnership) -> None:
        assert ownership.authority.run_id == run.run_id
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(driver.run_outcome(blocked))
    await started.wait()
    active = leases.current(run.run_id)
    assert active is not None
    leases.release(active.authority)
    result = await asyncio.wait_for(task, timeout=2)
    assert result.outcome is OutcomeStatus.UNKNOWN
    assert result.value is None
    assert cancelled.is_set()
    assert driver.heartbeat_task is None
    database.close()
