from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from agentforge.domain.test_execution import TestExecutionPlan


@runtime_checkable
class TestExecutionTool(Protocol):
    @property
    def managed_execution_kind(self) -> Literal["TEST_PROFILE"]: ...

    def prepare(self, arguments: BaseModel) -> TestExecutionPlan: ...
