from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.application.approval_commands import DecideApprovalCommand
from agentforge.domain.enums import (
    ApprovalConsumptionState,
    ApprovalStatus,
    EventType,
    RejectionStrategy,
    RunStatus,
)
from agentforge.domain.errors import ApprovalDecisionConflictError
from agentforge.domain.models import ApprovalRequest, Checkpoint, Run, RuntimeSnapshot
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.repositories import (
    ApprovalRepository,
    CheckpointRepository,
    EventRepository,
    RunRepository,
)
from agentforge.persistence.run_leases import RunLeaseStore


def make_harness(
    tmp_path: Path,
) -> tuple[ApprovalWorkflow, RunRepository, ApprovalRepository, EventRepository, Database]:
    database = Database.from_path(tmp_path / "workflow.sqlite3")
    database.create_schema()
    return (
        ApprovalWorkflow._evaluator_only_create(database),
        RunRepository(database),
        ApprovalRepository(database),
        EventRepository(database),
        database,
    )


def make_pause_records(run: Run) -> tuple[Checkpoint, ApprovalRequest]:
    checkpoint_id = uuid4()
    approval_id = uuid4()
    checkpoint = Checkpoint(
        checkpoint_id=checkpoint_id,
        run_id=run.run_id,
        step_number=run.current_step,
        runtime_state=RuntimeSnapshot(
            run_id=run.run_id,
            step_number=run.current_step,
            pending_tool_call=None,
            pending_approval_id=approval_id,
            tool_call_digest="a" * 64,
            resume_phase="AWAITING_APPROVAL",
        ).model_dump(mode="json"),
    )
    approval = ApprovalRequest(
        approval_id=approval_id,
        run_id=run.run_id,
        checkpoint_id=checkpoint_id,
        tool_name="approval_probe",
        sanitized_arguments={"value": "<redacted>"},
        request_digest="a" * 64,
    )
    return checkpoint, approval


def acquire_authority(database: Database, run: Run, purpose: str):
    return (
        RunLeaseStore(database)
        .acquire(
            run.run_id,
            owner_id=f"test:approval-workflow:{purpose}",
            ttl=timedelta(seconds=30),
        )
        .authority
    )


def decide(
    workflow: ApprovalWorkflow,
    approval_id,
    status: ApprovalStatus,
    strategy: RejectionStrategy,
    note: str | None,
):
    return workflow.resolve_command(
        DecideApprovalCommand(
            command_id=uuid4(),
            approval_id=approval_id,
            status=status,
            strategy=strategy,
            note=note,
        )
    )


def test_pause_and_approve_are_durable_and_idempotent(tmp_path: Path) -> None:
    workflow, runs, approvals, events, database = make_harness(tmp_path)
    run = runs.create(Run(task="pause and approve"))
    authority = acquire_authority(database, run, "pause-and-approve")
    run.transition_to(RunStatus.RUNNING)
    runs.save(run, authority=authority)
    checkpoint, approval = make_pause_records(run)

    workflow.pause_for_approval(run, checkpoint, approval, authority=authority)
    first = decide(
        workflow,
        approval.approval_id,
        ApprovalStatus.APPROVED,
        RejectionStrategy.CONTINUE,
        "approved",
    )
    repeated = decide(
        workflow,
        approval.approval_id,
        ApprovalStatus.APPROVED,
        RejectionStrategy.CONTINUE,
        "ignored repeat",
    )

    assert runs.get(run.run_id).status is RunStatus.PAUSED
    assert approvals.get(approval.approval_id).status is ApprovalStatus.APPROVED
    assert repeated == first
    assert [event.event_type for event in events.list_for_run(run.run_id)] == [
        EventType.CHECKPOINT_SAVED,
        EventType.APPROVAL_REQUESTED,
        EventType.RUN_PAUSED,
        EventType.APPROVAL_GRANTED,
    ]
    database.close()


def test_conflicting_decision_is_rejected(tmp_path: Path) -> None:
    workflow, runs, _, _, database = make_harness(tmp_path)
    run = runs.create(Run(task="decision conflict"))
    authority = acquire_authority(database, run, "decision-conflict")
    run.transition_to(RunStatus.RUNNING)
    runs.save(run, authority=authority)
    checkpoint, approval = make_pause_records(run)
    workflow.pause_for_approval(run, checkpoint, approval, authority=authority)
    decide(
        workflow,
        approval.approval_id,
        ApprovalStatus.APPROVED,
        RejectionStrategy.CONTINUE,
        None,
    )

    with pytest.raises(ApprovalDecisionConflictError):
        decide(
            workflow,
            approval.approval_id,
            ApprovalStatus.REJECTED,
            RejectionStrategy.CONTINUE,
            None,
        )
    database.close()


def test_repeating_same_rejection_returns_original_decision(tmp_path: Path) -> None:
    workflow, runs, _, _, database = make_harness(tmp_path)
    run = runs.create(Run(task="repeat rejection"))
    authority = acquire_authority(database, run, "repeat-rejection")
    run.transition_to(RunStatus.RUNNING)
    runs.save(run, authority=authority)
    checkpoint, approval = make_pause_records(run)
    workflow.pause_for_approval(run, checkpoint, approval, authority=authority)

    first = decide(
        workflow,
        approval.approval_id,
        ApprovalStatus.REJECTED,
        RejectionStrategy.CONTINUE,
        "first reason",
    )
    repeated = decide(
        workflow,
        approval.approval_id,
        ApprovalStatus.REJECTED,
        RejectionStrategy.CONTINUE,
        "different repeated note",
    )

    assert repeated == first
    database.close()


