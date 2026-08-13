from collections.abc import Awaitable
from typing import Protocol

from pydantic import BaseModel

from agentforge.domain.models import ToolResult, ToolSpec


class Tool(Protocol):
    @property
    def spec(self) -> ToolSpec: ...

    @property
    def input_model(self) -> type[BaseModel]: ...

    def execute(self, arguments: BaseModel) -> ToolResult | Awaitable[ToolResult]: ...
