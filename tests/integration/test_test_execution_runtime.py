import hashlib
import sys
from asyncio import create_task, to_thread
from datetime import timedelta
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from agentforge.application.contracts import OutcomeStatus, RunControlRequestStatus
from agentforge.application.run_commands import CancelRun, ResumeRun
from agentforge.domain.enums import EventType, ProcessExecutionStatus, RunStatus
from agentforge.models.mock import MockModelProvider
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.mutation_workflow import MutationWorkflow
from agentforge.persistence.mutations import (
    MutationApprovalBindingRepository,
    MutationExecutionRepository,
)
from agentforge.persistence.product_tables import (
    ApplicationCommandReceiptRow,
    RunLeaseRow,
)
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
from agentforge.policy.engine import PolicyEngine
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.process.base import SupervisorOutcome, SupervisorStatus
from agentforge.process.streaming import CapturedStream
from agentforge.runtime.engine import AgentRuntime
from agentforge.runtime.mutations import (
    EvaluatorOnlyUnboundSourcePolicy,
    MutationCoordinator,
)
from agentforge.runtime.snapshots import load_runtime_snapshot
from agentforge.runtime.test_execution import (
    TestExecutionCoordinator as ExecutionCoordinator,
)
from agentforge.tools.executor import ToolExecutor
from agentforge.tools.mutation.base import MutationLimits
from agentforge.tools.mutation.edit_file import EditFileTool
from agentforge.tools.mutation.security import MutationSecurityPolicy
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.testing.profiles import (
    TestProfileDefinition as ProfileDefinition,
)
from agentforge.tools.testing.profiles import (
    TestProfileRegistry as ProfileRegistry,
)
from agentforge.tools.testing.run_tests import RunTestsTool


class FakeSupervisor:
    def __init__(self, exit_code: int, text: str) -> None:
        self.exit_code = exit_code
        self.text = text
        self.calls = 0

    def run(self, profile: object) -> SupervisorOutcome:
        self.calls += 1
        stdout = self.text.encode()
        return SupervisorOutcome(
            status=SupervisorStatus.EXITED,
            root_pid=100,
            process_group_id=None,
            job_id="job-test",
            exit_code=self.exit_code,
            stdout=CapturedStream(
                retained_bytes=stdout,
                summary=self.text,
                sha256_digest=hashlib.sha256(stdout).hexdigest(),
                size=len(stdout),
                truncated=False,
            ),
            stderr=CapturedStream(
                retained_bytes=b"",
                summary="",
                sha256_digest=hashlib.sha256(b"").hexdigest(),
                size=0,
                truncated=False,
            ),
            duration_ms=10,
            termination_reason=None,
            termination_result="natural_exit",
            termination_confirmed=True,
        )

    def cancel(self, reason: str = "cancelled") -> bool:
        return True


class TimeoutSupervisor(FakeSupervisor):
    def run(self, profile: object) -> SupervisorOutcome:
        outcome = super().run(profile)
        return SupervisorOutcome(
            **{
                **outcome.__dict__,
                "status": SupervisorStatus.TIMEOUT,
                "exit_code": None,
                "termination_reason": "timeout",
                "termination_result": "tree_terminated",
            }
        )


class BlockingSupervisor(FakeSupervisor):
    def __init__(self) -> None:
        super().__init__(0, "cancelled")
        self.started = Event()
        self.stopped = Event()

    def run(self, profile: object) -> SupervisorOutcome:
        self.calls += 1
        self.started.set()
        self.stopped.wait(5)
        stdout = b"cancelled"
        return SupervisorOutcome(
            status=SupervisorStatus.CANCELLED,
            root_pid=200,
            process_group_id=None,
            job_id="job-cancel",
            exit_code=None,
            stdout=CapturedStream(
                retained_bytes=stdout,
                summary="cancelled",
                sha256_digest=hashlib.sha256(stdout).hexdigest(),
                size=len(stdout),
                truncated=False,
            ),
            stderr=CapturedStream(
                retained_bytes=b"",
                summary="",
                sha256_digest=hashlib.sha256(b"").hexdigest(),
                size=0,
                truncated=False,
            ),
            duration_ms=10,
            termination_reason="runtime_cancel",
            termination_result="tree_terminated",
            termination_confirmed=True,
        )

    def cancel(self, reason: str = "cancelled") -> bool:
        self.stopped.set()
        return True


