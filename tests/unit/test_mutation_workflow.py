from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import update

from agentforge.application.approval_commands import DecideApprovalCommand
from agentforge.application.contracts import ReceiptStatus
from agentforge.application.kernel_errors import (
    IdempotencyConflictError,
    MutationConflictError,
    StaleFenceError,
)
from agentforge.domain.enums import (
    ApprovalConsumptionState,
    ApprovalStatus,
    EventType,
    MutationExecutionStatus,
    RejectionStrategy,
    RunStatus,
)
from agentforge.domain.errors import ResumeNotAllowedError
from agentforge.domain.models import ApprovalRequest, Checkpoint, Run
from agentforge.domain.mutations import MutationApprovalBinding
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.mutation_workflow import MutationWorkflow
from agentforge.persistence.mutations import (
    MutationApprovalBindingRepository,
    MutationExecutionRepository,
)
from agentforge.persistence.product_tables import ApplicationCommandReceiptRow
from agentforge.persistence.repositories import (
    ApprovalRepository,
    EventRepository,
    RunRepository,
)
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.persistence.tables import MutationExecutionRow

SHA_A = "a" * 64
SHA_B = "b" * 64


def make_paused_mutation(
    database: Database,
) -> tuple[Run, ApprovalRequest, MutationApprovalBinding]:
    runs = RunRepository(database)
    run = runs.create(Run(task="mutation"))
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test-setup", ttl=timedelta(seconds=30)
    )
    run.transition_to(RunStatus.RUNNING)
    runs.save(run, authority=lease.authority)
    checkpoint = Checkpoint(
        run_id=run.run_id,
        step_number=1,
        runtime_state={"history": [], "resume_phase": "AWAITING_APPROVAL"},
    )
    approval = ApprovalRequest(
        run_id=run.run_id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_name="write_file",
        sanitized_arguments={"path": "src/app.py", "content": "<redacted>"},
        request_digest=SHA_A,
    )
    binding = MutationApprovalBinding(
        approval_id=approval.approval_id,
        run_id=run.run_id,
        checkpoint_id=checkpoint.checkpoint_id,
        tool_call_digest=approval.request_digest,
        tool_name=approval.tool_name,
        target_path="src/app.py",
        target_existed=True,
        before_sha256=SHA_A,
        expected_after_sha256=SHA_B,
        bytes_written=10,
    )
    ApprovalWorkflow(database).pause_for_approval(
        run,
        checkpoint,
        approval,
        mutation_binding=binding,
        authority=lease.authority,
    )
    return run, approval, binding


