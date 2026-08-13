import hashlib
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.application.contracts import OutcomeStatus
from agentforge.application.kernel_errors import SourceRevisionConflictError
from agentforge.application.run_commands import ResumeRun
from agentforge.domain.enums import (
    EventType,
    MutationExecutionStatus,
    RunStatus,
)
from agentforge.domain.errors import ResumeNotAllowedError
from agentforge.models.mock import MockModelProvider
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.mutation_workflow import MutationWorkflow
from agentforge.persistence.mutations import (
    MutationApprovalBindingRepository,
    MutationExecutionRepository,
)
from agentforge.persistence.repositories import (
    ApprovalRepository,
    CheckpointRepository,
    EventRepository,
    RunRepository,
)
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.persistence.source_revisions import WorkspaceDigester
from agentforge.persistence.tables import MutationExecutionRow
from agentforge.policy.engine import PolicyEngine
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.runtime.engine import AgentRuntime
from agentforge.runtime.mutations import (
    EvaluatorOnlyUnboundSourcePolicy,
    MutationCoordinator,
)
from agentforge.tools.executor import ToolExecutor
from agentforge.tools.mutation.base import MutationLimits
from agentforge.tools.mutation.edit_file import EditFileTool
from agentforge.tools.mutation.security import MutationSecurityPolicy
from agentforge.tools.mutation.write_file import WriteFileTool
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.registry import ToolRegistry


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_call(content: str = "created\n", path: str = "created.py") -> dict[str, object]:
    return {
        "type": "tool_call",
        "tool": "write_file",
        "arguments": {
            "path": path,
            "content": content,
            "mode": "CREATE_ONLY",
        },
    }


def _build_runtime(
    database_path: Path,
    workspace: Path,
    responses: list[object],
    *,
    source_policy: EvaluatorOnlyUnboundSourcePolicy | None,
) -> tuple[AgentRuntime, Database, MutationCoordinator]:
    workspace.mkdir(exist_ok=True)
    database = Database.from_path(database_path)
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    approvals = ApprovalRepository(database)
    resolver = WorkspacePathResolver(workspace)
    sensitive = SensitiveFilePolicy()
    security = MutationSecurityPolicy(resolver, sensitive, MutationLimits())
    workflow = (
        MutationWorkflow._evaluator_only_create(database)
        if source_policy is not None
        else MutationWorkflow(database)
    )
    coordinator_arguments: dict[str, object] = {}
    if source_policy is not None:
        coordinator_arguments["source_policy"] = source_policy
    coordinator = MutationCoordinator(
        MutationApprovalBindingRepository(database),
        MutationExecutionRepository(database),
        workflow,
        security,
        **coordinator_arguments,
    )
    executor = ToolExecutor(
        ToolRegistry([WriteFileTool(security), EditFileTool(security)]),
        PolicyEngine(resolver, sensitive),
        events,
        runs,
    )
    runtime = AgentRuntime(
        run_repository=runs,
        event_repository=events,
        checkpoint_repository=CheckpointRepository(database),
        model_provider=MockModelProvider(responses),
        tool_executor=executor,
        approval_repository=approvals,
        approval_workflow=(
            ApprovalWorkflow._evaluator_only_create(database)
            if source_policy is not None
            else ApprovalWorkflow(database)
        ),
        mutation_coordinator=coordinator,
    )
    return runtime, database, coordinator


def build_evaluator_runtime(
    database_path: Path,
    workspace: Path,
    responses: list[object],
) -> tuple[AgentRuntime, Database, MutationCoordinator]:
    return _build_runtime(
        database_path,
        workspace,
        responses,
        source_policy=EvaluatorOnlyUnboundSourcePolicy(),
    )