class SlowSupervisor(FakeSupervisor):
    def run(self, profile: object) -> SupervisorOutcome:
        Event().wait(0.8)
        return super().run(profile)


def run_tests_call() -> dict[str, object]:
    return {
        "type": "tool_call",
        "tool": "run_tests",
        "arguments": {"profile_id": "unit_tests"},
    }


def build_runtime(
    database_path: Path,
    workspace: Path,
    responses: list[object],
    supervisor: FakeSupervisor | None,
    *,
    profile_argv: tuple[str, ...] = ("-c", "print('ok')"),
) -> tuple[AgentRuntime, Database]:
    workspace.mkdir(exist_ok=True)
    database = Database.from_path(database_path)
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    resolver = WorkspacePathResolver(workspace)
    profiles = ProfileRegistry(resolver)
    profiles.register(
        ProfileDefinition(
            profile_id="unit_tests",
            name="Unit tests",
            description="Run unit tests",
            executable=sys.executable,
            argv=profile_argv,
            cwd=".",
            allowed_env={},
            timeout_seconds=30,
            max_output_bytes=4096,
            profile_version=1,
        )
    )
    coordinator_arguments = (
        BindingRepository(database),
        ProcessExecutionRepository(database),
        ExecutionWorkflow._evaluator_only_create(database),
        profiles,
    )
    test_coordinator = (
        ExecutionCoordinator(*coordinator_arguments)
        if supervisor is None
        else ExecutionCoordinator(
            *coordinator_arguments,
            supervisor_factory=lambda: supervisor,
        )
    )
    executor = ToolExecutor(
        ToolRegistry([RunTestsTool(profiles)]),
        PolicyEngine(resolver, SensitiveFilePolicy()),
        events,
        runs,
    )
    runtime = AgentRuntime(
        run_repository=runs,
        event_repository=events,
        checkpoint_repository=CheckpointRepository(database),
        model_provider=MockModelProvider(responses),
        tool_executor=executor,
        approval_repository=ApprovalRepository(database),
        approval_workflow=ApprovalWorkflow._evaluator_only_create(database),
        test_execution_coordinator=test_coordinator,
    )
    return runtime, database


def build_edit_and_test_runtime(
    database_path: Path,
    workspace: Path,
    responses: list[object],
    supervisor: FakeSupervisor,
) -> tuple[AgentRuntime, Database]:
    workspace.mkdir(exist_ok=True)
    database = Database.from_path(database_path)
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    resolver = WorkspacePathResolver(workspace)
    sensitive = SensitiveFilePolicy()
    profiles = ProfileRegistry(resolver)
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
            profile_version=1,
        )
    )
    test_coordinator = ExecutionCoordinator(
        BindingRepository(database),
        ProcessExecutionRepository(database),
        ExecutionWorkflow._evaluator_only_create(database),
        profiles,
        supervisor_factory=lambda: supervisor,
    )
    mutation_security = MutationSecurityPolicy(
        resolver,
        sensitive,
        MutationLimits(),
    )
    mutation_coordinator = MutationCoordinator(
        MutationApprovalBindingRepository(database),
        MutationExecutionRepository(database),
        MutationWorkflow._evaluator_only_create(database),
        mutation_security,
        source_policy=EvaluatorOnlyUnboundSourcePolicy(),
    )
    executor = ToolExecutor(
        ToolRegistry([EditFileTool(mutation_security), RunTestsTool(profiles)]),
        PolicyEngine(resolver, sensitive),
        events,
        runs,
    )
    return (
        AgentRuntime(
            run_repository=runs,
            event_repository=events,
            checkpoint_repository=CheckpointRepository(database),
            model_provider=MockModelProvider(responses),
            tool_executor=executor,
            approval_repository=ApprovalRepository(database),
            approval_workflow=ApprovalWorkflow._evaluator_only_create(database),
            mutation_coordinator=mutation_coordinator,
            test_execution_coordinator=test_coordinator,
        ),
        database,
    )


