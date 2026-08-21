import subprocess
from pathlib import Path
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


class _LongLoopModel(_Model):
    def __init__(self) -> None:
        super().__init__()
        self.responses = [
            ToolCall(type="tool_call", tool="bash", arguments={"command": "echo ok"})
            for _ in range(12)
        ] + [FinalAnswer(type="final", answer="submitted")]


class _LongShell:
    async def execute(self, command: str) -> str:
        assert command == "echo ok"
        return "x" * 20_000


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
    observation = model.requests[1].history[-1]
    assert observation["kind"] == "TOOL_RESULT"
    assert "VALUE = 1" in observation["payload"]


@pytest.mark.asyncio
async def test_mini_linear_engine_uses_swe_task_prompt_contract() -> None:
    from agentforge.repair_engines.mini_linear import MiniLinearRepairEngine

    model = _Model()
    await MiniLinearRepairEngine(model, _Shell()).run(
        run_id=uuid4(), task="repair module", max_steps=2
    )

    system = model.requests[0].instructions
    task = model.requests[0].task
    assert isinstance(system, str) and "interact with a computer shell" in system
    assert "<pr_description>" in task
    assert "DO NOT MODIFY: Tests" in task
    assert model.requests[0].history == []


@pytest.mark.asyncio
async def test_mini_linear_engine_bounds_observations_and_history() -> None:
    from agentforge.repair_engines.mini_linear import MiniLinearRepairEngine

    model = _LongLoopModel()
    result = await MiniLinearRepairEngine(model, _LongShell()).run(
        run_id=uuid4(), task="repair module", max_steps=13
    )

    assert result.submitted
    assert len(model.requests[-1].history) <= 12
    assert all(
        len(str(item.get("payload", ""))) <= 10_500
        for item in model.requests[-1].history
        if isinstance(item, dict)
    )


@pytest.mark.asyncio
async def test_candidate_shell_runs_bash_in_candidate_root(tmp_path: Path) -> None:
    from agentforge.repair_engines.mini_linear import SubprocessCandidateShell

    calls: list[tuple[tuple[str, ...], Path]] = []

    def runner(arguments: tuple[str, ...], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append((arguments, cwd))
        return subprocess.CompletedProcess(arguments, 0, stdout="ok\n", stderr="")

    output = await SubprocessCandidateShell(tmp_path, runner=runner).execute("pwd")

    assert calls == [(('bash', '-c', 'pwd'), tmp_path)]
    assert output == "<returncode>0</returncode>\n<output>ok\n</output>"
