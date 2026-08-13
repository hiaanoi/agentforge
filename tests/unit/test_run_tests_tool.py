import sys
from datetime import timedelta
from pathlib import Path

import pytest

from agentforge.application.run_driver import RunOwnership
from agentforge.domain.enums import (
    PolicyOutcome,
    ToolCapability,
    ToolErrorCode,
    ToolRisk,
    ToolSource,
)
from agentforge.domain.errors import ToolExecutionError
from agentforge.domain.models import ApprovalRequired, Run, ToolSpec
from agentforge.domain.test_execution import TestApprovalRequired as ManagedApproval
from agentforge.persistence.database import Database
from agentforge.persistence.repositories import EventRepository, RunRepository
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.policy.engine import PolicyEngine
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.tools.executor import ToolExecutor
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.testing.profiles import (
    TestProfileDefinition as ProfileDefinition,
)
from agentforge.tools.testing.profiles import (
    TestProfileRegistry as ProfileRegistry,
)
from agentforge.tools.testing.run_tests import RunTestsArguments, RunTestsTool


def make_tool(workspace: Path) -> RunTestsTool:
    registry = ProfileRegistry(WorkspacePathResolver(workspace))
    registry.register(
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
    return RunTestsTool(registry)


def test_run_tests_schema_accepts_only_registered_profile_id(tmp_path: Path) -> None:
    tool = make_tool(tmp_path)

    arguments = RunTestsArguments(profile_id="unit_tests")
    plan = tool.prepare(arguments)

    assert plan.profile_id == "unit_tests"
    assert set(tool.input_model.model_fields) == {"profile_id"}
    assert tool.spec.risk_level is ToolRisk.DANGEROUS
    assert tool.spec.source is ToolSource.LOCAL
    assert tool.spec.capability is ToolCapability.TEST_PROFILE_EXECUTION
    assert tool.spec.requires_approval is True
    with pytest.raises(ToolExecutionError, match="managed"):
        tool.execute(arguments)


@pytest.mark.asyncio
async def test_run_tests_requires_approval_without_starting_or_using_budget(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = make_tool(workspace)
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    runs = RunRepository(database)
    run = runs.create(Run(task="run tests"))
    executor = ToolExecutor(
        ToolRegistry([tool]),
        PolicyEngine(WorkspacePathResolver(workspace), SensitiveFilePolicy()),
        EventRepository(database),
        runs,
    )
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test:run-tests", ttl=timedelta(seconds=30)
    )

    outcome = await executor.execute(
        run,
        "run_tests",
        {"profile_id": "unit_tests"},
        ownership=RunOwnership(lambda: lease.authority),
    )

    assert isinstance(outcome, ManagedApproval)
    assert isinstance(outcome, ApprovalRequired)
    assert outcome.test_plan.profile_id == "unit_tests"
    assert run.tool_call_count == 0


@pytest.mark.asyncio
async def test_run_tests_rejects_model_supplied_execution_configuration(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = make_tool(workspace)
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    runs = RunRepository(database)
    run = runs.create(Run(task="invalid tests"))
    executor = ToolExecutor(
        ToolRegistry([tool]),
        PolicyEngine(WorkspacePathResolver(workspace), SensitiveFilePolicy()),
        EventRepository(database),
        runs,
    )
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test:run-tests-invalid", ttl=timedelta(seconds=30)
    )

    outcome = await executor.execute(
        run,
        "run_tests",
        {
            "profile_id": "unit_tests",
            "command": "pytest",
            "argv": ["--pwn"],
            "cwd": "..",
            "env": {"TOKEN": "secret"},
        },
        ownership=RunOwnership(lambda: lease.authority),
    )

    assert not isinstance(outcome, ApprovalRequired)
    assert outcome.success is False
    assert outcome.error_type is ToolErrorCode.INVALID_ARGUMENTS
    assert run.tool_call_count == 0


def test_policy_keeps_all_other_dangerous_tools_denied(tmp_path: Path) -> None:
    policy = PolicyEngine(WorkspacePathResolver(tmp_path), SensitiveFilePolicy())
    run = Run(task="policy")
    arguments = RunTestsArguments(profile_id="unit_tests")
    generic_dangerous = ToolSpec(
        name="dangerous",
        description="Not a registered test execution capability",
        input_schema=RunTestsArguments.model_json_schema(),
        risk_level=ToolRisk.DANGEROUS,
        source=ToolSource.LOCAL,
        requires_approval=True,
    )

    denied = policy.evaluate(
        run,
        generic_dangerous.name,
        generic_dangerous,
        arguments,
        arguments_valid=True,
    )
    test_spec = generic_dangerous.model_copy(
        update={
            "name": "run_tests",
            "capability": ToolCapability.TEST_PROFILE_EXECUTION,
        }
    )
    approval = policy.evaluate(
        run,
        test_spec.name,
        test_spec,
        arguments,
        arguments_valid=True,
    )

    assert denied.decision is PolicyOutcome.DENY
    assert approval.decision is PolicyOutcome.REQUIRE_APPROVAL
