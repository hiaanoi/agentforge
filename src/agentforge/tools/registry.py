from collections.abc import Iterable

from pydantic import BaseModel, JsonValue

from agentforge.domain.errors import (
    DuplicateToolError,
    InvalidToolSpecError,
    ToolNotFoundError,
)
from agentforge.domain.models import ToolSpec
from agentforge.tools.base import Tool


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise DuplicateToolError(name)
        if tool.spec.input_schema != tool.input_model.model_json_schema():
            raise InvalidToolSpecError(name)
        self._tools[name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(name) from exc

    def specs(self) -> list[ToolSpec]:
        return [self._tools[name].spec for name in sorted(self._tools)]

    def list_tools(self) -> list[Tool]:
        return [self._tools[name] for name in sorted(self._tools)]

    def validate_arguments(self, name: str, arguments: dict[str, JsonValue]) -> BaseModel:
        return self.get(name).input_model.model_validate(arguments)

    def export_schemas(self) -> list[dict[str, JsonValue]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.input_schema,
            }
            for spec in self.specs()
        ]
