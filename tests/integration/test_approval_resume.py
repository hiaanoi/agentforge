import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from agentforge.application.approval_commands import DecideApprovalCommand
from agentforge.application.contracts import OutcomeStatus, ReceiptStatus
from agentforge.application.run_commands import ResumeRun
from agentforge.context.models import LoopState, ResumeContextState
from agentforge.domain.enums import (
    ApprovalStatus,
    EventType,
    RejectionStrategy,
    RunStatus,
    ToolRisk,
)
from agentforge.domain.errors import ApprovalDecisionConflictError, ResumeNotAllowedError
from agentforge.domain.models import ToolResult, ToolSpec
from agentforge.models.base import ModelProvider, ModelRequest
from agentforge.models.mock import MockModelProvider
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.product_tables import ApplicationCommandReceiptRow, RunLeaseRow
from agentforge.persistence.repositories import (
    ApprovalRepository,
    CheckpointRepository,
    EventRepository,
    RunRepository,
)
from agentforge.persistence.resume_workflow import ResumeRunWorkflow
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.persistence.tables import (
    CheckpointRow,
    MutationApprovalBindingRow,
    MutationExecutionRow,
    ProcessExecutionRow,
)
from agentforge.persistence.tables import (
    TestApprovalBindingRow as PersistedTestApprovalBindingRow,
)
from agentforge.policy.engine import PolicyEngine
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.runtime.engine import AgentRuntime
from agentforge.tools.executor import ToolExecutor
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.registry import ToolRegistry


def test_nonexistent_resume_terminalizes_its_accepted_receipt(tmp_path: Path) -> None:
    database = Database.from_path(tmp_path / "missing-resume.sqlite3")
    database.create_schema()
    command = ResumeRun(command_id=uuid4(), run_id=uuid4())
    workflow = ResumeRunWorkflow(database)

    assert workflow.accept(command).status is ReceiptStatus.IN_PROGRESS
    with pytest.raises(Exception, match="does not exist"):
        workflow.prepare(command, owner_id="worker")

    replay = workflow.replay(command)
    assert replay is not None and replay.status is ReceiptStatus.FAILED
    database.close()


class ApprovalProbeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class ApprovalProbeTool:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    @property
    def input_model(self) -> type[BaseModel]:
        return ApprovalProbeArguments

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="approval_probe",
            description="Test-only tool that requires durable approval.",
            input_schema=self.input_model.model_json_schema(),
            risk_level=ToolRisk.READ,
            requires_approval=True,
        )

    def execute(self, arguments: BaseModel) -> ToolResult:
        parsed = ApprovalProbeArguments.model_validate(arguments)
        self._calls.append(parsed.value)
        return ToolResult(success=True, output={"approved_value": parsed.value})


class CrashingApprovalProbeTool(ApprovalProbeTool):
    def execute(self, arguments: BaseModel) -> ToolResult:
        parsed = ApprovalProbeArguments.model_validate(arguments)
        self._calls.append(parsed.value)
        raise SystemExit("simulated process crash after tool side effect")


class CrashBeforeSecondModelResponse:
    def __init__(self, first_response: object) -> None:
        self._first_response = first_response
        self._calls = 0

    @property
    def name(self) -> str:
        return "mock"

    async def generate(self, request: ModelRequest) -> object:
        del request
        self._calls += 1
        if self._calls == 1:
            return self._first_response
        raise SystemExit("simulated crash before next model response")


class BlockSecondModelResponse:
    def __init__(self, first_response: object) -> None:
        self._first_response = first_response
        self._calls = 0
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    @property
    def name(self) -> str:
        return "mock"

    async def generate(self, request: ModelRequest) -> object:
        del request
        self._calls += 1
        if self._calls == 1:
            return self._first_response
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()


