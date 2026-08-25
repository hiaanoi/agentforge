from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Self, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from agentforge.models.base import FinalAnswer, ModelRequest, ToolCall
from agentforge.models.domain import ModelResponse


class RepairActionKind(StrEnum):
    BASH = "BASH"
    READ = "READ"
    WRITE = "WRITE"
    TEST = "TEST"
    FINAL = "FINAL"


class RepairAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    tool_name: str = Field(min_length=1)
    working_directory: str = Field(min_length=1)
    parent_model_call_id: UUID | None = None
    kind: RepairActionKind
    arguments: dict[str, Any] = Field(default_factory=dict)
    arguments_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    approval_key: str | None = None
    action_id: UUID | None = None

    @model_validator(mode="after")
    def normalize_identity(self) -> RepairAction:
        digest = _arguments_digest(self.arguments)
        if self.arguments_digest is not None and self.arguments_digest != digest:
            raise ValueError("arguments_digest does not match arguments")
        object.__setattr__(self, "arguments_digest", digest)
        if self.kind is RepairActionKind.WRITE and not (self.approval_key or "").strip():
            raise ValueError("write actions require a non-empty approval key")
        expected_action_id = _action_id(self)
        if self.action_id is not None and self.action_id != expected_action_id:
            raise ValueError("action_id does not match action identity")
        object.__setattr__(self, "action_id", expected_action_id)
        return self

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        data = self.model_dump()
        if update:
            data.update(update)
        data.pop("action_id", None)
        data.pop("arguments_digest", None)
        return type(self).model_validate(data)

    @property
    def approval_binding_digest(self) -> str:
        """Digest binding an approval to this exact action, run, and arguments."""
        if self.kind is not RepairActionKind.WRITE or self.approval_key is None:
            return ""
        payload = f"{self.run_id}:{self.action_id}:{self.arguments_digest}:{self.approval_key}"
        return hashlib.sha256(payload.encode()).hexdigest()


class RepairActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    returncode: int | None = None
    stdout: str = Field(default="", max_length=20_000)
    stderr: str = Field(default="", max_length=20_000)
    duration_ms: int = Field(ge=0)
    truncated: bool = False

    @model_validator(mode="before")
    @classmethod
    def bound_streams(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        was_truncated = bool(data.get("truncated", False))
        for name in ("stdout", "stderr"):
            stream = _redact(data.get(name, ""))
            if len(stream) > 20_000:
                stream = stream[:20_000]
                was_truncated = True
            data[name] = stream
        data["truncated"] = was_truncated
        return data


class MiniNativeState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    step_number: int = Field(gt=0)
    history: tuple[JsonValue, ...] = ()
    last_test_passed: bool = False

    @model_validator(mode="before")
    @classmethod
    def bound_history(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        history = data.get("history", ())
        data["history"] = tuple(sanitize_history(list(history)))
        return data


class CandidatePatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    published: bool
    patch_digest: str | None = None


_SECRET_KEY = re.compile(r"(?:api[_-]?key|token|secret|password|authorization|credential)", re.I)
_SECRET_VALUE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]{8,}|token-[A-Za-z0-9._-]{8,})",
    re.I,
)


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


def _arguments_digest(arguments: dict[str, Any]) -> str:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _bound_history_item(value: Any) -> JsonValue:
    safe = _redact(value)
    encoded = json.dumps(safe, sort_keys=True, separators=(",", ":"), default=str)
    if len(encoded) <= 20_000:
        return cast(JsonValue, safe)
    return {"summary": encoded[:20_000]}


def sanitize_history(history: list[JsonValue]) -> list[JsonValue]:
    """Redact and bound generic AgentForge history before model reuse."""
    return [_bound_history_item(item) for item in history[-100:]]


def to_repair_action(
    action: ToolCall | FinalAnswer,
    *,
    run_id: UUID,
    step: int,
    working_directory: str,
    parent_model_call_id: UUID | None = None,
) -> RepairAction:
    if isinstance(action, FinalAnswer):
        return RepairAction(
            run_id=run_id,
            parent_model_call_id=parent_model_call_id,
            tool_name="submit",
            working_directory=working_directory,
            kind=RepairActionKind.FINAL,
            arguments={"answer": action.answer},
        )
    arguments = dict(action.arguments)
    kind = _classify_action(arguments, tool_name=action.tool)
    approval_key = f"mini-native-step-{step}" if kind is RepairActionKind.WRITE else None
    return RepairAction(
        run_id=run_id,
        parent_model_call_id=parent_model_call_id,
        tool_name=action.tool,
        working_directory=working_directory,
        kind=kind,
        arguments=arguments,
        approval_key=approval_key,
    )


def action_history_item(
    action: RepairAction,
    *,
    call_id: str | None = None,
    reason: str | None = None,
) -> JsonValue:
    payload: dict[str, Any] = {
        "tool_name": action.tool_name,
        "kind": action.kind.value,
        "arguments": _redact(action.arguments),
    }
    if reason is not None:
        payload["reason"] = reason
    return cast(
        JsonValue,
        {
            "kind": "TOOL_CALL",
            "payload": payload,
            "call_id": call_id if call_id is not None else str(action.action_id),
        },
    )


def _classify_action(
    arguments: dict[str, Any], *, tool_name: str | None = None
) -> RepairActionKind:
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


def _action_id(action: RepairAction) -> UUID:
    from uuid import uuid5

    payload = (
        f"{action.run_id}:{action.tool_name}:{action.working_directory}:"
        f"{action.parent_model_call_id}:{action.kind}:{action.arguments_digest}"
    )
    return uuid5(UUID("5a4c0d2e-a8a0-4cbb-9fb0-5cc7ec2ad6ed"), payload)


__all__ = [
    "CandidatePatchResult",
    "MiniNativeState",
    "ModelRequest",
    "ModelResponse",
    "RepairAction",
    "RepairActionKind",
    "RepairActionResult",
    "action_history_item",
    "sanitize_history",
    "to_repair_action",
]