@pytest.mark.asyncio
async def test_default_coordinator_rejects_unbound_run_before_mutation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    runtime, database, _ = _build_runtime(
        tmp_path / "runtime.db",
        workspace,
        [write_call()],
        source_policy=None,
    )
    run = runtime.create_run("unbound product mutation")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]

    with pytest.raises(SourceRevisionConflictError, match=r"^source revision conflict$"):
        runtime.approve(approval.approval_id)
    assert runtime.list_mutation_executions(run.run_id) == []
    assert not (workspace / "created.py").exists()
    database.close()


def test_evaluator_adapter_is_explicitly_marked_non_product() -> None:
    policy = EvaluatorOnlyUnboundSourcePolicy()
    assert policy.source_revision_semantics == "UNBOUND_EVALUATOR_ONLY"
    assert policy.source_verified is False
    with pytest.raises(TypeError):
        EvaluatorOnlyUnboundSourcePolicy(  # type: ignore[call-arg]
            source_revision_semantics="BOUND_REVISION_V1"
        )


@pytest.mark.asyncio
async def test_approved_mutation_executes_once_and_is_queryable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runtime, database, coordinator = build_evaluator_runtime(
        tmp_path / "runtime.db",
        workspace,
        [write_call(), {"type": "final", "answer": "done"}],
    )
    run = runtime.create_run("create one file")

    waiting = await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    first = runtime.approve(approval.approval_id)
    repeated = runtime.approve(approval.approval_id)

    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert first == repeated
    assert not (workspace / "created.py").exists()
    prepared = runtime.list_mutation_executions(run.run_id)
    assert len(prepared) == 1
    assert prepared[0].status is MutationExecutionStatus.PREPARED
    assert prepared[0].result_summary == (
        "UNBOUND_EVALUATOR_ONLY|source_verified=false: Mutation prepared"
    )

    resume_result = await runtime.resume(ResumeRun(command_id=uuid4(), run_id=run.run_id))
    assert resume_result.outcome is OutcomeStatus.UNVERIFIED
    completed = resume_result.value
    assert completed is not None

    assert completed.status is RunStatus.COMPLETED
    assert (workspace / "created.py").read_text(encoding="utf-8") == "created\n"
    committed = runtime.get_mutation_execution(prepared[0].execution_id)
    assert committed.status is MutationExecutionStatus.COMMITTED
    assert committed.result_summary == (
        "UNBOUND_EVALUATOR_ONLY|source_verified=false: Mutation committed"
    )
    assert committed.actual_after_sha256 == digest(b"created\n")
    replay_lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="evaluator-replay", ttl=timedelta(seconds=30)
    )
    replayed = coordinator.recover_claimed(
        run.run_id, approval.approval_id, authority=replay_lease.authority
    )
    RunLeaseStore(database).release(replay_lease.authority)
    assert replayed is not None
    assert replayed.metadata["source_revision_semantics"] == "UNBOUND_EVALUATOR_ONLY"
    assert replayed.metadata["source_verified"] is False
    with pytest.raises(ResumeNotAllowedError):
        await runtime.resume(run.run_id)
    assert (workspace / "created.py").read_text(encoding="utf-8") == "created\n"

    events = EventRepository(database).list_for_run(run.run_id)
    event_types = [event.event_type for event in events]
    assert EventType.MUTATION_REQUESTED in event_types
    assert EventType.MUTATION_STARTED in event_types
    assert EventType.MUTATION_COMMITTED in event_types
    mutation_payloads = [
        event.payload for event in events if event.event_type.value.startswith("MUTATION_")
    ]
    assert all(
        payload["source_revision_semantics"] == "UNBOUND_EVALUATOR_ONLY"
        and payload["source_verified"] is False
        for payload in mutation_payloads
    )
    assert "created\\n" not in str(mutation_payloads)
    database.close()