def build_runtime(
    path: Path,
    responses: list[object],
    calls: list[str],
    *,
    tool: ApprovalProbeTool | None = None,
    provider: ModelProvider | None = None,
) -> tuple[
    AgentRuntime,
    Database,
    RunRepository,
    ApprovalRepository,
    EventRepository,
]:
    database = Database.from_path(path)
    database.create_schema()
    runs = RunRepository(database)
    approvals = ApprovalRepository(database)
    events = EventRepository(database)
    checkpoints = CheckpointRepository(database)
    resolver = WorkspacePathResolver(path.parent)
    executor = ToolExecutor(
        ToolRegistry([tool or ApprovalProbeTool(calls)]),
        PolicyEngine(resolver, SensitiveFilePolicy()),
        events,
        runs,
    )
    runtime = AgentRuntime(
        run_repository=runs,
        event_repository=events,
        checkpoint_repository=checkpoints,
        model_provider=provider or MockModelProvider(responses),
        tool_executor=executor,
        approval_repository=approvals,
        approval_workflow=ApprovalWorkflow._evaluator_only_create(database),
    )
    return runtime, database, runs, approvals, events


def approval_call(value: str = "approved") -> dict[str, object]:
    return {
        "type": "tool_call",
        "tool": "approval_probe",
        "arguments": {"value": value},
    }


@pytest.mark.asyncio
async def test_approval_pauses_then_executes_once_after_approve(tmp_path: Path) -> None:
    calls: list[str] = []
    runtime, database, runs, _, events = build_runtime(
        tmp_path / "approve.sqlite3",
        [approval_call(), {"type": "final", "answer": "done"}],
        calls,
    )
    run = runtime.create_run("approve tool")

    waiting = await runtime.execute(run.run_id)
    pending = runtime.list_pending_approvals(run.run_id)

    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert len(pending) == 1
    assert calls == []
    approved = runtime.approve(pending[0].approval_id, "looks safe")
    repeated = runtime.approve(pending[0].approval_id, "repeat")
    assert approved.status is ApprovalStatus.APPROVED
    assert repeated == approved
    assert runs.get(run.run_id).status is RunStatus.PAUSED

    completed = await runtime.resume(run.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert calls == ["approved"]
    event_types = [event.event_type for event in events.list_for_run(run.run_id)]
    assert EventType.APPROVAL_REQUESTED in event_types
    assert EventType.APPROVAL_GRANTED in event_types
    assert EventType.RUN_PAUSED in event_types
    assert EventType.RUN_RESUMED in event_types
    with pytest.raises(ResumeNotAllowedError):
        await runtime.resume(run.run_id)
    assert calls == ["approved"]
    database.close()


@pytest.mark.asyncio
async def test_competing_approval_decisions_use_one_cas_winner(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    runtime, database, runs, _, events = build_runtime(
        tmp_path / "approval-cas.sqlite3",
        [approval_call()],
        calls,
    )
    run = runtime.create_run("competing approval decisions")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    barrier = Barrier(2)
    commands = (
        DecideApprovalCommand(
            command_id=uuid4(),
            approval_id=approval.approval_id,
            status=ApprovalStatus.APPROVED,
        ),
        DecideApprovalCommand(
            command_id=uuid4(),
            approval_id=approval.approval_id,
            status=ApprovalStatus.REJECTED,
        ),
    )

    def decide(command: DecideApprovalCommand) -> str:
        barrier.wait()
        try:
            ApprovalWorkflow(database).resolve_command(command)
        except ApprovalDecisionConflictError:
            return "LOSER"
        return "WINNER"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(decide, commands))

    assert sorted(outcomes) == ["LOSER", "WINNER"]
    with database.session() as session:
        receipt_statuses = {
            session.get(ApplicationCommandReceiptRow, str(command.command_id)).status
            for command in commands
        }
    assert receipt_statuses == {
        ReceiptStatus.COMPLETED.value,
        ReceiptStatus.FAILED.value,
    }
    decision_events = [
        event
        for event in events.list_for_run(run.run_id)
        if event.event_type in {EventType.APPROVAL_GRANTED, EventType.APPROVAL_REJECTED}
    ]
    assert len(decision_events) == 1
    assert runs.get(run.run_id).status in {RunStatus.PAUSED, RunStatus.FAILED}
    assert RunLeaseStore(database).current(run.run_id) is None
    loser = commands[outcomes.index("LOSER")]
    with pytest.raises(ApprovalDecisionConflictError, match="terminal"):
        ApprovalWorkflow(database).resolve_command(loser)
    with database.session() as session:
        loser_receipt = session.get(
            ApplicationCommandReceiptRow, str(loser.command_id)
        )
        assert loser_receipt is not None
        assert loser_receipt.status == ReceiptStatus.FAILED.value
    database.close()


@pytest.mark.asyncio
async def test_product_resume_uses_one_claim_and_replays_terminal_receipt(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    runtime, database, _, _, events = build_runtime(
        tmp_path / "product-resume.sqlite3",
        [approval_call(), {"type": "final", "answer": "done"}],
        calls,
    )
    run = runtime.create_run("product resume")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)
    command = ResumeRun(command_id=uuid4(), run_id=run.run_id)

    first = await runtime.resume(command)
    repeated = await runtime.resume(command)

    assert first.outcome is OutcomeStatus.UNVERIFIED
    assert first.value is not None and first.value.status is RunStatus.COMPLETED
    assert repeated.value == first.value
    assert calls == ["approved"]
    with database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        assert receipt is not None and receipt.status == ReceiptStatus.COMPLETED.value
    resumed = [
        event
        for event in events.list_for_run(run.run_id)
        if event.event_type is EventType.RUN_RESUMED
        and event.payload.get("command_id") == str(command.command_id)
    ]
    assert len(resumed) == 1
    assert resumed[0].payload["execution_phase"] == "DECISION"
    database.close()


@pytest.mark.asyncio
async def test_product_resume_lease_loss_returns_unknown_without_run_terminal(
    tmp_path: Path,
) -> None:
    provider = BlockSecondModelResponse(approval_call("blocked"))
    calls: list[str] = []
    runtime, database, runs, _, events = build_runtime(
        tmp_path / "product-resume-unknown.sqlite3",
        [],
        calls,
        provider=provider,
    )
    run = runtime.create_run("lose ownership during provider")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)
    runtime._lease_ttl = timedelta(seconds=5)
    runtime._heartbeat_interval = timedelta(milliseconds=50)
    command = ResumeRun(command_id=uuid4(), run_id=run.run_id)

    task = asyncio.create_task(runtime.resume(command))
    await asyncio.wait_for(provider.started.wait(), timeout=10)
    with database.session() as session:
        lease = session.get(RunLeaseRow, str(run.run_id))
        assert lease is not None
        lease.released_at = lease.heartbeat_at
    result = await asyncio.wait_for(task, timeout=10)

    assert result.outcome is OutcomeStatus.UNKNOWN
    assert result.value is None
    assert provider.cancelled.is_set()
    assert runs.get(run.run_id).status is RunStatus.RUNNING
    with database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        assert receipt is not None and receipt.status == ReceiptStatus.INDETERMINATE.value
    terminal_events = {
        EventType.RUN_COMPLETED,
        EventType.RUN_FAILED,
        EventType.RUN_CANCELLED,
    }
    assert not terminal_events.intersection(
        event.event_type for event in events.list_for_run(run.run_id)
    )
    database.close()


