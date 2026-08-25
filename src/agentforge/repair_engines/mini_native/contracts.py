from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentforge.models.base import ModelRequest
from agentforge.models.domain import ModelResponse


class RepairActionKind(StrEnum):
    READ = "READ"
    WRITE = "WRITE"
    TEST = "TEST"
    FINAL = "FINAL"


class RepairAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: UUID | None = None
    kind: RepairActionKind
    arguments: dict[str, Any] = Field(default_factory=dict)
    approval_key: str | None = None

    @model_validator(mode="after")
    def writes_require_approval(self) -> RepairAction:
        if self.kind is RepairActionKind.WRITE and not (self.approval_key or "").strip():
            raise ValueError("write actions require a non-empty approval key")
        return self


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
            stream = str(data.get(name, ""))
            if len(stream) > 20_000:
                data[name] = stream[:20_000]
                was_truncated = True
        data["truncated"] = was_truncated
        return data


class MiniNativeState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    step_number: int = Field(gt=0)
    history: list[dict[str, Any]] = Field(default_factory=list)


class CandidatePatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    published: bool
    patch_digest: str | None = None


__all__ = [
    "CandidatePatchResult",
    "MiniNativeState",
    "ModelRequest",
    "ModelResponse",
    "RepairAction",
    "RepairActionKind",
    "RepairActionResult",
]
