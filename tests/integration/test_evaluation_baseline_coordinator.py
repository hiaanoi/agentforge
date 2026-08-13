import hashlib
import sys
from pathlib import Path
from threading import Event

import pytest

from agentforge.domain.models import Run
from agentforge.evaluation.baseline import BaselineExecutionCoordinator
from agentforge.evaluation.baseline_models import (
    BaselineExecutionStatus,
    BaselineFailureReason,
    ExpectedBaselineFailure,
)
from agentforge.evaluation.baseline_persistence import (
    BaselineExecutionRepository,
    BaselineExecutionWorkflow,
)
from agentforge.evaluation.workspace import WorkspaceBaselineBuilder
from agentforge.persistence.database import Database
from agentforge.persistence.repositories import RunRepository
from agentforge.process.base import SupervisorOutcome, SupervisorStatus
from agentforge.process.managed import ManagedTestExecutionCore
from agentforge.process.streaming import CapturedStream
from agentforge.tools.paths import WorkspacePathResolver
from agentforge.tools.testing.profiles import (
    TestProfileDefinition as ManagedProfileDefinition,
)
from agentforge.tools.testing.profiles import TestProfileRegistry as ManagedProfileRegistry


def captured(text: str) -> CapturedStream:
    data = text.encode("utf-8")
    return CapturedStream(
        retained_bytes=data,
        summary=text,
        sha256_digest=hashlib.sha256(data).hexdigest(),
        size=len(data),
        truncated=False,
    )


class FixedSupervisor:
    def __init__(self, *, stdout: str, exit_code: int = 1) -> None:
        self.stdout = stdout
        self.exit_code = exit_code
        self.calls = 0

    def run(self, profile: object) -> SupervisorOutcome:
        del profile
        self.calls += 1
        return SupervisorOutcome(
            status=SupervisorStatus.EXITED,
            root_pid=101,
            process_group_id=None,
            job_id="baseline-job",
            exit_code=self.exit_code,
            stdout=captured(self.stdout),
            stderr=captured(""),
            duration_ms=4,
            termination_reason=None,
            termination_result="natural_exit",
            termination_confirmed=True,
        )

    def cancel(self, reason: str = "cancelled") -> bool:
        del reason
        return True


def make_profiles(workspace: Path, *, version: int = 1) -> ManagedProfileRegistry:
    profiles = ManagedProfileRegistry(WorkspacePathResolver(workspace))
    profiles.register(
        ManagedProfileDefinition(
            profile_id="visible",
            name="Visible tests",
            description="Visible development profile",
            executable=sys.executable,
            argv=("-m", "pytest", "tests/visible", "-q"),
            cwd=".",
            allowed_env={"PYTHONUTF8": "1"},
            timeout_seconds=10,
            max_output_bytes=4096,
            profile_version=version,
        )
    )
    return profiles


def setup(
    tmp_path: Path,
    supervisor: FixedSupervisor,
) -> tuple[Database, Run, BaselineExecutionCoordinator, object]:
    workspace = tmp_path / "workspace"
    (workspace / "tests" / "visible").mkdir(parents=True)
    (workspace / "source.py").write_text("value = 1\n", encoding="utf-8")
    database = Database.from_path(tmp_path / "baseline.db")
    database.create_schema()
    run = RunRepository(database).create(Run(task="repair baseline"))
    baseline = WorkspaceBaselineBuilder(WorkspacePathResolver(workspace)).build(
        task_id="fixture-task"
    )
    coordinator = BaselineExecutionCoordinator(
        BaselineExecutionRepository(database),
        BaselineExecutionWorkflow(database),
        make_profiles(workspace),
        ManagedTestExecutionCore(supervisor_factory=lambda: supervisor),
    )
    return database, run, coordinator, baseline


@pytest.mark.asyncio
async def test_expected_visible_failure_is_verified_once(tmp_path: Path) -> None:
    node = "tests/visible/test_flow.py::test_once"
    supervisor = FixedSupervisor(stdout=f"FAILED {node} - AssertionError\n")
    database, run, coordinator, baseline = setup(tmp_path, supervisor)
    created = coordinator.ensure_created(
        run_id=run.run_id,
        task_id="fixture-task",
        workspace_baseline=baseline,  # type: ignore[arg-type]
        profile_id="visible",
        expected_failure=ExpectedBaselineFailure(failed_node_ids=(node,)),
    )
    repeated = coordinator.ensure_created(
        run_id=run.run_id,
        task_id="fixture-task",
        workspace_baseline=baseline,  # type: ignore[arg-type]
        profile_id="visible",
        expected_failure=ExpectedBaselineFailure(failed_node_ids=(node,)),
    )

    first = await coordinator.execute(run.run_id)
    rebuilt = BaselineExecutionCoordinator(
        BaselineExecutionRepository(database),
        BaselineExecutionWorkflow(database),
        coordinator.profiles,
        ManagedTestExecutionCore(supervisor_factory=lambda: supervisor),
    )
    second = await rebuilt.execute(run.run_id)

    assert created.status is BaselineExecutionStatus.CREATED
    assert repeated == created
    assert first.status is BaselineExecutionStatus.VERIFIED_EXPECTED_FAILURE
    assert second == first
    assert supervisor.calls == 1
    persisted_run = RunRepository(database).get(run.run_id)
    assert persisted_run.tool_call_count == 0
    assert persisted_run.current_step == 0


