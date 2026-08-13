import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock

from agentforge.domain.test_execution import TestProfile
from agentforge.process.base import ProcessTreeSupervisor, SupervisorOutcome
from agentforge.process.runner import create_process_tree_supervisor


class ManagedExecutionOrigin(StrEnum):
    APPROVED_TOOL = "APPROVED_TOOL"
    EVALUATION_BASELINE = "EVALUATION_BASELINE"


@dataclass(frozen=True)
class ManagedExecutionKey:
    origin: ManagedExecutionOrigin
    execution_id: str

    def __post_init__(self) -> None:
        if not self.execution_id or len(self.execution_id) > 200:
            raise ValueError("Managed execution ID must be bounded and non-empty")


@dataclass(frozen=True)
class ManagedExecutionOutcome:
    outcome: SupervisorOutcome
    caller_cancelled: bool = False


class ManagedTestExecutionCore:
    def __init__(
        self,
        *,
        supervisor_factory: Callable[[], ProcessTreeSupervisor] = (create_process_tree_supervisor),
    ) -> None:
        self._supervisor_factory = supervisor_factory
        self._active: dict[ManagedExecutionKey, ProcessTreeSupervisor] = {}
        self._active_lock = Lock()

    async def execute(
        self,
        key: ManagedExecutionKey,
        profile: TestProfile,
    ) -> ManagedExecutionOutcome:
        supervisor = self._supervisor_factory()
        with self._active_lock:
            if key in self._active:
                raise RuntimeError("Managed execution is already active")
            self._active[key] = supervisor
        worker = asyncio.create_task(asyncio.to_thread(supervisor.run, profile))
        caller_cancelled = False
        try:
            outcome = await asyncio.shield(worker)
        except asyncio.CancelledError:
            caller_cancelled = True
            supervisor.cancel("execution_task_cancelled")
            outcome = await asyncio.shield(worker)
        finally:
            with self._active_lock:
                self._active.pop(key, None)
        return ManagedExecutionOutcome(
            outcome=outcome,
            caller_cancelled=caller_cancelled,
        )

    def cancel(self, key: ManagedExecutionKey, reason: str) -> bool:
        with self._active_lock:
            supervisor = self._active.get(key)
        return supervisor.cancel(reason) if supervisor is not None else False
