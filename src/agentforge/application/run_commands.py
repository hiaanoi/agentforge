from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ResumeRecoveryChoice(StrEnum):
    AUTO = "AUTO"
    DECISION = "DECISION"
    MUTATION = "MUTATION"
    TEST_EXECUTION = "TEST_EXECUTION"
    MODEL = "MODEL"


class ResumeRun(BaseModel):
    """Idempotent command that claims one persisted recovery phase."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    command_id: UUID
    run_id: UUID
    recovery_choice: ResumeRecoveryChoice = ResumeRecoveryChoice.AUTO

    @model_validator(mode="before")
    @classmethod
    def exact_schema_version(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and (
            "schema_version" in value
            and (type(value["schema_version"]) is not int or value["schema_version"] != 1)
        ):
            raise ValueError("schema_version must be the exact integer 1")
        return value

    @property
    def command_type(self) -> str:
        return "RESUME_RUN"


class CancelRun(BaseModel):
    """Idempotent request to the current executor; it is not terminal authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    command_id: UUID
    run_id: UUID
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="before")
    @classmethod
    def exact_schema_version(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and (
            "schema_version" in value
            and (type(value["schema_version"]) is not int or value["schema_version"] != 1)
        ):
            raise ValueError("schema_version must be the exact integer 1")
        return value

    @property
    def command_type(self) -> str:
        return "CANCEL_RUN"