@pytest.mark.asyncio
async def test_decision_claim_crash_takeover_is_unknown_without_side_effect_replay(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    runtime, database, runs, approvals, _ = build_runtime(
        tmp_path / "decision-claim-takeover.sqlite3",
        [approval_call("never-replay")],
        calls,
    )
    run = runtime.create_run("crash after decision claim")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)
    crashed_command = ResumeRun(command_id=uuid4(), run_id=run.run_id)
    ResumeRunWorkflow(database).prepare(
        crashed_command,
        owner_id="crashed-worker",
    )
    with database.session() as session:
        lease = session.get(RunLeaseRow, str(run.run_id))
        assert lease is not None
        lease.acquired_at -= timedelta(seconds=40)
        lease.heartbeat_at -= timedelta(seconds=35)
        lease.expires_at -= timedelta(seconds=31)
    replacement = ResumeRun(command_id=uuid4(), run_id=run.run_id)

    outcome = await runtime.resume(replacement)

    assert outcome.outcome is OutcomeStatus.UNKNOWN
    assert outcome.value is None
    assert calls == []
    assert runs.get(run.run_id).status is RunStatus.FAILED
    assert approvals.get(approval.approval_id).consumption_state.value == "INDETERMINATE"
    with database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(replacement.command_id))
        assert receipt is not None
        assert receipt.status == ReceiptStatus.INDETERMINATE.value
    database.close()