@pytest.mark.asyncio
async def test_approved_test_executes_once_and_returns_result_to_model(
    tmp_path: Path,
) -> None:
    supervisor = FakeSupervisor(1, "one failed")
    runtime, database = build_runtime(
        tmp_path / "runtime.db",
        tmp_path / "workspace",
        [run_tests_call(), {"type": "final", "answer": "test failure analyzed"}],
        supervisor,
    )
    run = runtime.create_run("verify code")
    summaries = runtime.list_test_profiles()

    waiting = await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)
    created = runtime.list_process_executions(run.run_id)[0]

    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert [summary.profile_id for summary in summaries] == ["unit_tests"]
    assert "argv" not in type(summaries[0]).model_fields
    assert "allowed_env" not in type(summaries[0]).model_fields
    assert "executable_path" not in type(summaries[0]).model_fields
    assert created.status is ProcessExecutionStatus.CREATED
    assert RunRepository(database).get(run.run_id).tool_call_count == 0

    resume_result = await runtime.resume(ResumeRun(command_id=uuid4(), run_id=run.run_id))
    assert resume_result.outcome is OutcomeStatus.UNVERIFIED
    completed = resume_result.value
    assert completed is not None

    assert completed.status is RunStatus.COMPLETED
    assert completed.final_output == "test failure analyzed"
    assert supervisor.calls == 1
    record = runtime.get_process_execution(created.execution_id)
    assert record.status is ProcessExecutionStatus.FAILED
    assert RunRepository(database).get(run.run_id).tool_call_count == 1
    checkpoints = CheckpointRepository(database).list_for_run(run.run_id)
    checkpoint_text = str([item.runtime_state for item in checkpoints])
    assert "repair_feedback" in checkpoint_text
    assert "DEVELOPMENT_TEST_FAILURE" in checkpoint_text
    event_types = [event.event_type for event in EventRepository(database).list_for_run(run.run_id)]
    assert EventType.TEST_REQUESTED in event_types
    assert EventType.TEST_STARTED in event_types
    assert EventType.TEST_FAILED in event_types
    assert EventType.TOOL_COMPLETED in event_types


@pytest.mark.asyncio
async def test_approved_test_survives_restart_before_resume(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    workspace = tmp_path / "workspace"
    first_supervisor = FakeSupervisor(0, "passed")
    runtime, database = build_runtime(path, workspace, [run_tests_call()], first_supervisor)
    run = runtime.create_run("restart")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)
    database.close()

    restarted_supervisor = FakeSupervisor(0, "passed")
    restarted, reopened = build_runtime(
        path,
        workspace,
        [{"type": "final", "answer": "done"}],
        restarted_supervisor,
    )
    completed = await restarted.resume(run.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert restarted_supervisor.calls == 1
    assert restarted.list_process_executions(run.run_id)[0].status is (
        ProcessExecutionStatus.COMPLETED
    )
    reopened.close()


@pytest.mark.asyncio
async def test_rejected_test_never_creates_execution_or_calls_supervisor(
    tmp_path: Path,
) -> None:
    supervisor = FakeSupervisor(0, "passed")
    runtime, database = build_runtime(
        tmp_path / "runtime.db",
        tmp_path / "workspace",
        [run_tests_call(), {"type": "final", "answer": "skipped"}],
        supervisor,
    )
    run = runtime.create_run("reject")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]

    runtime.reject(approval.approval_id)
    completed = await runtime.resume(run.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert supervisor.calls == 0
    assert runtime.list_process_executions(run.run_id) == []
    database.close()


@pytest.mark.asyncio
async def test_two_test_requests_create_distinct_monotonic_attempts(
    tmp_path: Path,
) -> None:
    supervisor = FakeSupervisor(0, "passed")
    runtime, database = build_runtime(
        tmp_path / "runtime.db",
        tmp_path / "workspace",
        [
            run_tests_call(),
            run_tests_call(),
            {"type": "final", "answer": "verified twice"},
        ],
        supervisor,
    )
    run = runtime.create_run("repeat tests")
    await runtime.execute(run.run_id)
    first = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(first.approval_id)

    waiting_again = await runtime.resume(run.run_id)
    second = runtime.list_pending_approvals(run.run_id)[0]
    second_checkpoint = CheckpointRepository(database).get(second.checkpoint_id)
    second_snapshot = load_runtime_snapshot(
        second_checkpoint.runtime_state,
        run_id=run.run_id,
        step_number=second_checkpoint.step_number,
    )
    runtime.approve(second.approval_id)
    completed = await runtime.resume(run.run_id)

    assert waiting_again.status is RunStatus.WAITING_APPROVAL
    assert completed.status is RunStatus.COMPLETED
    assert supervisor.calls == 2
    assert second_snapshot.last_test_result is not None
    assert second_snapshot.last_test_result.success is True
    assert second_snapshot.pending_test_execution is not None
    assert second_snapshot.pending_test_execution.approval_id == second.approval_id
    assert [record.attempt_number for record in runtime.list_process_executions(run.run_id)] == [
        1,
        2,
    ]
    database.close()


@pytest.mark.asyncio
async def test_cancelled_created_test_never_starts_process(tmp_path: Path) -> None:
    supervisor = FakeSupervisor(0, "passed")
    runtime, database = build_runtime(
        tmp_path / "runtime.db",
        tmp_path / "workspace",
        [run_tests_call()],
        supervisor,
    )
    run = runtime.create_run("cancel before test")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)

    cancelled = runtime.cancel(
        CancelRun(
            command_id=uuid4(),
            run_id=run.run_id,
            reason="operator cancelled",
        )
    )

    assert cancelled.status is RunControlRequestStatus.REQUESTED
    assert supervisor.calls == 0
    assert runtime.list_process_executions(run.run_id)[0].status is (ProcessExecutionStatus.CREATED)
    database.close()


