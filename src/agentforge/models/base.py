from typing import TYPE_CHECKING, Annotated, Literal, Protocol, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from agentforge.domain.errors import ModelOutputError
from agentforge.domain.models import ToolSpec

if TYPE_CHECKING:
    from agentforge.models.domain import ModelResponse


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID | None = None
    model_call_id: UUID | None = None
    task: str = Field(min_length=1)
    step_number: int = Field(gt=0)
    instructions: str | None = None
    preserve_tool_call_text: bool = False
    history: list[JsonValue] = Field(default_factory=list)
    tools: list[ToolSpec] = Field(default_factory=list)


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_call"]
    call_id: str | None = None
    tool: str = Field(min_length=1)
    arguments: dict[str, JsonValue]
    reason: str | None = None


class FinalAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["final"]
    answer: str


ModelOutput: TypeAlias = Annotated[ToolCall | FinalAnswer, Field(discriminator="type")]
MODEL_OUTPUT_ADAPTER: TypeAdapter[ModelOutput] = TypeAdapter(ModelOutput)


def parse_model_output(value: object) -> ModelOutput:
    try:
        return MODEL_OUTPUT_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise ModelOutputError(f"Model output failed validation: {exc}") from exc


class ModelProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def journal_identity(self) -> str: ...

    async def generate(self, request: ModelRequest) -> "ModelResponse": ...
