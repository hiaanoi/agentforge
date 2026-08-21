from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agentforge.domain.enums import RunStatus
from agentforge.models.base import ModelRequest
from agentforge.persistence.approval_workflow import ApprovalWorkflow
from agentforge.persistence.database import Database
from agentforge.persistence.repositories import (
    ApprovalRepository,
    CheckpointRepository,
    EventRepository,
    RunRepository,
)
from agentforge.policy.engine import PolicyEngine
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.runtime.candidate_patch import (
    CandidatePatchPublisher,
    CandidatePatchPublishTool,
    CandidatePatchStore,
)
from agentforge.runtime.engine import AgentRuntime
from agentforge.tools.executor import ToolExecutor
from agentforge.tools.mutation.base import MutationLimits
from agentforge.tools.mutation.security import MutationSecurityPolicy
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.registry import ToolRegistry


class PublishThenFinishModel:
    name = "test-model"

    async def generate(self, request: ModelRequest) -> object:
        if request.step_number == 1:
            return {
                "type": "tool_call",
                "tool": "publish_candidate_patch",
                "arguments": {"run_id": str(request.run_id)},
            }
        return {"type": "final", "answer": "candidate patch was published"}


@pytest.mark.asyncio
async def test_final_candidate_patch_pauses_until_approval_then_publishes(
    tmp_path: Path,
) -> None:
    canonical, candidate = _git_workspaces(tmp_path)
    (candidate / "src" / "module.py").write_text("value = 2\n", encoding="utf-8")
    security = MutationSecurityPolicy(
        WorkspacePathResolver(canonical), SensitiveFilePolicy(), MutationLimits()
    )
    publisher = CandidatePatchPublisher(canonical_root=canonical, security=security)
    store = CandidatePatchStore(canonical)
    runtime, database = _runtime(canonical, CandidatePatchPublishTool(publisher, store))
    run = runtime.create_run("publish candidate", max_steps=2)
    store.save(str(run.run_id), publisher.capture(candidate))

    waiting = await runtime.execute(run.run_id)

    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert (canonical / "src" / "module.py").read_text(encoding="utf-8") == "value = 1\n"
    approval = runtime.list_pending_approvals(run.run_id)[0]
    runtime.approve(approval.approval_id)

    completed = await runtime.resume(run.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert (canonical / "src" / "module.py").read_text(encoding="utf-8") == "value = 2\n"
    database.close()


def _runtime(canonical: Path, tool: CandidatePatchPublishTool) -> tuple[AgentRuntime, Database]:
    database = Database.from_path(canonical / ".agentforge" / "agentforge.db")
    database.create_schema()
    runs = RunRepository(database)
    events = EventRepository(database)
    runtime = AgentRuntime(
        run_repository=runs,
        event_repository=events,
        checkpoint_repository=CheckpointRepository(database),
        model_provider=PublishThenFinishModel(),
        tool_executor=ToolExecutor(
            ToolRegistry([tool]),
            PolicyEngine(WorkspacePathResolver(canonical), SensitiveFilePolicy()),
            events,
            runs,
        ),
        approval_repository=ApprovalRepository(database),
        approval_workflow=ApprovalWorkflow._evaluator_only_create(database),
    )
    return runtime, database


def _git_workspaces(tmp_path: Path) -> tuple[Path, Path]:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    (canonical / "src").mkdir()
    (canonical / "src" / "module.py").write_text("value = 1\n", encoding="utf-8")
    _git(canonical, "init")
    _git(canonical, "config", "user.email", "test@example.invalid")
    _git(canonical, "config", "user.name", "Test User")
    _git(canonical, "add", ".")
    _git(canonical, "commit", "-m", "baseline")
    shutil.copytree(canonical, candidate)
    return canonical, candidate


def _git(root: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ("git", *arguments), cwd=root, capture_output=True, check=False, text=True
    )
    assert completed.returncode == 0, completed.stderr