@pytest.mark.asyncio
async def test_mock_model_edit_then_test_flow_completes_without_automatic_repair(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    supervisor = FakeSupervisor(0, "1 passed")
    runtime, database = build_edit_and_test_runtime(
        tmp_path / "runtime.db",
        workspace,
        [
            {
                "type": "tool_call",
                "tool": "edit_file",
                "arguments": {
                    "path": "app.py",
                    "old_text": "value = 1",
                    "new_text": "value = 2",
                    "expected_sha256": before,
                },
            },
            run_tests_call(),
            {"type": "final", "answer": "change verified"},
        ],
        supervisor,
    )
    run = runtime.create_run("edit and verify")

    await runtime.execute(run.run_id)
    edit_approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(edit_approval.approval_id)
    waiting_for_tests = await runtime.resume(run.run_id)
    test_approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(test_approval.approval_id)
    completed = await runtime.resume(run.run_id)

    assert waiting_for_tests.status is RunStatus.WAITING_APPROVAL
    assert completed.status is RunStatus.COMPLETED
    assert completed.final_output == "change verified"
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert supervisor.calls == 1
    assert runtime.list_process_executions(run.run_id)[0].status is (
        ProcessExecutionStatus.COMPLETED
    )
    database.close()


@pytest.mark.asyncio
async def test_real_process_output_secret_never_enters_record_checkpoint_or_event(
    tmp_path: Path,
) -> None:
    secret = "sk-" + "q" * 40
    runtime, database = build_runtime(
        tmp_path / "runtime.db",
        tmp_path / "workspace",
        [run_tests_call(), {"type": "final", "answer": "redacted"}],
        None,
        profile_argv=("-u", "-c", f"print('OPENAI_API_KEY={secret}')"),
    )
    run = runtime.create_run("redact process output")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)

    completed = await runtime.resume(run.run_id)

    record = runtime.list_process_executions(run.run_id)[0]
    checkpoints = CheckpointRepository(database).list_for_run(run.run_id)
    events = EventRepository(database).list_for_run(run.run_id)
    assert completed.status is RunStatus.COMPLETED
    assert secret not in record.stdout_summary
    assert secret not in str([checkpoint.runtime_state for checkpoint in checkpoints])
    assert secret not in str([event.payload for event in events])
    assert "<redacted>" in record.stdout_summary
    database.close()


