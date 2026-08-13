import asyncio
import hashlib
from threading import Event

import pytest

from agentforge.domain.enums import ConfigSourceKind
from agentforge.domain.test_execution import TestProfile as ManagedTestProfile
from agentforge.process.base import SupervisorOutcome, SupervisorStatus
from agentforge.process.managed import (
    ManagedExecutionKey,
    ManagedExecutionOrigin,
    ManagedTestExecutionCore,
)
from agentforge.process.streaming import CapturedStream

SHA = "a" * 64


def profile() -> ManagedTestProfile:
    return ManagedTestProfile(
        profile_id="visible",
        name="Visible",
        description="Visible tests",
        executable_path="C:\\Python314\\python.exe",
        argv=("C:\\Python314\\python.exe", "-m", "pytest"),
        cwd="C:\\workspace",
        allowed_env={},
        timeout_seconds=10,
        max_output_bytes=1000,
        profile_version=1,
        executable_digest=SHA,
        argv_digest=SHA,
        cwd_digest=SHA,
        environment_digest=SHA,
        config_source_kind=ConfigSourceKind.PROJECT,
        config_source_identity="managed-test-core",
        config_source_digest=SHA,
        profile_digest=SHA,
    )


def empty_stream() -> CapturedStream:
    return CapturedStream(
        retained_bytes=b"",
        summary="",
        sha256_digest=hashlib.sha256(b"").hexdigest(),
        size=0,
        truncated=False,
    )


class BlockingSupervisor:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.cancel_reason: str | None = None

    def run(self, bound_profile: ManagedTestProfile) -> SupervisorOutcome:
        del bound_profile
        self.started.set()
        self.release.wait(timeout=5)
        cancelled = self.cancel_reason is not None
        return SupervisorOutcome(
            status=(SupervisorStatus.CANCELLED if cancelled else SupervisorStatus.EXITED),
            root_pid=123,
            process_group_id=None,
            job_id="job-managed",
            exit_code=None if cancelled else 0,
            stdout=empty_stream(),
            stderr=empty_stream(),
            duration_ms=1,
            termination_reason=self.cancel_reason,
            termination_result="terminated" if cancelled else "natural_exit",
            termination_confirmed=True,
        )

    def cancel(self, reason: str = "cancelled") -> bool:
        self.cancel_reason = reason
        self.release.set()
        return True


@pytest.mark.asyncio
async def test_core_returns_outcome_and_releases_execution_key() -> None:
    supervisors: list[BlockingSupervisor] = []

    def factory() -> BlockingSupervisor:
        item = BlockingSupervisor()
        item.release.set()
        supervisors.append(item)
        return item

    core = ManagedTestExecutionCore(supervisor_factory=factory)
    key = ManagedExecutionKey(
        origin=ManagedExecutionOrigin.EVALUATION_BASELINE,
        execution_id="baseline-1",
    )

    first = await core.execute(key, profile())
    second = await core.execute(key, profile())

    assert first.outcome.status is SupervisorStatus.EXITED
    assert second.outcome.status is SupervisorStatus.EXITED
    assert len(supervisors) == 2


@pytest.mark.asyncio
async def test_core_rejects_duplicate_active_key_and_cancels_supervisor() -> None:
    supervisor = BlockingSupervisor()
    core = ManagedTestExecutionCore(supervisor_factory=lambda: supervisor)
    key = ManagedExecutionKey(
        origin=ManagedExecutionOrigin.APPROVED_TOOL,
        execution_id="approval-1",
    )
    running = asyncio.create_task(core.execute(key, profile()))
    await asyncio.to_thread(supervisor.started.wait, 2)

    with pytest.raises(RuntimeError, match="already active"):
        await core.execute(key, profile())
    assert core.cancel(key, "test_cancel") is True
    result = await running

    assert result.outcome.status is SupervisorStatus.CANCELLED
    assert result.caller_cancelled is False
    assert supervisor.cancel_reason == "test_cancel"
    assert core.cancel(key, "late") is False
