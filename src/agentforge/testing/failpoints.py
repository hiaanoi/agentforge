from __future__ import annotations

from typing import Protocol


class SimulatedProcessCrash(RuntimeError):
    """Test harness signal for a process crash at a named durable boundary."""


class FailpointController(Protocol):
    def hit(self, name: str) -> None: ...


class DisabledFailpoints:
    def hit(self, name: str) -> None:
        del name


class CrashAt:
    def __init__(self, target: str) -> None:
        self._target = target

    def hit(self, name: str) -> None:
        if name == self._target:
            raise SimulatedProcessCrash(name)
