import sys
from asyncio import CancelledError, create_task, to_thread
from datetime import timedelta
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest

from agentforge.application.run_driver import RunOwnership
from agentforge.domain.enums import (
    ApprovalConsumptionState,
    ApprovalStatus,
    ProcessExecutionStatus,
    ProcessFailureKind,
    RejectionStrategy,
    RunStatus,
)
from agentforge.domain.models import ApprovalRequest, Run
from agentforge.domain.test_execution import TestApprovalBinding as ApprovalBinding
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.event_log import RunLeaseAuthority
from agentforge.persistence.product_tables import RunLeaseRow
from agentforge.persistence.repositories import (
    ApprovalRepository,
    CheckpointRepository,
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
from agentforge.process.base import SupervisorOutcome, SupervisorStatus
from agentforge.process.streaming import CapturedStream
from agentforge.runtime.test_execution import (
    TestExecutionCoordinator as ExecutionCoordinator,
)
from agentforge.runtime.test_execution import (
    TestExecutionPreflightFailedError as PreflightFailedError,
)
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.testing.profiles import (
    TestProfileDefinition as ProfileDefinition,
)
from agentforge.tools.testing.profiles import (
    TestProfileRegistry as ProfileRegistry,
)

SHA = "a" * 64


def expire_lease(database: Database, run_id: UUID) -> None:
    with database.session() as session:
        row = session.get(RunLeaseRow, str(run_id))
        assert row is not None
        row.acquired_at -= timedelta(seconds=40)
        row.heartbeat_at -= timedelta(seconds=35)
        row.expires_at -= timedelta(seconds=31)


class FakeSupervisor:
    def __init__(self, outcome: SupervisorOutcome) -> None:
        self.outcome = outcome
        self.calls = 0
        self.cancelled = False

    def run(self, profile: object) -> SupervisorOutcome:
        self.calls += 1
        return self.outcome

    def cancel(self, reason: str = "cancelled") -> bool:
        self.cancelled = True
        return True


class BlockingSupervisor(FakeSupervisor):
    def __init__(self) -> None:
        super().__init__(exited(0))
        self.started = Event()
        self.stopped = Event()

    def run(self, profile: object) -> SupervisorOutcome:
        self.calls += 1
        self.started.set()
        self.stopped.wait(5)
        return SupervisorOutcome(
            **{
                **self.outcome.__dict__,
                "status": SupervisorStatus.CANCELLED,
                "exit_code": None,
                "termination_reason": "resume_task_cancelled",
                "termination_result": "tree_terminated",
            }
        )

    def cancel(self, reason: str = "cancelled") -> bool:
        self.cancelled = True
        self.stopped.set()
        return True


def captured(value: bytes) -> CapturedStream:
    import hashlib

    return CapturedStream(
        retained_bytes=value,
        summary=value.decode(),
        sha256_digest=hashlib.sha256(value).hexdigest(),
        size=len(value),
        truncated=False,
    )


def exited(code: int) -> SupervisorOutcome:
    return SupervisorOutcome(
        status=SupervisorStatus.EXITED,
        root_pid=123,
        process_group_id=None,
        job_id="job-1",
        exit_code=code,
        stdout=captured(b"one failed\n" if code else b"passed\n"),
        stderr=captured(b""),
        duration_ms=25,
        termination_reason=None,
        termination_result="natural_exit",
        termination_confirmed=True,
    )


def make_profiles(workspace: Path, *, version: int = 1) -> ProfileRegistry:
    profiles = ProfileRegistry(WorkspacePathResolver(workspace))
    profiles.register(
        ProfileDefinition(
            profile_id="unit_tests",
            name="Unit tests",
            description="Run unit tests",
            executable=sys.executable,
            argv=("-c", "print('ok')"),
            cwd=".",
            allowed_env={},
            timeout_seconds=30,
            max_output_bytes=4096,
            profile_version=version,
        )
    )
    return profiles


def approved_test(
    database: Database,
    profiles: ProfileRegistry,
) -> tuple[Run, ApprovalRequest, ApprovalBinding, RunLeaseAuthority]:
    runs = RunRepository(database)
    run = runs.create(Run(task="recovery"))
    setup = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test-setup", ttl=timedelta(seconds=30)
    )
    run.transition_to(RunStatus.RUNNING)
    runs.save(run, authority=setup.authority)
    checkpoint = CheckpointRepository(database).save(
        run.run_id, 1, {"phase": "test"}, authority=setup.authority
    )
    plan = profiles.prepare("unit_tests")
    approval = ApprovalRepository(database).create(
        ApprovalRequest(
            run_id=run.run_id,
            checkpoint_id=checkpoint.checkpoint_id,
            tool_name="run_tests",
            sanitized_arguments={"profile_id": "unit_tests"},
            request_digest="f" * 64,
        )
    )
    binding = BindingRepository(database).create(
        ApprovalBinding(
            approval_id=approval.approval_id,
            run_id=run.run_id,
            checkpoint_id=checkpoint.checkpoint_id,
            tool_call_digest=approval.request_digest,
            **plan.model_dump(),
        )
    )
    run.transition_to(RunStatus.WAITING_APPROVAL)
    runs.save(run, authority=setup.authority)
    RunLeaseStore(database).release(setup.authority)
    approval = ApprovalWorkflow._evaluator_only_create(database)._evaluator_only_resolve(
        approval.approval_id,
        ApprovalStatus.APPROVED,
        RejectionStrategy.CONTINUE,
        None,
    )
    worker = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test-worker", ttl=timedelta(seconds=30)
    )
    return run, approval, binding, worker.authority