@pytest.mark.asyncio
async def test_replayed_tool_result_rejects_summary_audit_injection(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runtime, database, coordinator = build_evaluator_runtime(
        tmp_path / "runtime.db",
        workspace,
        [write_call(), {"type": "final", "answer": "done"}],
    )
    run = runtime.create_run("audit summary injection")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)
    await runtime.resume(run.run_id)
    execution = runtime.list_mutation_executions(run.run_id)[0]

    cases: list[tuple[str | None, str, bool]] = [
        (None, "UNBOUND_EVALUATOR_ONLY", False),
        (
            "BOUND_REVISION_V1|source_verified=false: error body |source_verified=true:",
            "BOUND_REVISION_V1",
            False,
        ),
        (
            "BOUND_REVISION_V1|source_verified=true: body |source_verified=false:",
            "BOUND_REVISION_V1",
            True,
        ),
        (
            "UNBOUND_EVALUATOR_ONLY|source_verified=false: body |source_verified=true:",
            "UNBOUND_EVALUATOR_ONLY",
            False,
        ),
        (
            "malformed\nBOUND_REVISION_V1|source_verified=true: injected",
            "UNBOUND_EVALUATOR_ONLY",
            False,
        ),
        (
            "BOUND_REVISION_V1|source_verified=false: "
            "BOUND_REVISION_V1|source_verified=true: duplicate",
            "UNBOUND_EVALUATOR_ONLY",
            False,
        ),
        (
            "BOUND_REVISION_V1|source_verified=false: body\n"
            "BOUND_REVISION_V1|source_verified=true: second prefix",
            "UNBOUND_EVALUATOR_ONLY",
            False,
        ),
        (
            "UNBOUND_EVALUATOR_ONLY|source_verified=true: impossible",
            "UNBOUND_EVALUATOR_ONLY",
            False,
        ),
        (
            " bound_revision_v1|source_verified=true: loose prefix",
            "UNBOUND_EVALUATOR_ONLY",
            False,
        ),
        (
            "UNKNOWN|source_verified=true: unknown semantics",
            "UNBOUND_EVALUATOR_ONLY",
            False,
        ),
    ]
    replay_lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="evaluator-replay", ttl=timedelta(seconds=30)
    )
    for summary, semantics, verified in cases:
        with database.session() as session:
            row = session.get(MutationExecutionRow, str(execution.execution_id))
            assert row is not None
            row.result_summary = summary
        replayed = coordinator.recover_claimed(
            run.run_id, approval.approval_id, authority=replay_lease.authority
        )
        assert replayed is not None
        assert replayed.metadata["source_revision_semantics"] == semantics
        assert replayed.metadata["source_verified"] is verified
    RunLeaseStore(database).release(replay_lease.authority)
    database.close()