def test_resume_claim_is_conditional_and_single_use(tmp_path: Path) -> None:
    workflow, runs, approvals, _, database = make_harness(tmp_path)
    run = runs.create(Run(task="single claim"))
    pause_authority = acquire_authority(database, run, "single-claim-pause")
    run.transition_to(RunStatus.RUNNING)
    runs.save(run, authority=pause_authority)
    checkpoint, approval = make_pause_records(run)
    workflow.pause_for_approval(run, checkpoint, approval, authority=pause_authority)
    decide(
        workflow,
        approval.approval_id,
        ApprovalStatus.APPROVED,
        RejectionStrategy.CONTINUE,
        None,
    )

    authority = acquire_authority(database, run, "single-claim-resume")
    assert workflow.claim_resume(run.run_id, approval.approval_id, authority=authority)
    assert not workflow.claim_resume(run.run_id, approval.approval_id, authority=authority)
    assert runs.get(run.run_id).status is RunStatus.RUNNING
    assert approvals.get(approval.approval_id).consumption_state is ApprovalConsumptionState.CLAIMED
    database.close()


def test_cancel_atomically_closes_run_and_unconsumed_approval(tmp_path: Path) -> None:
    workflow, runs, approvals, events, database = make_harness(tmp_path)
    run = runs.create(Run(task="cancel approval"))
    pause_authority = acquire_authority(database, run, "cancel-pause")
    run.transition_to(RunStatus.RUNNING)
    runs.save(run, authority=pause_authority)
    checkpoint, approval = make_pause_records(run)
    workflow.pause_for_approval(run, checkpoint, approval, authority=pause_authority)

    authority = acquire_authority(database, run, "cancel")
    cancelled = workflow.cancel(run.run_id, "user cancelled", authority=authority)
    repeated = workflow.cancel(run.run_id, "repeat", authority=authority)

    assert cancelled.status is RunStatus.CANCELLED
    assert repeated.status is RunStatus.CANCELLED
    assert approvals.get(approval.approval_id).status is ApprovalStatus.CANCELLED
    assert events.list_for_run(run.run_id)[-1].event_type is EventType.RUN_CANCELLED
    RunLeaseStore(database).release(authority)
    with pytest.raises(ApprovalDecisionConflictError):
        decide(
            workflow,
            approval.approval_id,
            ApprovalStatus.APPROVED,
            RejectionStrategy.CONTINUE,
            None,
        )
    database.close()


def test_consumption_persists_result_checkpoint_and_repauses_run(tmp_path: Path) -> None:
    workflow, runs, approvals, _, database = make_harness(tmp_path)
    run = runs.create(Run(task="consume approval"))
    pause_authority = acquire_authority(database, run, "consume-pause")
    run.transition_to(RunStatus.RUNNING)
    runs.save(run, authority=pause_authority)
    checkpoint, approval = make_pause_records(run)
    workflow.pause_for_approval(run, checkpoint, approval, authority=pause_authority)
    decide(
        workflow,
        approval.approval_id,
        ApprovalStatus.APPROVED,
        RejectionStrategy.CONTINUE,
        None,
    )
    authority = acquire_authority(database, run, "consume-resume")
    assert workflow.claim_resume(run.run_id, approval.approval_id, authority=authority)
    result_checkpoint = Checkpoint(
        run_id=run.run_id,
        step_number=run.current_step,
        runtime_state={"schema_version": 1, "result": "stored"},
    )

    consumed = workflow.persist_consumed(
        run.run_id,
        approval.approval_id,
        result_checkpoint,
        result_status="success",
        result_summary="approval_probe completed",
        authority=authority,
    )

    assert consumed.consumption_state is ApprovalConsumptionState.CONSUMED
    assert approvals.get(approval.approval_id).result_status == "success"
    assert runs.get(run.run_id).status is RunStatus.PAUSED
    assert (
        CheckpointRepository(database).latest(run.run_id).checkpoint_id
        == result_checkpoint.checkpoint_id
    )
    assert workflow.claim_consumed_continuation(
        run.run_id, approval.approval_id, authority=authority
    )
    assert not workflow.claim_consumed_continuation(
        run.run_id, approval.approval_id, authority=authority
    )
    database.close()


def test_claimed_approval_can_be_marked_indeterminate(tmp_path: Path) -> None:
    workflow, runs, approvals, events, database = make_harness(tmp_path)
    run = runs.create(Run(task="indeterminate approval"))
    pause_authority = acquire_authority(database, run, "indeterminate-pause")
    run.transition_to(RunStatus.RUNNING)
    runs.save(run, authority=pause_authority)
    checkpoint, approval = make_pause_records(run)
    workflow.pause_for_approval(run, checkpoint, approval, authority=pause_authority)
    decide(
        workflow,
        approval.approval_id,
        ApprovalStatus.APPROVED,
        RejectionStrategy.CONTINUE,
        None,
    )
    authority = acquire_authority(database, run, "indeterminate-resume")
    assert workflow.claim_resume(run.run_id, approval.approval_id, authority=authority)

    workflow.mark_indeterminate(run.run_id, approval.approval_id, authority=authority)

    assert (
        approvals.get(approval.approval_id).consumption_state
        is ApprovalConsumptionState.INDETERMINATE
    )
    assert runs.get(run.run_id).status is RunStatus.FAILED
    assert events.list_for_run(run.run_id)[-1].event_type is EventType.RUN_FAILED
    database.close()
