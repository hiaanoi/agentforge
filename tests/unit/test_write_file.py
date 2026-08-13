import hashlib
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentforge.application.run_driver import RunOwnership
from agentforge.domain.digests import compute_tool_call_digest
from agentforge.domain.enums import ToolErrorCode, ToolRisk, WriteMode
from agentforge.domain.errors import ToolExecutionError
from agentforge.domain.models import (
    ApprovalAuthorization,
    ApprovalRequired,
    Run,
    ToolSpec,
)
from agentforge.domain.mutations import MutationPlan
from agentforge.persistence.database import Database
from agentforge.persistence.repositories import EventRepository, RunRepository
from agentforge.persistence.run_leases import RunLeaseStore
from agentforge.policy.engine import PolicyEngine
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.tools.executor import ToolExecutor
from agentforge.tools.mutation.atomic import AtomicMutationResult, AtomicMutationWriter
from agentforge.tools.mutation.base import MutationLimits
from agentforge.tools.mutation.security import MutationSecurityPolicy
from agentforge.tools.mutation.write_file import WriteFileArguments, WriteFileTool
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.registry import ToolRegistry


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_tool(workspace: Path) -> WriteFileTool:
    return WriteFileTool(
        MutationSecurityPolicy(
            WorkspacePathResolver(workspace),
            SensitiveFilePolicy(),
            MutationLimits(max_file_bytes=128),
        )
    )


class SlowAtomicWriter(AtomicMutationWriter):
    def apply(
        self,
        target: Path,
        data: bytes,
        plan: MutationPlan,
    ) -> AtomicMutationResult:
        time.sleep(0.03)
        return super().apply(target, data, plan)


class ShortTimeoutWriteFileTool(WriteFileTool):
    @property
    def spec(self) -> ToolSpec:
        return super().spec.model_copy(update={"timeout_seconds": 0.001})


def test_write_file_arguments_bind_mode_and_expected_hash() -> None:
    created = WriteFileArguments(path="new.py", content="new", mode=WriteMode.CREATE_ONLY)
    replaced = WriteFileArguments(
        path="app.py",
        content="new",
        mode=WriteMode.EXPECTED_HASH_REPLACE,
        expected_sha256="a" * 64,
    )

    assert created.expected_sha256 is None
    assert replaced.expected_sha256 == "a" * 64
    with pytest.raises(ValidationError):
        WriteFileArguments(
            path="app.py",
            content="new",
            mode=WriteMode.EXPECTED_HASH_REPLACE,
        )
    with pytest.raises(ValidationError):
        WriteFileArguments(
            path="new.py",
            content="new",
            mode=WriteMode.CREATE_ONLY,
            expected_sha256="a" * 64,
        )


def test_write_file_preflight_is_read_only_and_builds_bound_plan(tmp_path: Path) -> None:
    tool = make_tool(tmp_path)
    arguments = WriteFileArguments(
        path="new.py",
        content="print('new')\n",
        mode=WriteMode.CREATE_ONLY,
    )

    plan = tool.prepare(arguments)

    assert not (tmp_path / "new.py").exists()
    assert plan.target_existed is False
    assert plan.before_sha256 is None
    assert plan.expected_after_sha256 == sha256(arguments.content.encode())


def test_write_file_executes_create_and_hash_checked_replace(tmp_path: Path) -> None:
    tool = make_tool(tmp_path)
    created = WriteFileArguments(path="app.py", content="first\n", mode=WriteMode.CREATE_ONLY)

    create_result = tool.execute(created)
    replace_result = tool.execute(
        WriteFileArguments(
            path="app.py",
            content="second\n",
            mode=WriteMode.EXPECTED_HASH_REPLACE,
            expected_sha256=sha256(b"first\n"),
        )
    )

    assert create_result.success is True
    assert replace_result.success is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "second\n"
    serialized = replace_result.model_dump_json()
    assert "first" not in serialized
    assert "second" not in serialized
    assert replace_result.metadata["after_sha256"] == sha256(b"second\n")


