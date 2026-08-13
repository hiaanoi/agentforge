import hashlib
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agentforge.application.run_driver import RunOwnership
from agentforge.domain.enums import RunStatus, ToolErrorCode
from agentforge.domain.models import ApprovalRequired, Run
from agentforge.domain.repair import (
    BudgetProfile,
    RepairCompletionStatus,
    RepairDifficulty,
    RepairTaskPolicy,
)
from agentforge.persistence.database import Database
from agentforge.persistence.repair_workflow import RepairWorkflow
from agentforge.persistence.repositories import EventRepository, RunRepository
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.policy.engine import PolicyEngine
from agentforge.policy.repair import RepairPolicyEnforcer
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.tools.executor import ToolExecutor
from agentforge.tools.mutation.base import MutationLimits
from agentforge.tools.mutation.edit_file import EditFileTool
from agentforge.tools.mutation.security import MutationSecurityPolicy
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.testing.profiles import TestProfileDefinition as ProfileDefinition
from agentforge.tools.testing.profiles import TestProfileRegistry as ProfileRegistry
from agentforge.tools.testing.run_tests import RunTestsTool


def policy() -> RepairTaskPolicy:
    return RepairTaskPolicy(
        task_id="policy-integration",
        policy_version=1,
        difficulty=RepairDifficulty.BASIC,
        budget_profile=BudgetProfile.BASIC,
        allowed_write_paths=("src/**",),
        forbidden_write_paths=("src/generated/**",),
        protected_paths=("tests/**",),
        allowed_development_test_profiles=("unit",),
        final_verification_profile_id="hidden",
        allow_file_creation=False,
        max_created_files=0,
        max_changed_files=2,
        max_total_changed_bytes=4096,
        max_single_file_changed_bytes=2048,
        path_case_sensitive=False,
    )


def build_executor(
    tmp_path: Path,
) -> tuple[ToolExecutor, RepairWorkflow, Run, ProfileRegistry, Path, RunOwnership]:
    workspace = tmp_path / "workspace"
    (workspace / "src" / "generated").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "src" / "module.py").write_text("value = 1\n", encoding="utf-8")
    protected = workspace / "tests" / "test_module.py"
    protected.write_text("def test_value(): pass\n", encoding="utf-8")
    resolver = WorkspacePathResolver(workspace)
    profiles = ProfileRegistry(resolver)
    for profile_id in ("unit", "other", "hidden"):
        profiles.register(
            ProfileDefinition(
                profile_id=profile_id,
                name=profile_id,
                description=f"{profile_id} tests",
                executable=sys.executable,
                argv=("-c", "print('ok')"),
                cwd=".",
                allowed_env={},
                timeout_seconds=10,
                max_output_bytes=1024,
                profile_version=1,
            )
        )
    database = Database.from_path(tmp_path / "repair-policy.db")
    database.create_schema()
    runs = RunRepository(database)
    run = Run(task="repair", status=RunStatus.RUNNING)
    runs.create(run)
    workflow = RepairWorkflow(database)
    workflow._evaluator_only_start(run.run_id, policy(), uuid4(), "a" * 64)
    mutation_security = MutationSecurityPolicy(
        resolver,
        SensitiveFilePolicy(),
        MutationLimits(),
    )
    executor = ToolExecutor(
        ToolRegistry(
            [
                EditFileTool(mutation_security),
                RunTestsTool(profiles),
            ]
        ),
        PolicyEngine(resolver, SensitiveFilePolicy()),
        EventRepository(database),
        runs,
        repair_guard=RepairPolicyEnforcer(workflow),
    )
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test:repair-policy", ttl=timedelta(seconds=30)
    )
    return (
        executor,
        workflow,
        run,
        profiles,
        protected,
        RunOwnership(lambda: lease.authority),
    )


@pytest.mark.asyncio
async def test_protected_edit_is_denied_before_approval(tmp_path: Path) -> None:
    executor, workflow, run, _, protected, ownership = build_executor(tmp_path)
    before = protected.read_bytes()

    result = await executor.execute(
        run,
        "edit_file",
        {
            "path": "tests/test_module.py",
            "old_text": "pass",
            "new_text": "assert True",
            "expected_sha256": hashlib.sha256(before).hexdigest(),
        },
        ownership=ownership,
    )

    assert not isinstance(result, ApprovalRequired)
    assert not result.success
    assert result.error_type is ToolErrorCode.REPAIR_POLICY_DENIED
    assert protected.read_bytes() == before
    assert workflow.get_state(run.run_id).status is RepairCompletionStatus.POLICY_BLOCKED


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_id", ["other", "hidden"])
async def test_disallowed_or_hidden_test_profile_is_denied_before_approval(
    tmp_path: Path,
    profile_id: str,
) -> None:
    executor, workflow, run, _, _, ownership = build_executor(tmp_path)

    result = await executor.execute(
        run, "run_tests", {"profile_id": profile_id}, ownership=ownership
    )

    assert not isinstance(result, ApprovalRequired)
    assert not result.success
    assert result.error_type is ToolErrorCode.REPAIR_POLICY_DENIED
    assert workflow.get_state(run.run_id).policy_violations == 1


@pytest.mark.asyncio
async def test_allowed_edit_and_development_profile_still_require_approval(
    tmp_path: Path,
) -> None:
    executor, _, run, _, _, ownership = build_executor(tmp_path)
    target = tmp_path / "workspace" / "src" / "module.py"

    edit = await executor.execute(
        run,
        "edit_file",
        {
            "path": "src/module.py",
            "old_text": "value = 1",
            "new_text": "value = 2",
            "expected_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        },
        ownership=ownership,
    )
    tests = await executor.execute(run, "run_tests", {"profile_id": "unit"}, ownership=ownership)

    assert isinstance(edit, ApprovalRequired)
    assert isinstance(tests, ApprovalRequired)
