from uuid import uuid4

import pytest

from agentforge.models.base import FinalAnswer, ModelRequest, ToolCall
from agentforge.models.domain import ModelResponse


class _Model:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []
        self.responses = [
            ToolCall(type="tool_call", tool="bash", arguments={"command": "cat src/module.py"}),
            FinalAnswer(type="final", answer="submitted"),
        ]

    @property
    def name(self) -> str:
        return "fake"

    @property
    def journal_identity(self) -> str:
        return "fake/model"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request.model_copy(deep=True))
        return ModelResponse(
            action=self.responses.pop(0),
            provider="fake",
            model="model",
            duration_ms=1,
            attempt_count=1,
        )


class _Shell:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def execute(self, command: str) -> str:
        self.commands.append(command)
        return "VALUE = 1\n"


@pytest.mark.asyncio
async def test_mini_linear_engine_appends_shell_observation_before_next_turn() -> None:
    from agentforge.repair_engines.mini_linear import MiniLinearRepairEngine

    model = _Model()
    shell = _Shell()
    result = await MiniLinearRepairEngine(model, shell).run(
        run_id=uuid4(), task="repair module", max_steps=2
    )

    assert result.submitted
    assert shell.commands == ["cat src/module.py"]
    assert "VALUE = 1" in model.requests[1].history[-1]["content"]