@pytest.mark.asyncio
async def test_unexpected_pass_blocks_without_retry(tmp_path: Path) -> None:
    supervisor = FixedSupervisor(stdout="1 passed\n", exit_code=0)
    _, run, coordinator, baseline = setup(tmp_path, supervisor)
    coordinator.ensure_created(
        run_id=run.run_id,
        task_id="fixture-task",
        workspace_baseline=baseline,  # type: ignore[arg-type]
        profile_id="visible",
        expected_failure=ExpectedBaselineFailure(
            failed_node_ids=("tests/visible/test_flow.py::test_once",)
        ),
    )

    result = await coordinator.execute(run.run_id)

    assert result.status is BaselineExecutionStatus.BLOCKED
    assert result.failure_reason is BaselineFailureReason.UNEXPECTED_PASS
    assert supervisor.calls == 1


@pytest.mark.asyncio
async def test_profile_binding_drift_blocks_before_process_launch(tmp_path: Path) -> None:
    node = "tests/visible/test_flow.py::test_once"
    supervisor = FixedSupervisor(stdout=f"FAILED {node}\n")
    database, run, coordinator, baseline = setup(tmp_path, supervisor)
    coordinator.ensure_created(
        run_id=run.run_id,
        task_id="fixture-task",
        workspace_baseline=baseline,  # type: ignore[arg-type]
        profile_id="visible",
        expected_failure=ExpectedBaselineFailure(failed_node_ids=(node,)),
    )
    drifted = BaselineExecutionCoordinator(
        BaselineExecutionRepository(database),
        BaselineExecutionWorkflow(database),
        make_profiles(Path(baseline.workspace_root), version=2),  # type: ignore[attr-defined]
        ManagedTestExecutionCore(supervisor_factory=lambda: supervisor),
    )

    result = await drifted.execute(run.run_id)

    assert result.status is BaselineExecutionStatus.BLOCKED
    assert result.failure_reason is BaselineFailureReason.PROFILE_BINDING_MISMATCH
    assert supervisor.calls == 0


class BlockingSupervisor(FixedSupervisor):
    def __init__(self) -> None:
        super().__init__(stdout="")
        self.started = Event()
        self.release = Event()
        self.cancelled = False

    def run(self, profile: object) -> SupervisorOutcome:
        del profile
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=5)
        return SupervisorOutcome(
            status=SupervisorStatus.CANCELLED,
            root_pid=101,
            process_group_id=None,
            job_id="baseline-job",
            exit_code=None,
            stdout=captured(""),
            stderr=captured(""),
            duration_ms=4,
            termination_reason="pilot_cancel",
            termination_result="tree_terminated",
            termination_confirmed=True,
        )

    def cancel(self, reason: str = "cancelled") -> bool:
        self.cancelled = True
        self.release.set()
        return True


@pytest.mark.asyncio
async def test_cancelled_baseline_persists_conclusive_block(tmp_path: Path) -> None:
    import asyncio

    node = "tests/visible/test_flow.py::test_once"
    supervisor = BlockingSupervisor()
    _, run, coordinator, baseline = setup(tmp_path, supervisor)
    coordinator.ensure_created(
        run_id=run.run_id,
        task_id="fixture-task",
        workspace_baseline=baseline,  # type: ignore[arg-type]
        profile_id="visible",
        expected_failure=ExpectedBaselineFailure(failed_node_ids=(node,)),
    )
    running = asyncio.create_task(coordinator.execute(run.run_id))
    assert await asyncio.to_thread(supervisor.started.wait, 2)

    with pytest.raises(RuntimeError, match="already active"):
        await coordinator.execute(run.run_id)
    assert coordinator.cancel(run.run_id, "pilot_cancel") is True
    result = await running

    assert supervisor.cancelled is True
    assert result.status is BaselineExecutionStatus.BLOCKED
    assert result.failure_reason is BaselineFailureReason.CANCELLED
