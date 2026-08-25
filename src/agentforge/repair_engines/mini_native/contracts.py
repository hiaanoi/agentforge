from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from agentforge.models.base import ModelRequest
from agentforge.models.domain import ModelResponse


class RepairActionKind(StrEnum):
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
        if self.action_id is None:
            object.__setattr__(self, "action_id", _action_id(self))
        return self

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

    @model_validator(mode="before")
    @classmethod
    def bound_history(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        history = data.get("history", ())
        data["history"] = tuple(_bound_history_item(item) for item in list(history)[-100:])
        return data


class CandidatePatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    published: bool
    patch_digest: str | None = None


_SECRET_KEY = re.compile(r"(?:api[_-]?key|token|secret|password|authorization|credential)", re.I)


def _redact(value: Any, key: str | None = None) -> Any:
    if key is not None and _SECRET_KEY.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
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
]