def coordinator(
    database: Database,
    profiles: ProfileRegistry,
    supervisor: FakeSupervisor,
) -> ExecutionCoordinator:
    return ExecutionCoordinator(
        BindingRepository(database),
        ProcessExecutionRepository(database),
        ExecutionWorkflow._evaluator_only_create(database),
        profiles,
        supervisor_factory=lambda: supervisor,
    )


@pytest.mark.asyncio
async def test_nonzero_test_result_is_recoverable_without_reexecution(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database_path = tmp_path / "runtime.db"
    database = Database.from_path(database_path)
    database.create_schema()
    profiles = make_profiles(workspace)
    run, approval, _, authority = approved_test(database, profiles)
    supervisor = FakeSupervisor(exited(1))
    first = coordinator(database, profiles, supervisor)
    prepared = first.ensure_created(approval.approval_id, authority=authority)
    # ProcessExecutionStatus.CREATED is the durable pre-dispatch/PREPARED state.
    assert prepared.status is ProcessExecutionStatus.CREATED

    result = await first.execute_approved(
        run.run_id,
        approval.approval_id,
        ownership=RunOwnership(lambda: authority),
    )
    expire_lease(database, run.run_id)
    database.close()
    reopened = Database.from_path(database_path)
    replacement_lease = RunLeaseStore(reopened).acquire(
        run.run_id, owner_id="replacement-worker", ttl=timedelta(seconds=30)
    )
    replacement = FakeSupervisor(exited(0))
    recovered = coordinator(
        reopened, make_profiles(workspace), replacement
    ).recover_claimed(
        run.run_id,
        approval.approval_id,
        authority=replacement_lease.authority,
    )

    record = ProcessExecutionRepository(reopened).get_for_approval(approval.approval_id)
    assert result.success is True
    assert result.output["success"] is False
    assert record.status is ProcessExecutionStatus.FAILED
    assert record.failure_kind is ProcessFailureKind.TEST_FAILURE
    assert supervisor.calls == 1
    assert replacement.calls == 0
    assert recovered == result
    assert replacement_lease.fencing_token > authority.fencing_token
    reopened.close()


def test_restart_with_started_execution_becomes_indeterminate(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database_path = tmp_path / "runtime.db"
    database = Database.from_path(database_path)
    database.create_schema()
    profiles = make_profiles(workspace)
    run, approval, _, authority = approved_test(database, profiles)
    first = coordinator(database, profiles, FakeSupervisor(exited(0)))
    first.ensure_created(approval.approval_id, authority=authority)
    assert first.claim_resume(run.run_id, approval.approval_id, authority=authority)

    expire_lease(database, run.run_id)
    database.close()
    reopened = Database.from_path(database_path)
    replacement_lease = RunLeaseStore(reopened).acquire(
        run.run_id, owner_id="replacement-worker", ttl=timedelta(seconds=30)
    )
    replacement = FakeSupervisor(exited(0))
    recovered = coordinator(reopened, make_profiles(workspace), replacement).recover_claimed(
        run.run_id,
        approval.approval_id,
        authority=replacement_lease.authority,
    )

    record = ProcessExecutionRepository(reopened).get_for_approval(approval.approval_id)
    assert recovered is None
    assert record.status is ProcessExecutionStatus.INDETERMINATE
    assert RunRepository(reopened).get(run.run_id).status is RunStatus.FAILED
    assert ApprovalRepository(reopened).get(approval.approval_id).consumption_state is (
        ApprovalConsumptionState.INDETERMINATE
    )
    assert replacement.calls == 0
    assert replacement_lease.fencing_token > authority.fencing_token
    reopened.close()


@pytest.mark.asyncio
async def test_profile_drift_fails_before_process_start(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    original = make_profiles(workspace, version=1)
    run, approval, _, authority = approved_test(database, original)
    changed = make_profiles(workspace, version=2)
    supervisor = FakeSupervisor(exited(0))
    restarted = coordinator(database, changed, supervisor)
    restarted.ensure_created(approval.approval_id, authority=authority)

    with pytest.raises(PreflightFailedError):
        await restarted.execute_approved(
            run.run_id,
            approval.approval_id,
            ownership=RunOwnership(lambda: authority),
        )

    record = ProcessExecutionRepository(database).get_for_approval(approval.approval_id)
    assert record.status is ProcessExecutionStatus.FAILED
    assert record.failure_kind is ProcessFailureKind.PROFILE_MISMATCH
    assert supervisor.calls == 0


def test_coordinator_query_is_run_isolated(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    profiles = make_profiles(workspace)
    run, approval, _, authority = approved_test(database, profiles)
    service = coordinator(database, profiles, FakeSupervisor(exited(0)))
    record = service.ensure_created(approval.approval_id, authority=authority)

    assert service.get_execution(record.execution_id) == record
    assert service.list_executions(run.run_id) == [record]
    assert service.list_executions(uuid4()) == []


@pytest.mark.asyncio
async def test_external_task_cancellation_persists_cancelled_before_propagating(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    profiles = make_profiles(workspace)
    run, approval, _, authority = approved_test(database, profiles)
    supervisor = BlockingSupervisor()
    service = coordinator(database, profiles, supervisor)
    service.ensure_created(approval.approval_id, authority=authority)
    task = create_task(
        service.execute_approved(
            run.run_id,
            approval.approval_id,
            ownership=RunOwnership(lambda: authority),
        )
    )
    assert await to_thread(supervisor.started.wait, 5)

    task.cancel()
    with pytest.raises(CancelledError):
        await task

    record = ProcessExecutionRepository(database).get_for_approval(approval.approval_id)
    assert record.status is ProcessExecutionStatus.CANCELLED
    assert supervisor.cancelled is True
    assert RunRepository(database).get(run.run_id).status is RunStatus.CANCELLED
