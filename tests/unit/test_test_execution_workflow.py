from datetime import timedelta
from pathlib import Path

import pytest

from agentforge.application.kernel_errors import StaleFenceError
from agentforge.domain.enums import (
    ApprovalStatus,
    ProcessExecutionStatus,
    ProcessFailureKind,
    RejectionStrategy,
    RunStatus,
)
from agentforge.domain.errors import ResumeNotAllowedError
from agentforge.domain.models import ApprovalRequest, Run
from agentforge.domain.test_execution import TestApprovalBinding as ApprovalBinding
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.repositories import (
    ApprovalRepository,
    CheckpointRepository,
    EventRepository,
    RunRepository,
)
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.persistence.test_execution_workflow import (
    TestExecutionWorkflow as ExecutionWorkflow,
)
from agentforge.persistence.test_executions import (
    ProcessExecutionRepository,
)
from agentforge.persistence.test_executions import (
    TestApprovalBindingRepository as BindingRepository,
)

SHA = "a" * 64


def approved_boundary(
    database: Database,
    *,
    task: str = "tests",
    max_tool_calls: int = 3,
) -> tuple[Run, ApprovalRequest, ApprovalBinding]:
    runs = RunRepository(database)
    run = runs.create(Run(task=task, max_tool_calls=max_tool_calls))
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test-setup", ttl=timedelta(seconds=30)
    )
    run.transition_to(RunStatus.RUNNING)
    runs.save(run, authority=lease.authority)
    checkpoint = CheckpointRepository(database).save(
        run.run_id, 1, {"phase": "test"}, authority=lease.authority
    )
    approval = ApprovalRepository(database).create(
        ApprovalRequest(
            run_id=run.run_id,
            checkpoint_id=checkpoint.checkpoint_id,
            tool_name="run_tests",
            sanitized_arguments={"profile_id": "unit_tests"},
            request_digest=SHA,
        )
    )
    binding = BindingRepository(database).create(
        ApprovalBinding(
            approval_id=approval.approval_id,
            run_id=run.run_id,
            checkpoint_id=checkpoint.checkpoint_id,
            tool_call_digest=approval.request_digest,
            profile_id="unit_tests",
            profile_version=1,
            profile_digest=SHA,
            executable_path="C:\\Python\\python.exe",
            argv_digest=SHA,
            cwd="C:\\workspace",
            environment_digest=SHA,
        )
    )
    run.transition_to(RunStatus.WAITING_APPROVAL)
    runs.save(run, authority=lease.authority)
    RunLeaseStore(database).release(lease.authority)
    approval = ApprovalWorkflow._evaluator_only_create(database)._evaluator_only_resolve(
        approval.approval_id,
        ApprovalStatus.APPROVED,
        RejectionStrategy.CONTINUE,
        None,
    )
    return run, approval, binding


def test_created_attempt_is_idempotent_and_budget_counts_only_at_started(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    run, approval, _ = approved_boundary(database)
    workflow = ExecutionWorkflow(database)
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test", ttl=timedelta(seconds=30)
    )

    created = workflow.ensure_created(
        approval.approval_id, authority=lease.authority
    )
    repeated = workflow.ensure_created(
        approval.approval_id, authority=lease.authority
    )

    assert created == repeated
    assert created.attempt_number == 1
    assert created.status is ProcessExecutionStatus.CREATED
    assert RunRepository(database).get(run.run_id).tool_call_count == 0

    assert workflow.claim_resume(
        run.run_id, approval.approval_id, authority=lease.authority
    ) is True
    started = ProcessExecutionRepository(database).get(created.execution_id)
    assert started.status is ProcessExecutionStatus.STARTED
    assert started.record_version == 2
    assert RunRepository(database).get(run.run_id).tool_call_count == 1


def test_stale_authority_cannot_create_process_attempt(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "stale.db")
    database.create_schema()
    run, approval, _ = approved_boundary(database)
    leases = RunLeaseStore(database)
    stale = leases.acquire(run.run_id, owner_id="stale", ttl=timedelta(seconds=30))
    leases.release(stale.authority)
    leases.acquire(run.run_id, owner_id="replacement", ttl=timedelta(seconds=30))

    with pytest.raises(StaleFenceError):
        ExecutionWorkflow(database).ensure_created(
            approval.approval_id, authority=stale.authority
        )

    assert ProcessExecutionRepository(database).list_for_run(run.run_id) == []
    database.close()