def test_pause_persists_safe_binding_and_requested_event_atomically(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    run, approval, binding = make_paused_mutation(database)

    stored = MutationApprovalBindingRepository(database).get_for_approval(
        approval.approval_id
    )
    payload = next(
        event.payload
        for event in EventRepository(database).list_for_run(run.run_id)
        if event.event_type is EventType.MUTATION_REQUESTED
    )

    assert stored == binding
    assert payload["relative_path"] == "src/app.py"
    assert payload["before_sha256"] == SHA_A
    assert "content" not in str(payload).lower()


def test_stale_authority_cannot_prepare_mutation(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "stale.db")
    database.create_schema()
    run, approval, _ = make_paused_mutation(database)
    ApprovalWorkflow._evaluator_only_create(database)._evaluator_only_resolve(
        approval.approval_id,
        ApprovalStatus.APPROVED,
        RejectionStrategy.CONTINUE,
        None,
    )
    leases = RunLeaseStore(database)
    stale = leases.acquire(run.run_id, owner_id="stale", ttl=timedelta(seconds=30))
    leases.release(stale.authority)
    leases.acquire(run.run_id, owner_id="replacement", ttl=timedelta(seconds=30))

    with pytest.raises(StaleFenceError):
        MutationWorkflow(database).ensure_prepared(
            approval.approval_id,
            before_workspace_digest=SHA_A,
            expected_after_workspace_digest=SHA_B,
            authority=stale.authority,
        )

    assert MutationExecutionRepository(database).list_for_run(run.run_id) == []
    database.close()


def test_decide_approval_command_receipt_is_atomic_and_idempotent(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "approval-command.db")
    database.create_schema()
    run, approval, _ = make_paused_mutation(database)
    command = DecideApprovalCommand(
        command_id=uuid4(),
        approval_id=approval.approval_id,
        status=ApprovalStatus.APPROVED,
        strategy=RejectionStrategy.CONTINUE,
        note="approved",
    )

    first = ApprovalWorkflow(database).resolve_command(command)
    repeated = ApprovalWorkflow(database).resolve_command(command)

    assert repeated == first
    with database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        assert receipt is not None
        assert receipt.status == ReceiptStatus.COMPLETED.value
        assert (receipt.result_scope_type, receipt.result_scope_id) == (
            "RUN",
            str(run.run_id),
        )
    changed = command.model_copy(update={"note": "changed"})
    with pytest.raises(IdempotencyConflictError, match="command identity conflicts"):
        ApprovalWorkflow(database).resolve_command(changed)
    database.close()


def test_prepared_creation_and_resume_claim_are_idempotent_and_atomic(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    run, approval, _ = make_paused_mutation(database)
    approval = ApprovalWorkflow._evaluator_only_create(database)._evaluator_only_resolve(
        approval.approval_id,
        ApprovalStatus.APPROVED,
        RejectionStrategy.CONTINUE,
        None,
    )
    workflow = MutationWorkflow._evaluator_only_create(database)
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test", ttl=timedelta(seconds=30)
    )

    first = workflow.ensure_prepared(
        approval.approval_id,
        before_workspace_digest=SHA_A,
        expected_after_workspace_digest=SHA_B,
        authority=lease.authority,
    )
    second = workflow.ensure_prepared(
        approval.approval_id,
        before_workspace_digest=SHA_A,
        expected_after_workspace_digest=SHA_B,
        authority=lease.authority,
    )
    claimed = workflow.claim_resume(
        run.run_id,
        approval.approval_id,
        actual_workspace_digest=SHA_A,
        authority=lease.authority,
    )

    assert first == second
    assert claimed is True
    assert workflow.claim_resume(
        run.run_id,
        approval.approval_id,
        actual_workspace_digest=SHA_A,
        authority=lease.authority,
    ) is False
    stored = MutationExecutionRepository(database).get(first.execution_id)
    assert stored.status is MutationExecutionStatus.WRITING
    assert ApprovalRepository(database).get(approval.approval_id).consumption_state is (
        ApprovalConsumptionState.CLAIMED
    )
    assert RunRepository(database).get(run.run_id).status is RunStatus.RUNNING


def test_concurrent_same_preparation_converges_to_one_record(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    _, approval, _ = make_paused_mutation(database)
    ApprovalWorkflow._evaluator_only_create(database)._evaluator_only_resolve(
        approval.approval_id,
        ApprovalStatus.APPROVED,
        RejectionStrategy.CONTINUE,
        None,
    )
    barrier = Barrier(2)
    lease = RunLeaseStore(database).acquire(
        approval.run_id, owner_id="test", ttl=timedelta(seconds=30)
    )

    def prepare() -> object:
        barrier.wait()
        return MutationWorkflow(database).ensure_prepared(
            approval.approval_id,
            before_workspace_digest=SHA_A,
            expected_after_workspace_digest=SHA_B,
            authority=lease.authority,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        records = list(executor.map(lambda _: prepare(), range(2)))
    assert records[0] == records[1]
    assert len(MutationExecutionRepository(database).list_for_run(approval.run_id)) == 1


def test_existing_preparation_rejects_changed_digest_or_operation(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    _, approval, _ = make_paused_mutation(database)
    ApprovalWorkflow._evaluator_only_create(database)._evaluator_only_resolve(
        approval.approval_id,
        ApprovalStatus.APPROVED,
        RejectionStrategy.CONTINUE,
        None,
    )
    workflow = MutationWorkflow(database)
    lease = RunLeaseStore(database).acquire(
        approval.run_id, owner_id="test", ttl=timedelta(seconds=30)
    )
    workflow.ensure_prepared(
        approval.approval_id,
        before_workspace_digest=SHA_A,
        expected_after_workspace_digest=SHA_B,
        authority=lease.authority,
    )

    with pytest.raises(MutationConflictError, match=r"^mutation execution conflict$"):
        workflow.ensure_prepared(
            approval.approval_id,
            before_workspace_digest=SHA_B,
            expected_after_workspace_digest=SHA_A,
            authority=lease.authority,
        )

    with database.session() as session:
        session.execute(
            update(MutationExecutionRow)
            .where(MutationExecutionRow.approval_id == str(approval.approval_id))
            .values(tool_name="different_operation")
        )
    with pytest.raises(MutationConflictError, match=r"^mutation execution conflict$"):
        workflow.ensure_prepared(
            approval.approval_id,
            before_workspace_digest=SHA_A,
            expected_after_workspace_digest=SHA_B,
            authority=lease.authority,
        )


def test_workflow_commits_only_writing_record_and_emits_safe_event(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    run, approval, _ = make_paused_mutation(database)
    ApprovalWorkflow._evaluator_only_create(database)._evaluator_only_resolve(
        approval.approval_id,
        ApprovalStatus.APPROVED,
        RejectionStrategy.CONTINUE,
        None,
    )
    workflow = MutationWorkflow(database)
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test", ttl=timedelta(seconds=30)
    )
    workflow.ensure_prepared(
        approval.approval_id,
        before_workspace_digest=SHA_A,
        expected_after_workspace_digest=SHA_B,
        authority=lease.authority,
    )
    assert workflow.claim_resume(
        run.run_id,
        approval.approval_id,
        actual_workspace_digest=SHA_A,
        authority=lease.authority,
    )

    committed = workflow.mark_committed(
        run.run_id,
        approval.approval_id,
        actual_after_sha256=SHA_B,
        actual_workspace_digest=SHA_B,
        bytes_written=10,
        duration_ms=12,
        authority=lease.authority,
    )

    assert committed.status is MutationExecutionStatus.COMMITTED
    event = EventRepository(database).list_for_run(run.run_id)[-1]
    assert event.event_type is EventType.MUTATION_COMMITTED
    assert event.payload["after_sha256"] == SHA_B
    with pytest.raises(ResumeNotAllowedError):
        workflow.mark_committed(
            run.run_id,
            approval.approval_id,
            actual_after_sha256=SHA_B,
            actual_workspace_digest=SHA_B,
            bytes_written=10,
            duration_ms=12,
            authority=lease.authority,
        )


def test_writing_crash_marks_execution_approval_and_run_indeterminate(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    run, approval, _ = make_paused_mutation(database)
    ApprovalWorkflow._evaluator_only_create(database)._evaluator_only_resolve(
        approval.approval_id,
        ApprovalStatus.APPROVED,
        RejectionStrategy.CONTINUE,
        None,
    )
    workflow = MutationWorkflow._evaluator_only_create(database)
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test", ttl=timedelta(seconds=30)
    )
    record = workflow.ensure_prepared(
        approval.approval_id,
        before_workspace_digest=SHA_A,
        expected_after_workspace_digest=SHA_B,
        authority=lease.authority,
    )
    assert workflow.claim_resume(
        run.run_id,
        approval.approval_id,
        actual_workspace_digest=SHA_A,
        authority=lease.authority,
    )

    workflow.mark_indeterminate(
        run.run_id,
        approval.approval_id,
        "interrupted write",
        authority=lease.authority,
    )

    assert MutationExecutionRepository(database).get(record.execution_id).status is (
        MutationExecutionStatus.INDETERMINATE
    )
    assert ApprovalRepository(database).get(approval.approval_id).consumption_state is (
        ApprovalConsumptionState.INDETERMINATE
    )
    assert RunRepository(database).get(run.run_id).status is RunStatus.FAILED
    events = EventRepository(database).list_for_run(run.run_id)
    assert events[-2].event_type is EventType.MUTATION_INDETERMINATE
    assert events[-1].event_type is EventType.RUN_FAILED


def test_mutation_claim_rejects_cross_run_identity(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    _, approval, _ = make_paused_mutation(database)
    ApprovalWorkflow._evaluator_only_create(database)._evaluator_only_resolve(
        approval.approval_id,
        ApprovalStatus.APPROVED,
        RejectionStrategy.CONTINUE,
        None,
    )
    workflow = MutationWorkflow(database)
    lease = RunLeaseStore(database).acquire(
        approval.run_id, owner_id="test", ttl=timedelta(seconds=30)
    )
    workflow.ensure_prepared(
        approval.approval_id,
        before_workspace_digest=SHA_A,
        expected_after_workspace_digest=SHA_B,
        authority=lease.authority,
    )

    with pytest.raises(StaleFenceError):
        workflow.claim_resume(
            uuid4(),
            approval.approval_id,
            actual_workspace_digest=SHA_A,
            authority=lease.authority,
        )
