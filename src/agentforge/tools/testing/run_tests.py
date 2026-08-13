from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agentforge.domain.enums import (
    ToolCapability,
    ToolErrorCode,
    ToolRisk,
    ToolSource,
)
from agentforge.domain.errors import ToolExecutionError
from agentforge.domain.models import ToolResult, ToolSpec
from agentforge.domain.test_execution import TestExecutionPlan
from agentforge.tools.testing.profiles import TestProfileRegistry


class RunTestsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(pattern=r"^[a-z_][a-z0-9_-]*$", max_length=100)


class RunTestsTool:
    def __init__(self, profiles: TestProfileRegistry) -> None:
        self._profiles = profiles
        self._spec = ToolSpec(
            name="run_tests",
            description="Run one administrator-registered test profile",
            input_schema=RunTestsArguments.model_json_schema(),
            risk_level=ToolRisk.DANGEROUS,
            source=ToolSource.LOCAL,
            capability=ToolCapability.TEST_PROFILE_EXECUTION,
            timeout_seconds=3600,
            requires_approval=True,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    @property
    def input_model(self) -> type[BaseModel]:
        return RunTestsArguments

    @property
    def managed_execution_kind(self) -> Literal["TEST_PROFILE"]:
        return "TEST_PROFILE"

    def prepare(self, arguments: BaseModel) -> TestExecutionPlan:
        validated = RunTestsArguments.model_validate(arguments)
        return self._profiles.prepare(validated.profile_id)

    def execute(self, arguments: BaseModel) -> ToolResult:
        raise ToolExecutionError(
            ToolErrorCode.MANAGED_EXECUTION_REQUIRED,
            "run_tests must execute through the managed test coordinator",
        )
