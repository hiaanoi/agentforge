from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from agentforge.domain.enums import ToolRisk
from agentforge.domain.models import ToolSpec
from agentforge.models.base import FinalAnswer, ModelProvider, ModelRequest


class CandidateShell(Protocol):
    async def execute(self, command: str) -> str: ...


@dataclass(frozen=True, slots=True)
class MiniLinearResult:
    submitted: bool
    history: list[dict[str, object]]
    model_calls: int


_BASH_TOOL = ToolSpec(
    name="bash",
    description="Run one non-interactive shell command in the candidate workspace",
    input_schema={
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
    risk_level=ToolRisk.DANGEROUS,
    requires_approval=False,
)


class MiniLinearRepairEngine:
    def __init__(self, model: ModelProvider, shell: CandidateShell) -> None:
        self._model = model
        self._shell = shell

    async def run(self, *, run_id: UUID, task: str, max_steps: int) -> MiniLinearResult:
        history: list[dict[str, object]] = [
            {
                "role": "system",
                "content": "Use the bash tool to inspect, edit, and test the candidate workspace.",
            },
            {"role": "user", "content": task},
        ]
        for step in range(1, max_steps + 1):
            response = await self._model.generate(
                ModelRequest(
                    run_id=run_id,
                    task=task,
                    step_number=step,
                    history=history,  # type: ignore[arg-type]
                    tools=[_BASH_TOOL],
                )
            )
            action = response.action
            if isinstance(action, FinalAnswer):
                return MiniLinearResult(submitted=True, history=history, model_calls=step)
            if action.tool != "bash":
                raise ValueError("Mini linear engine only accepts bash actions")
            command = action.arguments.get("command")
            if not isinstance(command, str) or not command.strip():
                raise ValueError("Bash action requires a non-empty command")
            history.append(
                {
                    "role": "assistant",
                    "content": action.reason or "",
                    "tool_call": {"name": "bash", "arguments": {"command": command}},
                }
            )
            output = await self._shell.execute(command)
            history.append({"role": "tool", "tool_name": "bash", "content": output})
        return MiniLinearResult(submitted=False, history=history, model_calls=max_steps)