@pytest.mark.asyncio
async def test_confirmed_timeout_is_structured_result_not_runtime_failure(
    tmp_path: Path,
) -> None:
    supervisor = TimeoutSupervisor(0, "partial output")
    runtime, database = build_runtime(
        tmp_path / "runtime.db",
        tmp_path / "workspace",
        [run_tests_call(), {"type": "final", "answer": "timeout analyzed"}],
        supervisor,
    )
    run = runtime.create_run("timeout")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)

    completed = await runtime.resume(run.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert completed.final_output == "timeout analyzed"
    assert runtime.list_process_executions(run.run_id)[0].status is (ProcessExecutionStatus.TIMEOUT)
    database.close()


@pytest.mark.asyncio
async def test_runtime_cancel_during_started_test_preserves_cancelled_execution(
    tmp_path: Path,
) -> None:
    supervisor = BlockingSupervisor()
    runtime, database = build_runtime(
        tmp_path / "runtime.db",
        tmp_path / "workspace",
        [run_tests_call()],
        supervisor,
    )
    run = runtime.create_run("cancel active test")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)
    resume_task = create_task(runtime.resume(run.run_id))
    assert await to_thread(supervisor.started.wait, 5)

    command = CancelRun(
        command_id=uuid4(),
        run_id=run.run_id,
        reason="operator cancelled",
    )
    cancelled = runtime.cancel(command)
    resumed = await resume_task

    assert cancelled.status is RunControlRequestStatus.REQUESTED
    assert resumed.status is RunStatus.CANCELLED
    assert runtime.list_process_executions(run.run_id)[0].status is (
        ProcessExecutionStatus.CANCELLED
    )
    assert runtime.cancel(command).status is RunControlRequestStatus.CANCELLED
    database.close()


@pytest.mark.asyncio
async def test_lease_loss_leaves_managed_process_started_for_takeover(
    tmp_path: Path,
) -> None:
    supervisor = BlockingSupervisor()
    runtime, database = build_runtime(
        tmp_path / "lease-loss.db",
        tmp_path / "workspace",
        [run_tests_call()],
        supervisor,
    )
    run = runtime.create_run("lose lease during active test")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)
    runtime._lease_ttl = timedelta(milliseconds=1200)
    runtime._heartbeat_interval = timedelta(milliseconds=100)
    command = ResumeRun(command_id=uuid4(), run_id=run.run_id)
    resume_task = create_task(runtime.resume(command))
    assert await to_thread(supervisor.started.wait, 5)
    lease = RunLeaseStore(database).current(run.run_id)
    assert lease is not None
    RunLeaseStore(database).release(lease.authority)

    result = await resume_task

    assert result.outcome is OutcomeStatus.UNKNOWN
    assert result.value is None
    assert runtime.list_process_executions(run.run_id)[0].status is (
        ProcessExecutionStatus.STARTED
    )
    assert RunRepository(database).get(run.run_id).status is RunStatus.RUNNING
    with database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        assert receipt is not None and receipt.status == "INDETERMINATE"
    event_types = [
        event.event_type for event in EventRepository(database).list_for_run(run.run_id)
    ]
    assert EventType.RUN_FAILED not in event_types
    assert EventType.RUN_CANCELLED not in event_types
    database.close()


@pytest.mark.asyncio
async def test_slow_managed_process_completes_across_many_heartbeats(
    tmp_path: Path,
) -> None:
    supervisor = SlowSupervisor(0, "passed")
    runtime, database = build_runtime(
        tmp_path / "slow-process.db",
        tmp_path / "workspace",
        [run_tests_call(), {"type": "final", "answer": "verified"}],
        supervisor,
    )
    run = runtime.create_run("slow managed process")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)
    runtime._lease_ttl = timedelta(seconds=3)
    runtime._heartbeat_interval = timedelta(milliseconds=50)
    with database.session() as session:
        before = session.get(RunLeaseRow, str(run.run_id))
        assert before is not None
        baseline_version = before.version

    resumed = await runtime.resume(ResumeRun(command_id=uuid4(), run_id=run.run_id))

    assert resumed.outcome is OutcomeStatus.UNVERIFIED
    assert resumed.value is not None and resumed.value.status is RunStatus.COMPLETED
    assert supervisor.calls == 1
    assert runtime.list_process_executions(run.run_id)[0].status is (
        ProcessExecutionStatus.COMPLETED
    )
    with database.session() as session:
        after = session.get(RunLeaseRow, str(run.run_id))
        assert after is not None
        assert after.version >= baseline_version + 3
    database.close()