def test_attempt_numbers_are_monotonic_per_run(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    run, approval_one, _ = approved_boundary(database)
    workflow = ExecutionWorkflow(database)
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test", ttl=timedelta(seconds=30)
    )
    first = workflow.ensure_created(
        approval_one.approval_id, authority=lease.authority
    )

    runs = RunRepository(database)
    paused = runs.get(run.run_id)
    paused.status = RunStatus.RUNNING
    runs.save(paused, authority=lease.authority)
    checkpoint = CheckpointRepository(database).save(
        run.run_id, 2, {"phase": "second"}, authority=lease.authority
    )
    approval_two = ApprovalRepository(database).create(
        ApprovalRequest(
            run_id=run.run_id,
            checkpoint_id=checkpoint.checkpoint_id,
            tool_name="run_tests",
            sanitized_arguments={"profile_id": "unit_tests"},
            request_digest="b" * 64,
            status=ApprovalStatus.APPROVED,
        )
    )
    BindingRepository(database).create(
        ApprovalBinding(
            approval_id=approval_two.approval_id,
            run_id=run.run_id,
            checkpoint_id=checkpoint.checkpoint_id,
            tool_call_digest=approval_two.request_digest,
            profile_id="unit_tests",
            profile_version=1,
            profile_digest=SHA,
            executable_path="C:\\Python\\python.exe",
            argv_digest=SHA,
            cwd="C:\\workspace",
            environment_digest=SHA,
        )
    )

    second = workflow.ensure_created(
        approval_two.approval_id, authority=lease.authority
    )

    assert (first.attempt_number, second.attempt_number) == (1, 2)


def test_finish_uses_version_cas_and_terminal_fact_cannot_be_overwritten(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    run, approval, _ = approved_boundary(database)
    workflow = ExecutionWorkflow(database)
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test", ttl=timedelta(seconds=30)
    )
    created = workflow.ensure_created(
        approval.approval_id, authority=lease.authority
    )
    assert workflow.claim_resume(
        run.run_id, approval.approval_id, authority=lease.authority
    )
    started = ProcessExecutionRepository(database).get(created.execution_id)

    finished = workflow.finish(
        run.run_id,
        approval.approval_id,
        expected_version=started.record_version,
        status=ProcessExecutionStatus.FAILED,
        failure_kind=ProcessFailureKind.TEST_FAILURE,
        exit_code=1,
        stdout_digest=SHA,
        stderr_digest=SHA,
        stdout_size=12,
        stderr_size=0,
        stdout_summary="one failed",
        stderr_summary="",
        stdout_truncated=False,
        stderr_truncated=False,
        duration_ms=20,
        termination_reason=None,
        termination_result="natural_exit",
        authority=lease.authority,
    )

    assert finished.record_version == started.record_version + 1
    assert finished.failure_kind is ProcessFailureKind.TEST_FAILURE
    with pytest.raises(ResumeNotAllowedError):
        workflow.finish(
            run.run_id,
            approval.approval_id,
            expected_version=started.record_version,
            status=ProcessExecutionStatus.CANCELLED,
            failure_kind=ProcessFailureKind.CANCELLED,
            exit_code=None,
            stdout_digest=SHA,
            stderr_digest=SHA,
            stdout_size=0,
            stderr_size=0,
            stdout_summary="",
            stderr_summary="",
            stdout_truncated=False,
            stderr_truncated=False,
            duration_ms=21,
            termination_reason="cancel",
            termination_result="group_terminated",
            authority=lease.authority,
        )


def test_budget_exhaustion_rolls_back_complete_start_claim(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    run, approval, _ = approved_boundary(database, max_tool_calls=0)
    workflow = ExecutionWorkflow(database)
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test", ttl=timedelta(seconds=30)
    )
    created = workflow.ensure_created(
        approval.approval_id, authority=lease.authority
    )

    with pytest.raises(ResumeNotAllowedError, match="budget"):
        workflow.claim_resume(
            run.run_id, approval.approval_id, authority=lease.authority
        )

    assert ProcessExecutionRepository(database).get(created.execution_id).status is (
        ProcessExecutionStatus.CREATED
    )
    assert ApprovalRepository(database).get(approval.approval_id).consumption_state.value == (
        "NOT_STARTED"
    )
    assert RunRepository(database).get(run.run_id).status is RunStatus.PAUSED
    assert EventRepository(database).list_for_run(run.run_id)[-1].event_type.value != (
        "TEST_STARTED"
    )
