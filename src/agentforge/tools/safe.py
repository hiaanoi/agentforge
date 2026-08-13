from pydantic import BaseModel, ConfigDict

from agentforge.domain.enums import ToolRisk
from agentforge.domain.models import ToolResult, ToolSpec


class EchoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class EchoTool:
    @property
    def input_model(self) -> type[BaseModel]:
        return EchoArguments

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="echo",
            description="Return the supplied text without external side effects.",
            input_schema=self.input_model.model_json_schema(),
            risk_level=ToolRisk.READ,
        )

    async def execute(self, arguments: BaseModel) -> ToolResult:
        parsed = EchoArguments.model_validate(arguments)
        return ToolResult(success=True, output=parsed.text)


class AddNumbersArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left: float
    right: float


class AddNumbersTool:
    @property
    def input_model(self) -> type[BaseModel]:
        return AddNumbersArguments

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="add_numbers",
            description="Add two numbers without external side effects.",
            input_schema=self.input_model.model_json_schema(),
            risk_level=ToolRisk.READ,
        )

    async def execute(self, arguments: BaseModel) -> ToolResult:
        parsed = AddNumbersArguments.model_validate(arguments)
        value = parsed.left + parsed.right
        if value.is_integer():
            return ToolResult(success=True, output=int(value))
        return ToolResult(success=True, output=value)