def test_write_file_rejects_stale_or_unconditional_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("current", encoding="utf-8")
    tool = make_tool(tmp_path)

    with pytest.raises(ToolExecutionError) as stale:
        tool.prepare(
            WriteFileArguments(
                path="app.py",
                content="new",
                mode=WriteMode.EXPECTED_HASH_REPLACE,
                expected_sha256="a" * 64,
            )
        )
    with pytest.raises(ToolExecutionError) as existing:
        tool.prepare(
            WriteFileArguments(
                path="app.py",
                content="new",
                mode=WriteMode.CREATE_ONLY,
            )
        )

    assert stale.value.code is ToolErrorCode.MUTATION_HASH_MISMATCH
    assert existing.value.code is ToolErrorCode.MUTATION_TARGET_EXISTS
    assert target.read_text(encoding="utf-8") == "current"


def test_write_file_spec_is_local_write_and_requires_approval(tmp_path: Path) -> None:
    spec = make_tool(tmp_path).spec

    assert spec.risk_level is ToolRisk.WRITE
    assert spec.requires_approval is True
    assert spec.protect_sensitive_path is True


@pytest.mark.asyncio
async def test_tool_executor_prepares_mutation_without_writing_before_approval(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = make_tool(workspace)
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    runs = RunRepository(database)
    run = runs.create(Run(task="write"))
    executor = ToolExecutor(
        ToolRegistry([tool]),
        PolicyEngine(WorkspacePathResolver(workspace), SensitiveFilePolicy()),
        EventRepository(database),
        runs,
    )
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test:write-file", ttl=timedelta(seconds=30)
    )

    outcome = await executor.execute(
        run,
        "write_file",
        {"path": "new.py", "content": "new\n", "mode": "CREATE_ONLY"},
        ownership=RunOwnership(lambda: lease.authority),
    )

    assert isinstance(outcome, ApprovalRequired)
    assert outcome.mutation_plan is not None
    assert outcome.mutation_plan.target_path == "new.py"
    assert not (workspace / "new.py").exists()
    assert run.tool_call_count == 0


@pytest.mark.asyncio
async def test_approved_sync_mutation_never_continues_in_detached_timeout_thread(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    security = MutationSecurityPolicy(
        WorkspacePathResolver(workspace),
        SensitiveFilePolicy(),
        MutationLimits(max_file_bytes=128),
    )
    tool = ShortTimeoutWriteFileTool(security, SlowAtomicWriter())
    database = Database.from_path(tmp_path / "runtime.db")
    database.create_schema()
    runs = RunRepository(database)
    run = runs.create(Run(task="inline write"))
    executor = ToolExecutor(
        ToolRegistry([tool]),
        PolicyEngine(WorkspacePathResolver(workspace), SensitiveFilePolicy()),
        EventRepository(database),
        runs,
    )
    lease = RunLeaseStore(database).acquire(
        run.run_id, owner_id="test:inline-write", ttl=timedelta(seconds=30)
    )
    ownership = RunOwnership(lambda: lease.authority)
    raw_arguments = {"path": "new.py", "content": "new\n", "mode": "CREATE_ONLY"}
    initial = await executor.execute(run, "write_file", raw_arguments, ownership=ownership)
    assert isinstance(initial, ApprovalRequired)
    checkpoint_id = uuid4()
    authorization = ApprovalAuthorization(
        approval_id=uuid4(),
        checkpoint_id=checkpoint_id,
        step_number=1,
        request_digest=compute_tool_call_digest(
            tool_name="write_file",
            validated_arguments=initial.validated_arguments,
            checkpoint_id=checkpoint_id,
            step_number=1,
        ),
    )

    result = await executor.execute(
        run,
        "write_file",
        raw_arguments,
        approval=authorization,
        ownership=ownership,
    )

    assert result.success is True
    assert (workspace / "new.py").read_text(encoding="utf-8") == "new\n"
