from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agentforge.domain.enums import RunStatus
from agentforge.domain.models import ApprovalRequest, Run
from agentforge.domain.repair import (
    BudgetKind,
    BudgetProfile,
    RepairDifficulty,
    RepairTaskPolicy,
)
from agentforge.evaluation.auto_approval import (
    AutoApprovalHarness,
    EvaluationWorkspaceHandle,
)
from agentforge.persistence.database import Database
from agentforge.persistence.legacy_evaluator import LegacyEvaluatorEventRepository
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.persistence.repositories import EventRepository, RunRepository
from agentforge.persistence.run_leases import RunLeaseStore


class RecordingRuntime:
    def __init__(self, approval: ApprovalRequest) -> None:
        self.approval = approval
        self.approved: list[UUID] = []
        self.resumed: list[UUID] = []

    def list_pending_approvals(self, run_id: UUID) -> list[ApprovalRequest]:
        return [self.approval] if self.approval.run_id == run_id else []

    def approve(self, approval_id: UUID, note: str | None = None) -> ApprovalRequest:
        self.approved.append(approval_id)
        return self.approval

    async def resume_evaluator(self, run_id: UUID) -> Run:
        self.resumed.append(run_id)
        return Run(run_id=run_id, task="repair", status=RunStatus.COMPLETED)


def policy() -> RepairTaskPolicy:
    return RepairTaskPolicy(
        task_id="auto-approval",
        policy_version=1,
        difficulty=RepairDifficulty.BASIC,
        budget_profile=BudgetProfile.BASIC,
        allowed_write_paths=("src/**",),
        protected_paths=("tests/**",),
        allowed_development_test_profiles=("unit",),
        final_verification_profile_id="hidden",
        allow_file_creation=False,
        max_created_files=0,
        max_changed_files=2,
        max_total_changed_bytes=1024,
        max_single_file_changed_bytes=1024,
        path_case_sensitive=False,
    )


def setup_workflow(
    tmp_path: Path,
) -> tuple[RepairWorkflow, EventRepository, Run, RepairTaskPolicy, Database]:
    database = Database.from_path(tmp_path / "approval.db")
    database.create_schema()
    run = RunRepository(database).create(Run(task="repair"))
    events = LegacyEvaluatorEventRepository(database)
    repairs = RepairWorkflow(database)
    bound_policy = policy()
    repairs._evaluator_only_start(run.run_id, bound_policy, uuid4(), "a" * 64)
    return repairs, events, run, bound_policy, database


def approval(run_id: UUID, tool: str, arguments: dict[str, str]) -> ApprovalRequest:
    return ApprovalRequest(
        run_id=run_id,
        checkpoint_id=uuid4(),
        tool_name=tool,
        sanitized_arguments=arguments,
        request_digest="b" * 64,
    )


@pytest.mark.asyncio
async def test_auto_approval_requires_bound_run_and_policy_digest(tmp_path: Path) -> None:
    repairs, events, run, bound_policy, _ = setup_workflow(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    handle = EvaluationWorkspaceHandle.create(
        workspace,
        run_id=uuid4(),
        policy_digest=bound_policy.policy_digest,
    )
    runtime = RecordingRuntime(approval(run.run_id, "run_tests", {"profile_id": "unit"}))

    with pytest.raises(RuntimeError, match="binding"):
        await AutoApprovalHarness(runtime, events, handle, repairs).process(run.run_id)  # type: ignore[arg-type]

    assert runtime.approved == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("edit_file", {"path": "tests/test_locked.py"}),
        ("write_file", {"path": "src/new.py"}),
        ("run_tests", {"profile_id": "unknown"}),
    ],
)
async def test_auto_approval_rejects_policy_ineligible_requests(
    tmp_path: Path,
    tool: str,
    arguments: dict[str, str],
) -> None:
    repairs, events, run, bound_policy, _ = setup_workflow(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    handle = EvaluationWorkspaceHandle.create(
        workspace,
        run_id=run.run_id,
        policy_digest=bound_policy.policy_digest,
    )
    runtime = RecordingRuntime(approval(run.run_id, tool, arguments))

    with pytest.raises(RuntimeError, match="eligible"):
        await AutoApprovalHarness(runtime, events, handle, repairs).process(run.run_id)  # type: ignore[arg-type]

    assert runtime.approved == []


@pytest.mark.asyncio
async def test_auto_approval_rejects_exhausted_budget(tmp_path: Path) -> None:
    repairs, events, run, bound_policy, database = setup_workflow(tmp_path)
    authority = (
        RunLeaseStore(database)
        .acquire(run.run_id, owner_id="test:auto-approval:budget", ttl=timedelta(seconds=30))
        .authority
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for index in range(bound_policy.max_edit_attempts):
        repairs.consume_budget(run.run_id, BudgetKind.EDIT, f"edit-{index}", authority=authority)
    RunLeaseStore(database).release(authority)
    handle = EvaluationWorkspaceHandle.create(
        workspace,
        run_id=run.run_id,
        policy_digest=bound_policy.policy_digest,
    )
    runtime = RecordingRuntime(approval(run.run_id, "edit_file", {"path": "src/a.py"}))

    with pytest.raises(RuntimeError, match="budget"):
        await AutoApprovalHarness(runtime, events, handle, repairs).process(run.run_id)  # type: ignore[arg-type]

    assert runtime.approved == []


@pytest.mark.asyncio
async def test_hidden_profile_is_only_eligible_when_final_verification_is_pending(
    tmp_path: Path,
) -> None:
    repairs, events, run, bound_policy, database = setup_workflow(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    handle = EvaluationWorkspaceHandle.create(
        workspace,
        run_id=run.run_id,
        policy_digest=bound_policy.policy_digest,
    )
    runtime = RecordingRuntime(approval(run.run_id, "run_tests", {"profile_id": "hidden"}))

    with pytest.raises(RuntimeError, match="eligible"):
        await AutoApprovalHarness(runtime, events, handle, repairs).process(run.run_id)  # type: ignore[arg-type]

    state = repairs.get_state(run.run_id)
    authority = (
        RunLeaseStore(database)
        .acquire(run.run_id, owner_id="test:auto-approval:final", ttl=timedelta(seconds=30))
        .authority
    )
    repairs.mark_pending_final_verification(run.run_id, state.state_version, authority=authority)
    RunLeaseStore(database).release(authority)
    result = await AutoApprovalHarness(runtime, events, handle, repairs).process(run.run_id)  # type: ignore[arg-type]

    assert result.status is RunStatus.COMPLETED
    assert runtime.approved == [runtime.approval.approval_id]
    assert runtime.resumed == [run.run_id]