@pytest.mark.asyncio
async def test_claimed_tool_with_unknown_outcome_is_not_retried_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "indeterminate.sqlite3"
    calls: list[str] = []
    crashing_tool = CrashingApprovalProbeTool(calls)
    runtime, database, _, _, _ = build_runtime(
        path,
        [approval_call("once")],
        calls,
        tool=crashing_tool,
    )
    run = runtime.create_run("crash during approved tool")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)

    with pytest.raises(SystemExit):
        await runtime.resume(run.run_id)
    database.close()

    restored, restored_database, _, approvals, _ = build_runtime(
        path,
        [{"type": "final", "answer": "must not execute"}],
        calls,
    )
    failed = await restored.resume(run.run_id)

    assert failed.status is RunStatus.FAILED
    assert calls == ["once"]
    assert approvals.get(approval.approval_id).consumption_state.value == "INDETERMINATE"
    restored_database.close()


@pytest.mark.asyncio
async def test_expired_model_crash_resumes_model_over_historical_side_effect_bindings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "consumed-restart.sqlite3"
    calls: list[str] = []
    provider = CrashBeforeSecondModelResponse(approval_call("persisted"))
    runtime, database, _, _, _ = build_runtime(
        path,
        [],
        calls,
        provider=provider,
    )
    run = runtime.create_run("crash after result checkpoint")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)

    with pytest.raises(SystemExit):
        await runtime.resume(run.run_id)
    assert (
        ApprovalRepository(database).get(approval.approval_id).consumption_state.value
        == "CONSUMED"
    )
    with database.session() as session:
        session.add(
            MutationApprovalBindingRow(
                approval_id=str(approval.approval_id),
                run_id=str(run.run_id),
                checkpoint_id=str(approval.checkpoint_id),
                tool_call_digest="a" * 64,
                tool_name="write_file",
                target_path="historical.txt",
                target_existed=False,
                before_sha256=None,
                expected_after_sha256="b" * 64,
                bytes_written=1,
                created_at=approval.requested_at,
            )
        )
        session.add(
            PersistedTestApprovalBindingRow(
                approval_id=str(approval.approval_id),
                run_id=str(run.run_id),
                checkpoint_id=str(approval.checkpoint_id),
                tool_call_digest="c" * 64,
                profile_id="historical",
                profile_version=1,
                profile_digest="d" * 64,
                executable_path="python",
                argv_digest="e" * 64,
                cwd=".",
                environment_digest="f" * 64,
                created_at=approval.requested_at,
            )
        )
    crashed_lease = RunLeaseStore(database).acquire(
        run.run_id,
        owner_id="crashed-model-worker",
        ttl=timedelta(seconds=30),
    )
    with database.session() as session:
        lease = session.get(RunLeaseRow, str(run.run_id))
        assert lease is not None
        lease.acquired_at -= timedelta(seconds=40)
        lease.heartbeat_at -= timedelta(seconds=35)
        lease.expires_at -= timedelta(seconds=31)
        assert session.scalars(select(MutationExecutionRow)).all() == []
        assert session.scalars(select(ProcessExecutionRow)).all() == []
    database.close()

    restored, restored_database, _, _, _ = build_runtime(
        path,
        [{"type": "final", "answer": "continued"}],
        calls,
    )
    command = ResumeRun(command_id=uuid4(), run_id=run.run_id)
    product_result = await restored.resume(command)
    assert product_result.outcome is OutcomeStatus.UNVERIFIED
    completed = product_result.value
    assert completed is not None

    assert completed.status is RunStatus.COMPLETED
    assert completed.final_output == "continued"
    assert calls == ["persisted"]
    assert completed.tool_call_count == 1
    with restored_database.session() as session:
        receipt = session.get(ApplicationCommandReceiptRow, str(command.command_id))
        lease = session.get(RunLeaseRow, str(run.run_id))
        assert receipt is not None and receipt.status == ReceiptStatus.COMPLETED.value
        assert lease is not None and lease.fencing_token > crashed_lease.fencing_token
        assert session.scalars(select(MutationExecutionRow)).all() == []
        assert session.scalars(select(ProcessExecutionRow)).all() == []
    resumed = [
        event
        for event in EventRepository(restored_database).list_for_run(run.run_id)
        if event.event_type is EventType.RUN_RESUMED
        and event.payload.get("command_id") == str(command.command_id)
    ]
    assert len(resumed) == 1 and resumed[0].payload["phase"] == "MODEL"
    restored_database.close()


