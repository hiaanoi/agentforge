from __future__ import annotations

import json
import re
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid5

from agentforge.models.base import ModelRequest
from agentforge.models.domain import ModelResponse
from agentforge.repair_engines.mini_native.contracts import (
    CandidatePatchResult,
    MiniNativeState,
    RepairAction,
    RepairActionKind,
    RepairActionResult,
)

_ACTION_NAMESPACE = UUID("5a4c0d2e-a8a0-4cbb-9fb0-5cc7ec2ad6ed")
_SECRET_KEY = re.compile(r"(?:api[_-]?key|token|secret|password|authorization|credential)", re.I)
_SECRET_VALUE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]{8,}|token-[A-Za-z0-9._-]{8,})",
    re.I,
)
MAX_OUTPUT_CHARS = 20_000


@runtime_checkable
class MiniNativeHost(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...

    async def execute(self, action: RepairAction) -> RepairActionResult: ...

    async def checkpoint(self, state: MiniNativeState) -> None: ...

    async def publish(self, run_id: UUID) -> CandidatePatchResult: ...


def classify_action(
    arguments: dict[str, Any], *, tool_name: str | None = None
) -> RepairActionKind:
    """Classify a mini-SWE action payload without executing it."""
    registered_kind = {
        "bash": RepairActionKind.BASH,
        "read_file": RepairActionKind.READ,
        "edit_file": RepairActionKind.WRITE,
        "write_file": RepairActionKind.WRITE,
        "run_tests": RepairActionKind.TEST,
    }.get(tool_name) if tool_name is not None else None
    if registered_kind is not None:
        return registered_kind
    raw = arguments.get("kind", arguments.get("action", arguments.get("type")))
    if isinstance(raw, str):
        try:
            return RepairActionKind(raw.upper())
        except ValueError:
            pass
    if any(key in arguments for key in ("content", "new_content", "patch", "edits")):
        return RepairActionKind.WRITE
    if any(key in arguments for key in ("command", "cmd", "test", "tests")):
        return RepairActionKind.TEST
    if any(key in arguments for key in ("answer", "final")):
        return RepairActionKind.FINAL
    return RepairActionKind.READ


def action_argument_summary(arguments: dict[str, Any]) -> str:
    """Serialize action metadata while redacting values likely to contain secrets."""
    return json.dumps(_redact(arguments), sort_keys=True, separators=(",", ":"), default=str)


def deterministic_action_id(kind: RepairActionKind, arguments: dict[str, Any]) -> UUID:
    payload = f"{kind.value}:{action_argument_summary(arguments)}"
    return uuid5(_ACTION_NAMESPACE, payload)


def bound_output(value: str, *, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if limit < 0:
        raise ValueError("output limit must be non-negative")
    safe = _redact(value)
    return (safe, False) if len(safe) <= limit else (safe[:limit], True)


def _redact(value: Any, key: str | None = None) -> Any:
    if key is not None and _SECRET_KEY.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("<redacted>", value)
    return value


__all__ = [
    "MAX_OUTPUT_CHARS",
    "MiniNativeHost",
    "action_argument_summary",
    "bound_output",
    "classify_action",
    "deterministic_action_id",
]
