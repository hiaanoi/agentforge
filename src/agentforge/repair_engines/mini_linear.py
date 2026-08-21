from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from agentforge.domain.enums import ToolRisk
from agentforge.domain.models import ToolSpec
from agentforge.models.base import FinalAnswer, ModelProvider, ModelRequest, parse_model_output
from agentforge.models.domain import ModelResponse


class CandidateShell(Protocol):
    async def execute(self, command: str) -> str: ...


CandidateShellRunner = Callable[..., subprocess.CompletedProcess[str]]

_MINI_SYSTEM_PROMPT = (
    "You are a helpful assistant that can interact with a computer shell to solve "
    "programming tasks."
)


def _mini_task_prompt(task: str) -> str:
    return f"""<pr_description>
Consider the following PR description:
{task}
</pr_description>

<instructions>
You are a software engineer interacting continuously with a computer by submitting
commands. Work in the candidate workspace and make a general, minimal source fix.

For each response, include a short THOUGHT section and exactly one bash tool call.
Run commands, inspect their results, and use the next response to continue the repair.

Important boundaries:
- Modify regular source files needed for the fix.
- DO NOT MODIFY: Tests, configuration files, or build metadata.
- Do not fabricate command output or claim completion without verification.

Recommended workflow:
1. Inspect the relevant source and tests.
2. Reproduce or understand the reported behavior.
3. Edit the source files with a shell command.
4. Run the narrowest useful verification.
5. When the fix is ready and verified, return a final answer to submit the candidate.
</instructions>"""


class SubprocessCandidateShell:
    def __init__(
        self,
        root: Path,
        *,
        runner: CandidateShellRunner | None = None,
    ) -> None:
        self._root = root.resolve(strict=True)
        self._runner = runner or self._run

    async def execute(self, command: str) -> str:
        completed = await asyncio.to_thread(
            self._runner,
            ("bash", "-c", command),
            cwd=self._root,
        )
        output = completed.stdout + completed.stderr
        return f"<returncode>{completed.returncode}</returncode>\n<output>{output}</output>"

    @staticmethod
    def _run(arguments: tuple[str, ...], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            arguments,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )


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
                "content": _MINI_SYSTEM_PROMPT,
            },
            {"role": "user", "content": _mini_task_prompt(task)},
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
            action = (
                response.action
                if isinstance(response, ModelResponse)
                else parse_model_output(response)
            )
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