@pytest.mark.asyncio
async def test_approval_snapshots_use_v3_and_preserve_recovery_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "approval-v2.sqlite3"
    calls: list[str] = []
    provider = CrashBeforeSecondModelResponse(approval_call("v2"))
    runtime, database, _, _, _ = build_runtime(path, [], calls, provider=provider)
    run = runtime.create_run("preserve v2 approval state")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    checkpoints = CheckpointRepository(database)
    pending = checkpoints.get(approval.checkpoint_id)
    assert pending is not None
    assert pending.runtime_state["schema_version"] == 4

    expected_loop = LoopState(warning_count=1)
    expected_context = ResumeContextState(
        item_count=1,
        character_count=10,
        utf8_bytes=10,
    )
    with database.session() as session:
        row = session.get(CheckpointRow, str(approval.checkpoint_id))
        assert row is not None
        state = dict(row.runtime_state)
        state["loop_state"] = expected_loop.model_dump(mode="json")
        state["context_state"] = expected_context.model_dump(mode="json")
        row.runtime_state = state

    runtime.approve(approval.approval_id)
    with pytest.raises(SystemExit):
        await runtime.resume(run.run_id)

    ready = checkpoints.latest(run.run_id)
    assert ready is not None
    assert ready.runtime_state["schema_version"] == 4
    assert ready.runtime_state["loop_state"] == expected_loop.model_dump(mode="json")
    assert ready.runtime_state["context_state"]["item_count"] == 2
    assert (
        ready.runtime_state["context_state"]["character_count"] > expected_context.character_count
    )
    assert calls == ["v2"]
    database.close()


@pytest.mark.asyncio
async def test_corrupt_approval_snapshot_fails_without_restarting(tmp_path: Path) -> None:
    calls: list[str] = []
    runtime, database, runs, _, _ = build_runtime(
        tmp_path / "corrupt.sqlite3", [approval_call()], calls
    )
    run = runtime.create_run("corrupt checkpoint")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)
    with database.session() as session:
        row = session.get(CheckpointRow, str(approval.checkpoint_id))
        assert row is not None
        row.runtime_state = {"schema_version": 999, "history": []}

    failed = await runtime.resume(run.run_id)

    assert failed.status is RunStatus.FAILED
    assert "checkpoint is invalid" in (runs.get(run.run_id).error_message or "").lower()
    assert calls == []
    database.close()