@pytest.mark.asyncio
async def test_rejected_mutation_does_not_write_and_model_can_continue(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runtime, database, _ = build_evaluator_runtime(
        tmp_path / "runtime.db",
        workspace,
        [write_call(), {"type": "final", "answer": "changed course"}],
    )
    run = runtime.create_run("reject write")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]

    runtime.reject(approval.approval_id)
    completed = await runtime.resume(run.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert not (workspace / "created.py").exists()
    assert runtime.list_mutation_executions(run.run_id) == []
    database.close()


@pytest.mark.asyncio
async def test_external_change_after_approval_fails_without_overwrite(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "created.py"
    target.write_text("before", encoding="utf-8")
    runtime, database, _ = build_evaluator_runtime(
        tmp_path / "runtime.db",
        workspace,
        [
            {
                "type": "tool_call",
                "tool": "write_file",
                "arguments": {
                    "path": "created.py",
                    "content": "approved",
                    "mode": "EXPECTED_HASH_REPLACE",
                    "expected_sha256": digest(b"before"),
                },
            }
        ],
    )
    run = runtime.create_run("replace file")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)
    target.write_text("external", encoding="utf-8")

    failed = await runtime.resume(run.run_id)

    assert failed.status is RunStatus.FAILED
    assert target.read_text(encoding="utf-8") == "external"
    assert runtime.list_mutation_executions(run.run_id)[0].status is (
        MutationExecutionStatus.FAILED
    )
    database.close()


@pytest.mark.asyncio
async def test_approved_edit_preflight_failure_is_deterministic(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("before", encoding="utf-8")
    runtime, database, _ = build_evaluator_runtime(
        tmp_path / "runtime.db",
        workspace,
        [
            {
                "type": "tool_call",
                "tool": "edit_file",
                "arguments": {
                    "path": "app.py",
                    "old_text": "before",
                    "new_text": "after",
                    "expected_sha256": digest(b"before"),
                },
            }
        ],
    )
    run = runtime.create_run("reject stale edit")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)
    target.write_text("external", encoding="utf-8")

    failed = await runtime.resume(run.run_id)

    assert failed.status is RunStatus.FAILED
    assert failed.error_message == "Mutation target does not match expected_sha256"
    assert target.read_text(encoding="utf-8") == "external"
    assert runtime.list_mutation_executions(run.run_id)[0].status is (
        MutationExecutionStatus.FAILED
    )
    database.close()


@pytest.mark.asyncio
async def test_approved_edit_file_uses_exact_replacement_once(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_bytes(b"prefix old suffix")
    runtime, database, _ = build_evaluator_runtime(
        tmp_path / "runtime.db",
        workspace,
        [
            {
                "type": "tool_call",
                "tool": "edit_file",
                "arguments": {
                    "path": "app.py",
                    "old_text": "old",
                    "new_text": "new",
                    "expected_sha256": digest(b"prefix old suffix"),
                },
            },
            {"type": "final", "answer": "done"},
        ],
    )
    run = runtime.create_run("edit one occurrence")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)

    completed = await runtime.resume(run.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert target.read_bytes() == b"prefix new suffix"
    assert runtime.list_mutation_executions(run.run_id)[0].status is (
        MutationExecutionStatus.COMMITTED
    )
    database.close()


@pytest.mark.asyncio
async def test_approved_mutation_survives_runtime_recreation_before_resume(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    workspace = tmp_path / "workspace"
    runtime, database, _ = build_evaluator_runtime(path, workspace, [write_call()])
    run = runtime.create_run("restart before resume")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)
    database.close()

    restarted, reopened, _ = build_evaluator_runtime(
        path,
        workspace,
        [{"type": "final", "answer": "done"}],
    )
    completed = await restarted.resume(run.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert (workspace / "created.py").read_text(encoding="utf-8") == "created\n"
    assert restarted.list_mutation_executions(run.run_id)[0].status is (
        MutationExecutionStatus.COMMITTED
    )
    reopened.close()


@pytest.mark.asyncio
async def test_writing_crash_becomes_indeterminate_and_never_writes(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    workspace = tmp_path / "workspace"
    runtime, database, coordinator = build_evaluator_runtime(path, workspace, [write_call()])
    run = runtime.create_run("crash while writing")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)
    crash_lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="crashed-evaluator", ttl=timedelta(seconds=30)
    )
    assert coordinator.claim_resume(
        run.run_id, approval.approval_id, authority=crash_lease.authority
    )
    RunLeaseStore(database).release(crash_lease.authority)
    database.close()

    restarted, reopened, _ = build_evaluator_runtime(path, workspace, [])
    failed = await restarted.resume(run.run_id)

    assert failed.status is RunStatus.FAILED
    assert not (workspace / "created.py").exists()
    assert restarted.list_mutation_executions(run.run_id)[0].status is (
        MutationExecutionStatus.INDETERMINATE
    )
    reopened.close()


@pytest.mark.asyncio
async def test_committed_crash_recovers_result_without_rewriting(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    workspace = tmp_path / "workspace"
    runtime, database, coordinator = build_evaluator_runtime(path, workspace, [write_call()])
    run = runtime.create_run("crash after commit")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)
    crash_lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="crashed-evaluator", ttl=timedelta(seconds=30)
    )
    assert coordinator.claim_resume(
        run.run_id, approval.approval_id, authority=crash_lease.authority
    )
    target = workspace / "created.py"
    target.write_bytes(b"created\n")
    MutationWorkflow(database).mark_committed(
        run.run_id,
        approval.approval_id,
        actual_after_sha256=digest(b"created\n"),
        actual_workspace_digest=WorkspaceDigester().digest(workspace),
        bytes_written=len(b"created\n"),
        duration_ms=1,
        authority=crash_lease.authority,
    )
    RunLeaseStore(database).release(crash_lease.authority)
    before_mtime = target.stat().st_mtime_ns
    database.close()

    restarted, reopened, _ = build_evaluator_runtime(
        path,
        workspace,
        [{"type": "final", "answer": "done"}],
    )
    completed = await restarted.resume(run.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert target.stat().st_mtime_ns == before_mtime
    assert target.read_bytes() == b"created\n"
    reopened.close()


@pytest.mark.asyncio
async def test_cancelled_prepared_mutation_never_executes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runtime, database, _ = build_evaluator_runtime(
        tmp_path / "runtime.db",
        workspace,
        [write_call()],
    )
    run = runtime.create_run("cancel prepared write")
    await runtime.execute(run.run_id)
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)

    cancelled = runtime.cancel(run.run_id, "operator cancelled")

    assert cancelled.status is RunStatus.CANCELLED
    assert not (workspace / "created.py").exists()
    assert runtime.list_mutation_executions(run.run_id)[0].status is (
        MutationExecutionStatus.FAILED
    )
    with pytest.raises(ResumeNotAllowedError):
        await runtime.resume(run.run_id)
    assert not (workspace / "created.py").exists()
    database.close()


@pytest.mark.asyncio
async def test_multiple_runs_keep_mutations_approvals_and_events_isolated(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    runtime, database, _ = build_evaluator_runtime(
        tmp_path / "runtime.db",
        workspace,
        [
            write_call("one", "one.py"),
            write_call("two", "two.py"),
            {"type": "final", "answer": "one done"},
            {"type": "final", "answer": "two done"},
        ],
    )
    first = runtime.create_run("first")
    second = runtime.create_run("second")
    await runtime.execute(first.run_id)
    await runtime.execute(second.run_id)
    first_approval = runtime.list_pending_approvals(first.run_id)[0]
    second_approval = runtime.list_pending_approvals(second.run_id)[0]
    runtime.approve(first_approval.approval_id)
    runtime.approve(second_approval.approval_id)

    assert (await runtime.resume(first.run_id)).status is RunStatus.COMPLETED
    assert (await runtime.resume(second.run_id)).status is RunStatus.COMPLETED

    first_records = runtime.list_mutation_executions(first.run_id)
    second_records = runtime.list_mutation_executions(second.run_id)
    assert [record.target_path for record in first_records] == ["one.py"]
    assert [record.target_path for record in second_records] == ["two.py"]
    assert (workspace / "one.py").read_text(encoding="utf-8") == "one"
    assert (workspace / "two.py").read_text(encoding="utf-8") == "two"
    first_event_runs = {
        event.run_id for event in EventRepository(database).list_for_run(first.run_id)
    }
    second_event_runs = {
        event.run_id for event in EventRepository(database).list_for_run(second.run_id)
    }
    assert first_event_runs == {first.run_id}
    assert second_event_runs == {second.run_id}
    database.close()


@pytest.mark.asyncio
async def test_sensitive_content_fails_before_approval_checkpoint_or_event(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    secret_value = "sk-" + "x" * 24
    runtime, database, _ = build_evaluator_runtime(
        tmp_path / "runtime.db",
        workspace,
        [write_call(f"OPENAI_API_KEY={secret_value}")],
    )
    run = runtime.create_run("reject sensitive mutation")

    failed = await runtime.execute(run.run_id)

    assert failed.status is RunStatus.FAILED
    assert runtime.list_pending_approvals(run.run_id) == []
    assert CheckpointRepository(database).list_for_run(run.run_id) == []
    serialized_events = str(
        [event.payload for event in EventRepository(database).list_for_run(run.run_id)]
    )
    assert secret_value not in serialized_events
    assert not (workspace / "created.py").exists()
    database.close()
