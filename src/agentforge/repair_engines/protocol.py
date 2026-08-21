from __future__ import annotations

from typing import Protocol

from agentforge.repair_engines.models import RepairEngineKind


class RepairEngine(Protocol):
    @property
    def kind(self) -> RepairEngineKind: ...