@pytest.mark.asyncio
async def test_two_runs_keep_approvals_checkpoints_and_events_isolated(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    runtime, database, _, _, events = build_runtime(
        tmp_path / "two-runs.sqlite3",
        [
            approval_call("first"),
            approval_call("second"),
            {"type": "final", "answer": "first complete"},
        ],
        calls,
    )
    first = runtime.create_run("first")
    second = runtime.create_run("second")
    await runtime.execute(first.run_id)
    await runtime.execute(second.run_id)
    first_approval = runtime.list_pending_approvals(first.run_id)[0]

    runtime.approve(first_approval.approval_id)
    completed = await runtime.resume(first.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert runtime.list_pending_approvals(second.run_id)[0].run_id == second.run_id
    assert calls == ["first"]
    assert {event.run_id for event in events.list_for_run(first.run_id)} == {first.run_id}
    assert {event.run_id for event in events.list_for_run(second.run_id)} == {second.run_id}
    database.close()


@pytest.mark.asyncio
async def test_approval_survives_runtime_recreation_before_resume(tmp_path: Path) -> None:
    path = tmp_path / "restart.sqlite3"
    calls: list[str] = []
    first_runtime, first_database, _, _, _ = build_runtime(path, [approval_call("restart")], calls)
    run = first_runtime.create_run("restart approval")
    await first_runtime.execute(run.run_id)
    approval_id = first_runtime.list_pending_approvals(run.run_id)[0].approval_id
    first_runtime.approve(approval_id)
    first_database.close()

    restored, restored_database, _, _, _ = build_runtime(
        path, [{"type": "final", "answer": "restored"}], calls
    )
    completed = await restored.resume(run.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert completed.final_output == "restored"
    assert calls == ["restart"]
    restored_database.close()


@pytest.mark.asyncio
async def test_reject_continue_skips_tool_and_lets_model_change_course(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    runtime, database, _, _, events = build_runtime(
        tmp_path / "reject.sqlite3",
        [approval_call(), {"type": "final", "answer": "alternate path"}],
        calls,
    )
    run = runtime.create_run("reject tool")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]

    rejected = runtime.reject(approval.approval_id)
    decision_events = events.list_for_run(run.run_id)
    assert decision_events[-1].event_type is EventType.TOOL_FAILED
    completed = await runtime.resume(run.run_id)

    assert rejected.status is ApprovalStatus.REJECTED
    assert rejected.rejection_strategy is RejectionStrategy.CONTINUE
    assert completed.status is RunStatus.COMPLETED
    assert completed.final_output == "alternate path"
    assert calls == []
    assert EventType.TOOL_STARTED not in {
        event.event_type for event in events.list_for_run(run.run_id)
    }
    database.close()


@pytest.mark.asyncio
async def test_missing_approval_checkpoint_fails_without_restarting(tmp_path: Path) -> None:
    path = tmp_path / "missing-checkpoint.sqlite3"
    calls: list[str] = []
    runtime, database, _, _, _ = build_runtime(path, [approval_call()], calls)
    run = runtime.create_run("missing checkpoint")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM checkpoints WHERE checkpoint_id = ?",
            (str(approval.checkpoint_id),),
        )

    restored, restored_database, runs, _, _ = build_runtime(path, [], calls)
    failed = await restored.resume(run.run_id)

    assert failed.status is RunStatus.FAILED
    assert "checkpoint is invalid" in (runs.get(run.run_id).error_message or "").lower()
    assert calls == []
    restored_database.close()


@pytest.mark.asyncio
async def test_reject_fail_run_is_terminal_and_conflicting_decision_fails(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    runtime, database, runs, _, _ = build_runtime(
        tmp_path / "reject-fail.sqlite3", [approval_call()], calls
    )
    run = runtime.create_run("reject and fail")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]

    rejected = runtime.reject(
        approval.approval_id,
        strategy=RejectionStrategy.FAIL_RUN,
    )

    assert rejected.status is ApprovalStatus.REJECTED
    assert runs.get(run.run_id).status is RunStatus.FAILED
    with pytest.raises(ApprovalDecisionConflictError):
        runtime.approve(approval.approval_id)
    with pytest.raises(ResumeNotAllowedError):
        await runtime.resume(run.run_id)
    assert calls == []
    database.close()


@pytest.mark.asyncio
async def test_cancel_closes_pending_approval_and_blocks_resume(tmp_path: Path) -> None:
    calls: list[str] = []
    runtime, database, _, approvals, _ = build_runtime(
        tmp_path / "cancel.sqlite3", [approval_call()], calls
    )
    run = runtime.create_run("cancel approval")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]

    cancelled = runtime.cancel(run.run_id, "no longer needed")
    repeated = runtime.cancel(run.run_id)

    assert cancelled.status is RunStatus.CANCELLED
    assert repeated.status is RunStatus.CANCELLED
    assert approvals.get(approval.approval_id).status is ApprovalStatus.CANCELLED
    with pytest.raises(ResumeNotAllowedError):
        await runtime.resume(run.run_id)
    assert calls == []
    database.close()
